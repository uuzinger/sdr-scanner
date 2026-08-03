# Scanner Transcription Pipeline — Architecture

**System:** Loudoun County P25 (WACN BEE00, System 373, Site 001-001)
**Status:** Design — capture stage operational, transcription and storage stages to be built

---

## 1. Objective

Convert live P25 trunked radio traffic into a durable, queryable corpus of timestamped
transcripts with full call metadata, and drive proximity-based alerting off that corpus.

Three capabilities, in build order:

1. **Archive** — every non-encrypted voice call transcribed and stored with metadata
2. **Query** — arbitrary retrieval by text, talkgroup, unit, and time window
3. **Alert** — push notification when traffic matches a neighborhood gazetteer

---

## 2. Host inventory

| Host | Role | Notes |
|---|---|---|
| MicroPC | SDR capture | Ubuntu 24.04, Airspy Mini, trunk-recorder |
| `gpu-host` | Transcription | Ubuntu 24.04, RTX Pro 6000 Blackwell 96GB |
| `db-host` | Postgres + query/dashboard | Dedicated VM, new deployment |
| NAS | Audio storage | `/vol1/sdr-scanner` NFS export, 68TB available |

`gpu-host` and `db-host` are placeholders — substitute real hostnames at deploy time,
or resolve them via `/etc/hosts`.

**NFS mounts required:**

| Host | Mount point | Purpose |
|---|---|---|
| MicroPC | `/vol1/sdr-scanner` | trunk-recorder writes audio here |
| `gpu-host` | `/vol1/sdr-scanner` | Worker reads audio from the same path |
| `db-host` | `/vol1/sdr-scanner` | Dashboard serves audio files to the browser |

Mounting to the same path on all three hosts means `audio_path` in the database is a
valid filesystem path everywhere — no translation layer needed.

The MicroPC does capture and nothing else. It must never block on network I/O to
another host — a stalled hook backs up call handling and drops traffic.

---

## 3. Data flow

```
trunk-recorder (MicroPC)
    writes  <talkgroup>-<epoch>.m4a  +  <talkgroup>-<epoch>.json
        │
        ├── uploadScript fires on call completion, receives JSON path as $1
        │
        ▼
Postgres  jobs  table                        ← durable queue, SKIP LOCKED
        │
        ▼
Transcription worker (gpu-host)
        │  pre-filters, fetches audio, POSTs to local Whisper
        ▼
Whisper HTTP service :10301 (gpu-host)       ← dedicated instance, NOT the HA one
        │
        ▼
Postgres  calls  table  (+ GIN index on tsvector)
        │
        ├──▶ SQL / web dashboard
        └──▶ gazetteer matcher ──▶ Pushover
```

---

## 4. Stage 1 — Capture

### 4.1 Required trunk-recorder config

Per-system block in `config.json`:

```json
{
  "shortName": "loudoun",
  "control_channels": [ ... ],
  "type": "p25",
  "modulation": "qpsk",
  "talkgroupsFile": "loudoun-talkgroups.csv",
  "captureDir": "/vol1/sdr-scanner/audio",
  "audioArchive": true,
  "callLog": true,
  "compressWav": true,
  "uploadScript": "/opt/scanner/enqueue.sh",
  "unitTagsFile": "loudoun-units.csv"
}
```

- `captureDir` points to the NFS mount. Audio and JSON sidecars land directly on the
  NAS — no separate copy step, and the path is valid from `gpu-host` and `db-host`
  without translation.
- `callLog: true` produces the `.json` sidecar. This is mandatory — it carries the
  metadata the entire downstream system depends on.
- `audioArchive: true` retains audio after the upload script runs. Without it,
  trunk-recorder deletes the file and the worker finds nothing.
- `compressWav: true` yields `.m4a` (AAC). faster-whisper decodes it natively via
  PyAV. No separate WAV handling needed.

### 4.2 Sidecar JSON fields consumed downstream

| Field | Use |
|---|---|
| `talkgroup` | Primary partition key |
| `talkgroup_tag`, `talkgroup_description`, `talkgroup_group` | Denormalized labels |
| `start_time`, `stop_time`, `call_length` | Timestamps, duration filter |
| `freq` | Diagnostics |
| `encrypted` | Hard skip |
| `srcList` | Unit IDs with per-transmission offsets — enables "which units were on this call" |
| `freqList` | Diagnostics, error rate per transmission |

