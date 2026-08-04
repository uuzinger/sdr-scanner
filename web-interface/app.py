"""
scanner-web — browse, search and play back Loudoun P25 scanner traffic.

Runs on the MicroPC alongside trunk-recorder. Reads jobs/transcripts on pgsql,
serves audio off the NFS mount at /vol1/sdr-scanner.

Schema this targets:
  transcripts(id, job_id, talkgroup, talkgroup_tag, talkgroup_grp,
              call_start, call_length, text, language, confidence, created_at)
  jobs(id, json_path, audio_path, state, attempts, last_error,
       enqueued_at, started_at, finished_at, locked_by)
"""
from __future__ import annotations

import html
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

BASE = Path(__file__).parent

DSN = os.environ.get("SCANNER_DSN", "postgresql://scanner_web@192.168.1.28:5432/scanner")
AUDIO_ROOT = Path(os.environ.get("AUDIO_ROOT", "/vol1/sdr-scanner/audio")).resolve()
PAGE = int(os.environ.get("PAGE_SIZE", "60"))

# ts_headline markers — control chars, so they can't collide with transcript text.
HL_START, HL_STOP = "\x02", "\x03"

# Sort key: call_start is what we want, created_at is the never-null backstop.
CT = "COALESCE(t.call_start, t.created_at)"

pool: AsyncConnectionPool | None = None

app = FastAPI(title="scanner-web")
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=str(BASE / "templates"))


# --------------------------------------------------------------------------
# Presentation helpers
# --------------------------------------------------------------------------

FIRE_WORDS = ("fire", "ems", "rescue", "medic", "hospital", "lcfr")
LAW_WORDS = ("law", "police", "sheriff", "lcso", "patrol", "corrections")


def kind_of(grp: str | None, tag: str | None) -> str:
    blob = f"{grp or ''} {tag or ''}".lower()
    if any(w in blob for w in FIRE_WORDS):
        return "fire"
    if any(w in blob for w in LAW_WORDS):
        return "law"
    return "other"


def confidence_band(c) -> str:
    """faster-whisper may hand back either a 0..1 score or an avg logprob."""
    if c is None:
        return "unknown"
    try:
        c = float(c)
    except (TypeError, ValueError):
        return "unknown"
    if c < 0:                       # avg logprob
        return "low" if c < -0.9 else "ok"
    return "low" if c < 0.55 else "ok"


def decorate(row: dict) -> dict:
    row["kind"] = kind_of(row.get("talkgroup_grp"), row.get("talkgroup_tag"))
    row["label"] = row.get("talkgroup_tag") or f"TG {row.get('talkgroup') or '?'}"
    dur = row.get("call_length")
    dur = float(dur) if dur is not None else None
    row["duration_f"] = dur
    # 45s is a long transmission; anything past that pins the bar.
    row["bar_pct"] = min(100, max(4, round((dur or 0) / 45 * 100))) if dur else 4
    row["conf_band"] = confidence_band(row.get("confidence"))

    ts = row.get("sort_time") or row.get("call_start")
    if isinstance(ts, datetime):
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        local = ts.astimezone()
        row["ts_iso"] = ts.isoformat()
        row["ts_short"] = local.strftime("%H:%M:%S")
        row["ts_day"] = local.strftime("%a %b %-d")
    else:
        row["ts_iso"] = row["ts_short"] = row["ts_day"] = ""
    return row


def highlight(value) -> Markup:
    """Escape first, then turn ts_headline markers into <mark>."""
    if value is None:
        return Markup("")
    escaped = html.escape(str(value))
    return Markup(escaped.replace(HL_START, "<mark>").replace(HL_STOP, "</mark>"))


templates.env.filters["highlight"] = highlight


# --------------------------------------------------------------------------
# Talkgroup roster, cached from the data itself
# --------------------------------------------------------------------------

_roster: list[dict] = []
_roster_at = 0.0


async def roster(max_age=600) -> list[dict]:
    global _roster, _roster_at
    if _roster and time.monotonic() - _roster_at < max_age:
        return _roster
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT talkgroup, "
                "       max(talkgroup_tag) AS tag, "
                "       max(talkgroup_grp) AS grp, "
                "       count(*) AS n "
                "FROM transcripts "
                "WHERE talkgroup IS NOT NULL "
                "GROUP BY talkgroup ORDER BY n DESC"
            )
            rows = await cur.fetchall()
    for r in rows:
        r["kind"] = kind_of(r["grp"], r["tag"])
        r["label"] = r["tag"] or f"TG {r['talkgroup']}"
    _roster, _roster_at = rows, time.monotonic()
    return rows


