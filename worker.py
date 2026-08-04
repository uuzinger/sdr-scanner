#!/usr/bin/env python3
"""
Stage 4 worker: claims rows from the `jobs` queue, transcribes the
associated call audio with faster-whisper, writes the result into
`transcripts`, and marks the job done/failed.

Run under systemd with Restart=always. Safe to run multiple copies
concurrently (SKIP LOCKED handles the coordination).
"""

import json
import logging
import os
import socket
import time

import psycopg2
import psycopg2.extras
from faster_whisper import WhisperModel

# ---- config (env-overridable) ----
DB_DSN = os.environ.get(
    "SCANNER_DB_DSN",
    "host=db-host dbname=scanner user=scanner",
)
MODEL_NAME = os.environ.get("WHISPER_MODEL", "large-v3-turbo")
DEVICE = os.environ.get("WHISPER_DEVICE", "cuda")
COMPUTE_TYPE = os.environ.get("WHISPER_COMPUTE_TYPE", "float16")
POLL_SECONDS = float(os.environ.get("POLL_SECONDS", "2"))
MAX_ATTEMPTS = int(os.environ.get("MAX_ATTEMPTS", "5"))
WORKER_ID = f"{socket.gethostname()}-{os.getpid()}"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("scanner-worker")

CLAIM_SQL = """
UPDATE jobs SET
  state      = 'running',
  attempts   = attempts + 1,
  started_at = now(),
  locked_by  = %s
WHERE id = (
  SELECT id FROM jobs
  WHERE state = 'pending'
  ORDER BY id
  FOR UPDATE SKIP LOCKED
  LIMIT 1
)
RETURNING id, json_path, audio_path, attempts;
"""

DONE_SQL = """
UPDATE jobs SET state = 'done', finished_at = now(), locked_by = NULL
WHERE id = %s;
"""

FAIL_SQL = """
UPDATE jobs SET
  state       = CASE WHEN attempts >= %s THEN 'failed' ELSE 'pending' END,
  last_error  = %s,
  finished_at = CASE WHEN attempts >= %s THEN now() ELSE NULL END,
  locked_by   = NULL
WHERE id = %s;
"""

INSERT_TRANSCRIPT_SQL = """
INSERT INTO transcripts
  (job_id, talkgroup, talkgroup_tag, talkgroup_grp,
   call_start, call_length, text, language, confidence)
VALUES (%s, %s, %s, %s, to_timestamp(%s), %s, %s, %s, %s);
"""


def load_call_metadata(json_path):
    """Pull talkgroup/timing fields out of trunk-recorder's sidecar JSON."""
    with open(json_path) as f:
        d = json.load(f)
    return {
        "talkgroup": d.get("talkgroup"),
        "talkgroup_tag": d.get("talkgroup_tag") or d.get("talkgrouptag"),
        "talkgroup_grp": d.get("talkgroup_group"),
        "start_time": d.get("start_time") or d.get("startTime"),
        "call_length": d.get("call_length") or d.get("length"),
    }


def transcribe(model, audio_path):
    segments, info = model.transcribe(
        audio_path,
        language="en",
        beam_size=1,
        vad_filter=True,  # important for scanner audio: trims dead air/squelch tail
    )
    text = " ".join(seg.text.strip() for seg in segments).strip()
    # avg per-segment logprob isn't a calibrated confidence, but it's a
    # useful relative signal for later filtering out garbage transcripts
    conf = getattr(info, "language_probability", None)
    return text, conf


def main():
    log.info("loading %s on %s (%s)", MODEL_NAME, DEVICE, COMPUTE_TYPE)
    model = WhisperModel(MODEL_NAME, device=DEVICE, compute_type=COMPUTE_TYPE)

    conn = psycopg2.connect(DB_DSN)
    conn.autocommit = False

    log.info("worker %s ready, polling every %.1fs", WORKER_ID, POLL_SECONDS)

    while True:
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                cur.execute(CLAIM_SQL, (WORKER_ID,))
                row = cur.fetchone()
                conn.commit()

            if row is None:
                time.sleep(POLL_SECONDS)
                continue

            job_id, json_path, audio_path, attempts = row
            log.info("claimed job %s: %s", job_id, audio_path)

            try:
                meta = load_call_metadata(json_path)
                text, conf = transcribe(model, audio_path)

                with conn.cursor() as cur:
                    cur.execute(
                        INSERT_TRANSCRIPT_SQL,
                        (
                            job_id,
                            meta["talkgroup"],
                            meta["talkgroup_tag"],
                            meta["talkgroup_grp"],
                            meta["start_time"],
                            meta["call_length"],
                            text,
                            "en",
                            conf,
                        ),
                    )
                    cur.execute(DONE_SQL, (job_id,))
                conn.commit()
                log.info("job %s done (%d chars)", job_id, len(text))

            except Exception as e:
                conn.rollback()
                log.exception("job %s failed", job_id)
                with conn.cursor() as cur:
                    cur.execute(
                        FAIL_SQL,
                        (MAX_ATTEMPTS, str(e)[:1000], MAX_ATTEMPTS, job_id),
                    )
                conn.commit()

        except psycopg2.OperationalError:
            log.exception("db connection lost, reconnecting in 5s")
            time.sleep(5)
            try:
                conn = psycopg2.connect(DB_DSN)
                conn.autocommit = False
            except Exception:
                pass


if __name__ == "__main__":
    main()