### 4.3 Known talkgroup exclusions

- **TG 5010** — fires on a fixed 10-second cadence, carries no voice. Confirmed
  data/telemetry. Exclude at the worker, not the recorder, so the pattern stays
  visible in the job table.

### 4.4 Radio configuration dependency

The Airspy requires `"error": 2939` in the SDR source block. Sign is positive;
`-2939` kills control channel decode. Without this correction the recorder produces
silent captures that look like successful calls — the failure mode is invisible until
you transcribe them, so verify this is still in place before trusting output.

---

## 5. Stage 2 — Enqueue hook

The hook has exactly one job: write a row and exit. No HTTP, no ffmpeg, no retries.

`/opt/scanner/enqueue.sh`:

```bash
#!/usr/bin/env bash
# Invoked by trunk-recorder on call completion.
# $1 = absolute path to the call's .json sidecar
set -uo pipefail

JSON="$1"
AUDIO="${JSON%.json}.m4a"

PGPASSWORD="$SCANNER_DB_PASS" psql \
  -h db-host -U scanner -d scanner \
  -v ON_ERROR_STOP=0 -q -c \
  "INSERT INTO jobs (json_path, audio_path)
   VALUES ('${JSON}', '${AUDIO}')
   ON CONFLICT (json_path) DO NOTHING;" \
  >/dev/null 2>>/var/log/scanner/enqueue.err &

exit 0
```

Design notes:

- Backgrounded with `&` and immediate `exit 0`. trunk-recorder never waits.
- `ON CONFLICT DO NOTHING` on a unique `json_path` makes re-fires idempotent.
- Errors go to a log file, never to stdout — trunk-recorder captures child output.
- If the database is unreachable, calls are lost from the queue but audio remains on
  disk. A reconciliation sweep (§10.3) recovers them.

**Alternative if you want zero DB dependency on the MicroPC:** have the hook append the
JSON path to a local spool file, and run a small forwarder that drains the spool into
Postgres. Adds a moving part; buys full offline tolerance. Worth it only if the link
between the MicroPC and the DB host is unreliable.

---

## 6. Stage 3 — Job queue

Postgres, not Redis. Volume is 2–6k calls/day. A dedicated broker adds an operational
dependency and buys nothing at this scale.

```sql
CREATE TABLE jobs (
  id           bigserial PRIMARY KEY,
  json_path    text NOT NULL UNIQUE,
  audio_path   text NOT NULL,
  state        text NOT NULL DEFAULT 'pending',
    -- pending | running | done | skipped | failed
  attempts     int  NOT NULL DEFAULT 0,
  last_error   text,
  enqueued_at  timestamptz NOT NULL DEFAULT now(),
  started_at   timestamptz,
  finished_at  timestamptz
);

CREATE INDEX jobs_pending_idx ON jobs (enqueued_at) WHERE state = 'pending';
CREATE INDEX jobs_state_idx   ON jobs (state, finished_at DESC);
```

Claim pattern:

```sql
UPDATE jobs
SET state = 'running', started_at = now(), attempts = attempts + 1
WHERE id = (
  SELECT id FROM jobs
  WHERE state = 'pending'
     OR (state = 'running' AND started_at < now() - interval '10 minutes')
  ORDER BY enqueued_at
  FOR UPDATE SKIP LOCKED
  LIMIT 1
)
RETURNING id, json_path, audio_path, attempts;
```

The `running AND started_at < now() - 10min` clause reclaims jobs orphaned by a worker
crash. Jobs exceeding 5 attempts move to `failed` and are left for manual inspection.

---

## 7. Stage 4 — Transcription worker

Runs on `gpu-host`. Single process is sufficient; a burst of 30 calls clears in
well under a minute.

### 7.1 Pre-filter (before any GPU work)

Applied in order, each producing `state = 'skipped'` with a reason:

| Condition | Rationale |
|---|---|
| `encrypted == true` | No decodable audio |
| `call_length < 1.5` | Squelch tails and control artifacts |
| `talkgroup IN (5010, …)` | Known data/telemetry talkgroups |
| Audio file missing or < 4KB | Failed capture |
| `audio_sha256` already in `calls` | Duplicate re-fire |