# --------------------------------------------------------------------------
# Call log query
# --------------------------------------------------------------------------

async def fetch_calls(q=None, tg=None, hours=None, kind=None,
                      before_ts=None, before_id=None, limit=PAGE) -> list[dict]:
    where = ["TRUE"]
    params: list = []

    if q:
        where.append("to_tsvector('english', t.text) @@ websearch_to_tsquery('english', %s)")
        params.append(q)
    if tg:
        where.append("t.talkgroup = %s")
        params.append(int(tg))
    if hours:
        where.append(f"{CT} >= %s")
        params.append(datetime.now(timezone.utc) - timedelta(hours=int(hours)))
    if kind in ("fire", "law"):
        words = FIRE_WORDS if kind == "fire" else LAW_WORDS
        pat = [f"%{w}%" for w in words]
        where.append("(t.talkgroup_grp ILIKE ANY(%s) OR t.talkgroup_tag ILIKE ANY(%s))")
        params += [pat, pat]
    if before_ts and before_id:
        where.append(f"({CT}, t.id) < (%s::timestamptz, %s)")
        params += [before_ts, int(before_id)]

    # Highlight only when searching; ts_headline is expensive.
    text_expr = ("ts_headline('english', t.text, websearch_to_tsquery('english', %s), %s)"
                 if q else "t.text")
    head_params = ([q, f"StartSel={HL_START},StopSel={HL_STOP},"
                       "HighlightAll=TRUE,MaxFragments=0"] if q else [])

    sql = f"""
        SELECT t.id, t.job_id, t.talkgroup, t.talkgroup_tag, t.talkgroup_grp,
               t.call_start, t.call_length, t.confidence, t.language,
               {text_expr} AS text,
               {CT} AS sort_time,
               j.audio_path, j.state
        FROM transcripts t
        JOIN jobs j ON j.id = t.job_id
        WHERE {' AND '.join(where)}
        ORDER BY {CT} DESC, t.id DESC
        LIMIT %s
    """
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql, head_params + params + [limit])
            rows = await cur.fetchall()
    return [decorate(r) for r in rows]


async def system_status() -> dict:
    out: dict = {}
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT state, count(*) AS n FROM jobs GROUP BY state "
                "ORDER BY array_position(ARRAY['running','pending','failed','done'], state)"
            )
            out["states"] = await cur.fetchall()
            out["backlog"] = sum(r["n"] for r in out["states"]
                                 if r["state"] in ("pending", "running"))
            out["failed"] = sum(r["n"] for r in out["states"] if r["state"] == "failed")
            out["total_calls"] = sum(r["n"] for r in out["states"])

            await cur.execute("SELECT count(*) AS n FROM transcripts")
            out["total_transcripts"] = (await cur.fetchone())["n"]

            await cur.execute(f"SELECT max({CT}) AS last FROM transcripts t")
            last = (await cur.fetchone())["last"]
            if isinstance(last, datetime):
                if last.tzinfo is None:
                    last = last.replace(tzinfo=timezone.utc)
                out["seconds_since"] = int((datetime.now(timezone.utc) - last).total_seconds())

            await cur.execute(
                f"SELECT count(*) AS n FROM transcripts t "
                f"WHERE {CT} >= now() - interval '24 hours'"
            )
            out["calls_24h"] = (await cur.fetchone())["n"]
    return out


# --------------------------------------------------------------------------
# Audio
# --------------------------------------------------------------------------

def resolve_audio(stored: str | None) -> Path | None:
    if not stored:
        return None
    p = Path(stored)
    base = p if p.is_absolute() else AUDIO_ROOT / p
    for c in [base] + [base.with_suffix(e) for e in (".m4a", ".wav", ".mp3")]:
        try:
            rc = c.resolve()
        except OSError:
            continue
        if (rc == AUDIO_ROOT or AUDIO_ROOT in rc.parents) and rc.is_file():
            return rc
    return None


RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")
MEDIA = {".m4a": "audio/mp4", ".wav": "audio/wav", ".mp3": "audio/mpeg"}


