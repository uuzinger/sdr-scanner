# scanner-web

Browser front end for the Loudoun P25 archive. Runs on the MicroPC
(192.168.1.37), reads `jobs`/`transcripts` on pgsql (192.168.1.28),
serves audio off the NFS mount.

FastAPI + HTMX + Jinja. One process, no build step, no node_modules —
trunk-recorder keeps priority on that box.

## Install

### 1. Create the directory and copy files

```bash
sudo mkdir -p /opt/scanner-web
sudo chown zinger:zinger /opt/scanner-web   # run as your own user, not a separate scanner account
```

Copy the files over preserving directory structure — `static/` and `templates/`
must exist as subdirectories of `/opt/scanner-web`, not alongside it:

```
/opt/scanner-web/
  app.py
  requirements.txt
  scanner-web.service
  .env
  static/
    app.css
  templates/
    base.html
    index.html
    stats.html
    _calls.html
    _live.html
    _error.html
```

### 2. Python environment

```bash
cd /opt/scanner-web
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### 3. Configure

```bash
cp .env.example .env && nano .env
```

Set at minimum:

```
SCANNER_DSN=postgresql://scanner_web:YOURPASSWORD@192.168.1.28:5432/scanner
AUDIO_ROOT=/vol1/sdr-scanner/audio
```

### 4. Postgres role (run on pgsql as superuser, connected to the `scanner` database)

The grants must be run while connected to `scanner`, not `postgres`:

```bash
psql -d scanner   # or \c scanner if already in psql
```

```sql
CREATE ROLE scanner_web LOGIN PASSWORD 'YOURPASSWORD';
GRANT CONNECT ON DATABASE scanner TO scanner_web;
GRANT USAGE ON SCHEMA public TO scanner_web;
GRANT SELECT ON jobs, transcripts TO scanner_web;
```

### 5. Allow the MicroPC to connect (run on pgsql)

Add a line to `/etc/postgresql/*/main/pg_hba.conf` — place it after the
local socket entries, before any reject rules:

```
host    scanner         scanner_web     192.168.1.37/32         scram-sha-256
```

Then reload Postgres (no restart needed):

```bash
sudo systemctl reload postgresql
```

If you add other hosts that need to connect (gpu-host, etc.), use a subnet
instead of a single IP:

```
host    scanner         scanner_web     192.168.1.0/24          scram-sha-256
```

### 6. systemd service

```bash
sudo cp /opt/scanner-web/scanner-web.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now scanner-web
```

Check it started cleanly:

```bash
sudo systemctl status scanner-web
sudo journalctl -u scanner-web -n 30 --no-pager
```

Then `http://192.168.1.37:8080/`.

## Indexes to add first

You already have `transcripts_text_idx` (GIN) and `transcripts_tg_idx`. The log
sorts by call time, and nothing you have covers that:

```sql
CREATE INDEX transcripts_ct_idx
    ON transcripts ((COALESCE(call_start, created_at)) DESC);
CREATE INDEX transcripts_tg_ct_idx
    ON transcripts (talkgroup, (COALESCE(call_start, created_at)) DESC);
```

The `COALESCE` matches the app's sort key exactly — `call_start` is what you
want to sort by, `created_at` is the NOT NULL backstop for any row where the
worker didn't populate it. A plain index on `call_start` would not be used.
Verified with `EXPLAIN`: the first index drives the log, the second earns its
keep once one talkgroup's traffic is a small fraction of the table.

Paging is keyset (`WHERE (sort_key, id) < (…)`), not `OFFSET`, so deep
scrolling stays flat and new calls arriving mid-scroll can't shift rows into
or out of pages you've already seen.

## Searching

The search box goes through `websearch_to_tsquery`, so it uses the GIN index
you already built and takes the syntax you'd expect:

| you type | you get |
|---|---|
| `structure fire` | calls containing both words |
| `"structure fire"` | that phrase |
| `medic -drill` | medic, excluding drill |

Matches are highlighted via `ts_headline`. Stemming is on: `responding` matches
`respond`. If you'd rather have literal substring matching (partial words, unit
numbers mid-token), that needs `pg_trgm` and a different index — say the word.

## Audio

`/audio/{transcript_id}` joins to `jobs.audio_path`, resolves it under
`AUDIO_ROOT`, and refuses anything landing outside that root after symlink
resolution. Tested against relative traversal, absolute paths, and a symlink
pointing out of the tree — all 404.

It tries the stored extension first, then `.m4a`/`.wav`/`.mp3`, so rows still
pointing at a pre-compression `.wav` keep playing. Range requests are handled
(including suffix ranges), so seeking works.

If you put nginx in front later, an `alias` on the audio directory will be
faster — but then the path guard becomes yours to write.

## What's here

- **Calls** — search, filter by talkgroup / service / window, click any row to
  play it. Each row carries a bar proportional to call length, so a screenful
  reads as a timeline of air traffic; the bar fills amber as it plays.
- **Live rail** — polls every 5s: time since the last transcript, 24h count,
  job queue by state, and a failed-job warning when `state = 'failed'` appears.
- **Activity** — calls-per-hour histogram and busiest talkgroups by count and
  airtime, over 6h / 24h / 7d. Talkgroup names link back into a filtered log.

Talkgroup labels and colours come from `talkgroup_tag` / `talkgroup_grp` in the
database, so they can't drift from what's actually captured — no CSV to keep in
sync. Red is fire/EMS/hospital, blue is law, grey is everything else; the
keyword lists are `FIRE_WORDS` / `LAW_WORDS` at the top of `app.py`.

Transcripts whose `confidence` is low are dimmed and flagged. The check handles
both a 0–1 score and a raw average logprob — if the flag looks wrong,
`confidence_band()` is the one place to adjust.

## Not done yet

No auth. Bind it to the LAN or put it behind your reverse proxy before it sees
anything wider.