This filter eliminates a large fraction of jobs at near-zero cost and materially
reduces hallucination volume.

### 7.2 Worker skeleton

```python
#!/usr/bin/env python3
"""Scanner transcription worker. Runs on gpu-host."""

import hashlib, json, os, time, pathlib
import httpx, psycopg
from psycopg.rows import dict_row

DSN         = os.environ["SCANNER_DSN"]
WHISPER_URL = "http://127.0.0.1:10301/transcribe"
SKIP_TG     = {5010}
MIN_SECONDS = 1.5
MAX_ATTEMPTS = 5

CLAIM = """
UPDATE jobs SET state='running', started_at=now(), attempts=attempts+1
WHERE id = (
  SELECT id FROM jobs
  WHERE state='pending'
     OR (state='running' AND started_at < now() - interval '10 minutes')
  ORDER BY enqueued_at FOR UPDATE SKIP LOCKED LIMIT 1)
RETURNING id, json_path, audio_path, attempts;
"""


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def should_skip(meta, audio_path):
    if meta.get("encrypted"):
        return "encrypted"
    if float(meta.get("call_length", 0)) < MIN_SECONDS:
        return "too_short"
    if int(meta.get("talkgroup", 0)) in SKIP_TG:
        return "data_talkgroup"
    p = pathlib.Path(audio_path)
    if not p.exists() or p.stat().st_size < 4096:
        return "audio_missing"
    return None


def handle(conn, job):
    meta = json.loads(pathlib.Path(job["json_path"]).read_text())

    reason = should_skip(meta, job["audio_path"])
    if reason:
        conn.execute(
            "UPDATE jobs SET state='skipped', last_error=%s, finished_at=now() "
            "WHERE id=%s", (reason, job["id"]))
        return

    digest = sha256(job["audio_path"])
    dupe = conn.execute(
        "SELECT 1 FROM calls WHERE audio_sha256=%s", (digest,)).fetchone()
    if dupe:
        conn.execute(
            "UPDATE jobs SET state='skipped', last_error='duplicate', "
            "finished_at=now() WHERE id=%s", (job["id"],))
        return

    with open(job["audio_path"], "rb") as fh:
        r = httpx.post(
            WHISPER_URL,
            files={"file": (os.path.basename(job["audio_path"]), fh)},
            data={"talkgroup_tag": meta.get("talkgroup_tag", "")},
            timeout=300.0,
        )
    r.raise_for_status()
    result = r.json()

    conn.execute("""
        INSERT INTO calls (
          system, talkgroup, talkgroup_tag, talkgroup_group,
          call_start, call_length, freq, encrypted,
          src_list, freq_list, audio_path, audio_sha256,
          transcript, segments, avg_logprob, model, transcribed_at, suspect)
        VALUES (%s,%s,%s,%s, to_timestamp(%s),%s,%s,%s,
                %s,%s,%s,%s, %s,%s,%s,%s, now(), %s)
        ON CONFLICT (audio_sha256) DO NOTHING;
    """, (
        meta.get("short_name"), meta["talkgroup"], meta.get("talkgroup_tag"),
        meta.get("talkgroup_group"), meta["start_time"], meta.get("call_length"),
        meta.get("freq"), bool(meta.get("encrypted")),
        json.dumps(meta.get("srcList", [])), json.dumps(meta.get("freqList", [])),
        job["audio_path"], digest,
        result["text"], json.dumps(result["segments"]),
        result["avg_logprob"], result["model"], result["suspect"],
    ))

    conn.execute(
        "UPDATE jobs SET state='done', finished_at=now() WHERE id=%s",
        (job["id"],))


def main():
    with psycopg.connect(DSN, row_factory=dict_row, autocommit=True) as conn:
        while True:
            job = conn.execute(CLAIM).fetchone()
            if not job:
                time.sleep(2)
                continue
            try:
                handle(conn, job)
            except Exception as exc:
                state = 'failed' if job["attempts"] >= MAX_ATTEMPTS else 'pending'
                conn.execute(
                    "UPDATE jobs SET state=%s, last_error=%s WHERE id=%s",
                    (state, str(exc)[:2000], job["id"]))


if __name__ == "__main__":
    main()
```