def ranged(path: Path, range_header: str | None) -> Response:
    media = MEDIA.get(path.suffix.lower(), "application/octet-stream")
    headers = {"Accept-Ranges": "bytes", "Cache-Control": "private, max-age=3600"}
    match = RANGE_RE.match(range_header or "")
    if not match:
        return FileResponse(path, media_type=media, headers=headers)

    size = path.stat().st_size
    start_s, end_s = match.groups()
    if start_s:
        start = int(start_s)
        end = min(int(end_s), size - 1) if end_s else size - 1
    else:                                   # suffix range: last N bytes
        start = max(0, size - int(end_s or 0))
        end = size - 1
    if start > end or start >= size:
        return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})

    with path.open("rb") as fh:
        fh.seek(start)
        chunk = fh.read(end - start + 1)
    headers |= {"Content-Range": f"bytes {start}-{end}/{size}",
                "Content-Length": str(len(chunk))}
    return Response(chunk, status_code=206, media_type=media, headers=headers)


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

@app.on_event("startup")
async def startup():
    global pool
    pool = AsyncConnectionPool(DSN, min_size=1, max_size=8, open=False)
    await pool.open(wait=True, timeout=15)


@app.on_event("shutdown")
async def shutdown():
    if pool:
        await pool.close()


@app.get("/", response_class=HTMLResponse)
async def index(request: Request, tg: str | None = None):
    return templates.TemplateResponse(
        request, "index.html",
        { "talkgroups": await roster(), "preset_tg": tg or ""},
    )


@app.get("/partials/calls", response_class=HTMLResponse)
async def partial_calls(
    request: Request,
    q: str | None = None,
    tg: str | None = None,
    kind: str | None = None,
    hours: int | None = Query(None),
    before_ts: str | None = None,
    before_id: int | None = None,
):
    try:
        calls = await fetch_calls(q=q, tg=tg, hours=hours, kind=kind,
                                  before_ts=before_ts, before_id=before_id)
    except Exception as exc:                       # malformed query, bad tg, etc.
        return templates.TemplateResponse(
            request, "_error.html", {"message": str(exc).split("\n")[0]}
        )
    last = calls[-1] if calls else None
    return templates.TemplateResponse(
        request, "_calls.html",
        {"calls": calls, "limit": PAGE,
         "q": q or "", "tg": tg or "", "kind": kind or "", "hours": hours or "",
         "next_ts": last["sort_time"].isoformat() if last and last.get("sort_time") else "",
         "next_id": last["id"] if last else "",
         "first_page": not before_id},
    )


@app.get("/partials/live", response_class=HTMLResponse)
async def partial_live(request: Request):
    return templates.TemplateResponse(
        request, "_live.html",
        {"calls": await fetch_calls(limit=12),
         "status": await system_status()},
    )


@app.get("/stats", response_class=HTMLResponse)
async def stats(request: Request, hours: int = 24):
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""SELECT t.talkgroup,
                           max(t.talkgroup_tag) AS talkgroup_tag,
                           max(t.talkgroup_grp) AS talkgroup_grp,
                           count(*) AS n,
                           COALESCE(sum(t.call_length), 0) AS airtime
                    FROM transcripts t WHERE {CT} >= %s
                    GROUP BY t.talkgroup ORDER BY n DESC LIMIT 25""",
                (since,),
            )
            by_tg = await cur.fetchall()
            await cur.execute(
                f"""SELECT date_trunc('hour', {CT}) AS bucket, count(*) AS n
                    FROM transcripts t WHERE {CT} >= %s GROUP BY 1 ORDER BY 1""",
                (since,),
            )
            by_hour = await cur.fetchall()

    for r in by_tg:
        r["kind"] = kind_of(r["talkgroup_grp"], r["talkgroup_tag"])
        r["label"] = r["talkgroup_tag"] or f"TG {r['talkgroup']}"
    return templates.TemplateResponse(
        request, "stats.html",
        {"by_tg": by_tg, "by_hour": by_hour,
         "peak": max([r["n"] for r in by_hour], default=1) or 1,
         "top": max([r["n"] for r in by_tg], default=1) or 1,
         "hours": hours, "status": await system_status()},
    )


@app.get("/audio/{transcript_id}")
async def audio(transcript_id: int, request: Request):
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT j.audio_path FROM transcripts t "
                "JOIN jobs j ON j.id = t.job_id WHERE t.id = %s",
                (transcript_id,),
            )
            row = await cur.fetchone()
    if not row:
        raise HTTPException(404, "No such call.")
    path = resolve_audio(row["audio_path"])
    if not path:
        raise HTTPException(404, "Audio file is missing from the archive.")
    return ranged(path, request.headers.get("range"))


@app.get("/healthz")
async def healthz():
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT 1")
    return {"ok": True}