---

## 8. Stage 5 — Whisper service

### 8.1 Why a second instance

The running Wyoming service is unsuitable for this workload:

```
python3 -m wyoming_faster_whisper --uri tcp://0.0.0.0:10300 \
  --device cuda --model large-v3-turbo --beam-size 1 --language en
```

| Limitation | Consequence |
|---|---|
| No per-request `initial_prompt` | Loses the largest available accuracy gain on vocoded P25 audio |
| Returns a bare string | No `avg_logprob`, no `no_speech_prob`, no segments — hallucination filtering is impossible |
| Serializes requests | A 30-call incident burst stalls Home Assistant voice for a minute |
| `--beam-size 1` | Tuned for HA latency; wrong tradeoff for archival accuracy |
| No VAD control | Squelch tails feed the model pure noise |

Run a dedicated instance on port **10301**. `large-v3-turbo` in float16 occupies under
2GB. On 96GB this is free.

Leave the Wyoming service on 10300 untouched.

### 8.2 Service implementation

`/opt/scanner/whisper_service.py`:

```python
#!/usr/bin/env python3
import os, re, tempfile
from fastapi import FastAPI, UploadFile, File, Form
from faster_whisper import WhisperModel

MODEL_NAME = "large-v3-turbo"

DOMAIN_PROMPT = (
    "Loudoun County fire and rescue radio traffic. LCFR, Ashburn, Sterling, "
    "Dulles, South Riding, Chantilly, Aldie, Arcola, Brambleton. "
    "Engine, Tower, Truck, Medic, Ambulance, Battalion, Rescue, Tanker. "
    "Route 50, Route 7, Loudoun County Parkway, Braddock Road, Gum Spring Road, "
    "Belmont Ridge Road, Waxpool Road, Ryan Road, Evergreen Mills Road. "
    "Dispatched, on scene, en route, staging, command, working incident, "
    "structure fire, automatic fire alarm, motor vehicle accident with injuries, "
    "unresponsive party, sick person, fall victim, chest pain, difficulty breathing, "
    "PSA, cross street, apparatus, mark up, clear, in service."
)

HALLUCINATIONS = {
    "thank you.", "thanks for watching!", "thank you for watching.",
    "you", "bye.", "bye bye.", ".", "subtitles by the amara.org community",
    "please subscribe.", "i'll see you next time.", "okay.",
}

app = FastAPI()
model = WhisperModel(MODEL_NAME, device="cuda", compute_type="float16")


def degenerate(text: str) -> bool:
    """Detect runaway repetition."""
    words = text.lower().split()
    if len(words) < 6:
        return False
    for n in (1, 2, 3):
        grams = [tuple(words[i:i + n]) for i in range(len(words) - n + 1)]
        if grams and max(grams.count(g) for g in set(grams)) > len(grams) * 0.5:
            return True
    return False


@app.post("/transcribe")
async def transcribe(file: UploadFile = File(...),
                     talkgroup_tag: str = Form("")):
    suffix = os.path.splitext(file.filename)[1] or ".m4a"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(await file.read())
        path = tmp.name

    try:
        prompt = DOMAIN_PROMPT
        if talkgroup_tag:
            prompt = f"{talkgroup_tag}. {prompt}"

        segments, info = model.transcribe(
            path,
            language="en",
            beam_size=5,
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 300},
            condition_on_previous_text=False,   # non-negotiable
            initial_prompt=prompt,
            temperature=[0.0, 0.2, 0.4],
        )
        segs = [{
            "start": s.start, "end": s.end, "text": s.text.strip(),
            "avg_logprob": s.avg_logprob, "no_speech_prob": s.no_speech_prob,
            "compression_ratio": s.compression_ratio,
        } for s in segments]
    finally:
        os.unlink(path)

    text = " ".join(s["text"] for s in segs).strip()
    text = re.sub(r"\s+", " ", text)
    avg_lp = (sum(s["avg_logprob"] for s in segs) / len(segs)) if segs else -10.0

    suspect = (
        not text
        or text.lower().strip() in HALLUCINATIONS
        or avg_lp < -1.0
        or (segs and min(s["no_speech_prob"] for s in segs) > 0.6)
        or degenerate(text)
    )

    return {
        "text": text,
        "segments": segs,
        "avg_logprob": avg_lp,
        "duration": info.duration,
        "model": MODEL_NAME,
        "suspect": suspect,
    }
```

### 8.3 systemd unit

`/etc/systemd/system/scanner-whisper.service`:

```ini
[Unit]
Description=Scanner Whisper transcription service
After=network-online.target

[Service]
User=scanner
WorkingDirectory=/opt/scanner
ExecStart=/opt/scanner/venv/bin/uvicorn whisper_service:app \
          --host 127.0.0.1 --port 10301 --workers 1
Restart=always
RestartSec=5
Environment=HF_HOME=/config

[Install]
WantedBy=multi-user.target
```

Bind to `127.0.0.1`. The worker is on the same host; there is no reason to expose it.

### 8.4 Prompt tuning

`DOMAIN_PROMPT` is the highest-leverage knob in the system. Revise it as real
transcripts accumulate:

- Add street names the model consistently mangles
- Add unit designators actually in use (learned from `unitTagsFile` and from audio)
- Keep it under roughly 200 tokens — Whisper truncates long prompts and over-long
  prompts increase the chance the model echoes prompt text into the transcript
- Do **not** put rare words in it that never occur; that induces false positives

---

## 9. Stage 6 — Storage

```sql
CREATE TABLE calls (
  id              bigserial PRIMARY KEY,
  system          text,
  talkgroup       int NOT NULL,
  talkgroup_tag   text,
  talkgroup_group text,
  call_start      timestamptz NOT NULL,
  call_length     numeric,
  freq            bigint,
  encrypted       boolean DEFAULT false,
  src_list        jsonb,
  freq_list       jsonb,
  audio_path      text,
  audio_sha256    text UNIQUE,
  transcript      text,
  segments        jsonb,
  avg_logprob     real,
  model           text,
  transcribed_at  timestamptz,
  suspect         boolean DEFAULT false,
  alerted         boolean DEFAULT false,
  tsv tsvector GENERATED ALWAYS AS (
        to_tsvector('english', coalesce(transcript, ''))) STORED
);

CREATE INDEX calls_tsv_idx      ON calls USING gin (tsv);
CREATE INDEX calls_tg_time_idx  ON calls (talkgroup, call_start DESC);
CREATE INDEX calls_time_idx     ON calls (call_start DESC);
CREATE INDEX calls_src_idx      ON calls USING gin (src_list jsonb_path_ops);
CREATE INDEX calls_alert_idx    ON calls (call_start) WHERE NOT alerted AND NOT suspect;
```

### 9.1 Reference tables

```sql
-- Loaded from the RadioReference CSV already used by trunk-recorder
CREATE TABLE talkgroups (
  talkgroup   int PRIMARY KEY,
  alpha_tag   text,
  description text,
  tag         text,
  category    text,
  is_data     boolean DEFAULT false   -- drives the worker skip list
);

-- Unit ID to alias. Backfilled from unitTagsFile and refined from transcripts.
CREATE TABLE units (
  unit_id   bigint PRIMARY KEY,
  alias     text,
  agency    text,
  confirmed boolean DEFAULT false,
  last_seen timestamptz
);
```

Drive the worker's `SKIP_TG` set from `talkgroups.is_data` rather than a hardcoded
Python constant once the table is populated.

---

## 10. Operational concerns

### 10.1 Capacity

| Metric | Estimate |
|---|---|
| Calls/day (countywide) | 2,000–6,000 |
| Median call length | 8–15 s |
| Daily audio | 6–25 hours |
| Realtime factor, `large-v3-turbo` on RTX Pro 6000 | 30–60× |
| Daily GPU time | 10–50 minutes |
| Audio storage at ~12 KB/s AAC | ~1 GB/day |

GPU is not the constraint. At ~1 GB/day of audio, 68TB on the NAS represents roughly
180 years of capacity — retain audio indefinitely. Transcripts and metadata on the
Postgres VM are a small fraction of that volume (5–15 GB/year).

### 10.2 Monitoring

Surface these on the existing web dashboard, and alert via the same Pushover path used
by `drive-monitor`:

- `jobs` pending depth — sustained growth means the worker is down or wedged
- `jobs` in `failed` state — any non-zero count warrants inspection
- Ratio of `suspect` to total in the last hour — a spike indicates a radio-side
  regression (gain drift, frequency error) before it becomes obvious anywhere else
- Time since last `calls` insert — a silent gap is the strongest signal that capture
  has stopped

The suspect-ratio metric is worth building early. It is the only automated way to
detect that the Airspy has drifted back into producing silent captures.

### 10.3 Reconciliation

Nightly sweep on the MicroPC: walk the capture directory, find `.json` files with no
corresponding `jobs` row, and enqueue them. Covers hook failures and DB outages.

### 10.4 Backfill and reprocessing

Prompt changes and model upgrades will invalidate old transcripts. Keep audio long
enough to reprocess. To re-run a window:

```sql
INSERT INTO jobs (json_path, audio_path)
SELECT replace(audio_path, '.m4a', '.json'), audio_path
FROM calls
WHERE call_start BETWEEN %s AND %s AND audio_path IS NOT NULL
ON CONFLICT (json_path) DO UPDATE SET state='pending', attempts=0;
```

Store `model` and a `prompt_version` on each row so you can tell which transcripts came
from which configuration.

---

## 11. Query layer

### 11.1 Full-text

```sql
-- Incidents mentioning a street, last 7 days
SELECT call_start, talkgroup_tag, transcript
FROM calls
WHERE tsv @@ websearch_to_tsquery('english', 'structure fire "braddock road"')
  AND call_start > now() - interval '7 days'
  AND NOT suspect
ORDER BY call_start DESC;

-- Reconstruct an incident: all traffic on a talkgroup around a known call
SELECT call_start, transcript
FROM calls
WHERE talkgroup = 2875
  AND call_start BETWEEN %s - interval '10 minutes'
                     AND %s + interval '30 minutes'
ORDER BY call_start;

-- Which units appeared on calls matching a phrase
SELECT DISTINCT (s->>'src')::bigint AS unit_id, u.alias
FROM calls c, jsonb_array_elements(c.src_list) s
LEFT JOIN units u ON u.unit_id = (s->>'src')::bigint
WHERE c.tsv @@ websearch_to_tsquery('english', 'working structure fire');
```

`websearch_to_tsquery` is the right entry point for a UI — it accepts quoted phrases,
`OR`, and `-exclusion` in syntax users already know.

### 11.2 Semantic search (phase 3)

Add `pgvector`, embed each transcript on the GPU host, and index with HNSW. This makes
"anything about a person down near the townhomes" work without knowing dispatch's exact
phrasing.

Sequence this **after** full-text is in production. On 8kHz vocoded audio, transcripts
are lossy enough that exact matching on proper nouns outperforms embeddings until
transcript quality is tuned.

### 11.3 Natural-language query (phase 4)

Text-to-SQL against the local Qwen instance, constrained to a read-only role with a
schema-only prompt. Retrieval-augmented summarization over a time window is the more
valuable variant: "summarize everything that happened on LCFR talkgroups overnight."

---

## 12. Alerting

Closes the loop on the original objective.

```sql
CREATE TABLE gazetteer (
  id       serial PRIMARY KEY,
  term     text NOT NULL,       -- street, subdivision, landmark
  kind     text NOT NULL,       -- 'street' | 'subdivision' | 'landmark'
  priority int DEFAULT 1
);

CREATE TABLE alert_keywords (
  id       serial PRIMARY KEY,
  term     text NOT NULL,       -- 'structure fire', 'shots fired', 'entrapment'
  priority int DEFAULT 1
);
```

Matcher runs every 30 seconds:

1. Select `calls` where `NOT alerted AND NOT suspect AND call_start > now() - 15 min`
2. Score against gazetteer and keyword terms
3. Above threshold, push via Pushover with talkgroup, timestamp, and transcript
4. Set `alerted = true`

**Debounce.** A single incident generates dozens of calls. Suppress by
`(talkgroup, matched_term)` for 20 minutes, and mark every call in the window as
alerted so the incident produces one notification, not forty.

Start with high-precision terms only. A noisy alerter gets muted, and a muted alerter
is worth nothing.

---

## 13. Build order

| Phase | Deliverable | Exit criteria |
|---|---|---|
| 1 | Postgres schema, `jobs` + `calls` | Tables created, indexes present |
| 2 | `enqueue.sh` wired to `uploadScript` | Rows appearing within seconds of a call |
| 3 | Whisper service on :10301 | `curl` a sample `.m4a`, get structured JSON |
| 4 | Worker | `calls` populating, pending depth stays near zero |
| 5 | Suspect-ratio and pending-depth monitoring | Metrics on the dashboard |
| 6 | Prompt tuning pass | Suspect ratio below ~15%, spot-check accuracy acceptable |
| 7 | Full-text query UI on the existing dashboard | Search box, talkgroup and time filters |
| 8 | Gazetteer + Pushover alerting | One notification per real incident |
| 9 | pgvector, then NL query | — |

Phases 1–4 are the minimum viable system. Do not start phase 8 before phase 6 —
alerting on untuned transcripts produces false positives that will train you to ignore
the notifications.

---

## 14. Decisions log

| Decision | Resolution |
|---|---|
| Postgres host | New dedicated VM (`db-host`). See §15 for sizing. |
| Audio storage | NAS NFS export `/vol1/sdr-scanner`, mounted on all three hosts at the same path |
| Retention | Unlimited — 68TB available on NAS; keep audio indefinitely |
| Enqueue resilience | Simple direct-to-Postgres hook + nightly reconciliation sweep (§10.3) |
| Multi-site scope | Single site only |

---

## 15. Postgres VM sizing

**Workload profile:** write-light OLTP (peak ~10 inserts/minute during a busy
incident), mixed with occasional analytical queries from the dashboard. GIN indexes
for full-text search are the primary memory consumer. pgvector is a planned phase-3
addition (HNSW index).

### Recommended spec

| Resource | Minimum | Recommended |
|---|---|---|
| vCPUs | 2 | 4 |
| RAM | 4 GB | 8 GB |
| OS disk | 32 GB | 32 GB |
| Data disk | 100 GB | 200 GB (thin-provisioned) |

**RAM rationale.** GIN indexes for FTS are read-heavy and benefit from caching in
`shared_buffers`. A 4GB VM is workable until pgvector lands — HNSW graphs for even a
year of transcript embeddings (1536-dim float32, ~2M rows) add several GB of working
set. 8GB keeps you clear of that boundary.

**vCPU rationale.** 2 vCPUs handles steady-state writes and dashboard queries
comfortably. 4 vCPUs is the recommended floor because HNSW index builds during
embedding ingestion (phase 9) saturate a core for minutes at a time.

**Data disk.** The Postgres data volume (transcripts, metadata, FTS indexes) will grow
roughly 5–15 GB/year at current call volume. A 200GB thin-provisioned disk provides
many years of headroom. Do **not** put this on NFS — Postgres requires reliable fsync
semantics and is sensitive to NFS latency; put it on local VM storage. Audio stays on
the NAS; Postgres only stores the paths and text.

### Key postgresql.conf knobs

Set these at provisioning time:

```ini
shared_buffers            = 2GB        # ~25% of RAM on an 8GB VM
effective_cache_size      = 6GB        # ~75% of RAM
work_mem                  = 64MB       # per-sort; FTS query plans benefit
maintenance_work_mem      = 512MB      # GIN and HNSW index builds
checkpoint_completion_target = 0.9
wal_buffers               = 16MB
random_page_cost          = 1.1        # SSD/NVMe-backed storage
```

`work_mem` at 64MB is deliberately conservative — it applies per sort node per
connection, and analytical queries can fan out. Watch `pg_stat_activity` during
dashboard queries and tune up if you see spill-to-disk in `EXPLAIN ANALYZE`.

### pgvector addition (phase 9)

When pgvector is enabled, add:

```ini
max_parallel_workers_per_gather = 2    # HNSW searches parallelize well
```

HNSW index creation with `m=16, ef_construction=64` on 100k rows takes 2–5 minutes;
schedule reindexing off-hours.

### Networking

The VM must be reachable from:
- MicroPC (enqueue hook — low bandwidth, latency-tolerant)
- `gpu-host` (worker — sustained inserts, moderate bandwidth)
- Dashboard clients (query — bursty reads)

Place it on the same VLAN as `gpu-host` if your hypervisor supports it. A dedicated
interface for Postgres traffic is not necessary at this scale.
