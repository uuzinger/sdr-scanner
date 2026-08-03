# Scanner Transcription Database — Installation Guide

**Host:** `db-host` (dedicated VM)
**OS:** Ubuntu 24.04 LTS
**Postgres version:** 18 (current stable, 18.4 as of May 2026)
**Extensions:** `pg_trgm`, `btree_gin`, `pgcrypto`, `pgvector`

---

## 1. VM prerequisites

```bash
# Update the base system
sudo apt-get update && sudo apt-get upgrade -y

# Set the hostname
sudo hostnamectl set-hostname db-host

# Install utilities used during setup
sudo apt-get install -y curl ca-certificates gnupg lsb-release \
  nfs-common htop sysstat
```

### 1.1 NFS mount

The dashboard will serve audio files directly from this host. Mount the NAS export
at the same path used on all other hosts so `audio_path` values in the database
resolve without translation.

```bash
sudo mkdir -p /vol1/sdr-scanner

# Add to /etc/fstab for persistent mount
echo "nas:/vol1/sdr-scanner  /vol1/sdr-scanner  nfs  \
  rsize=131072,wsize=131072,hard,intr,noatime,nfsvers=4  0  0" \
  | sudo tee -a /etc/fstab

sudo mount -a

# Verify
df -h /vol1/sdr-scanner
```

Tune `rsize`/`wsize` down to `65536` if your NAS does not support 128K I/O.

---

## 2. Install PostgreSQL 18

Ubuntu 24.04 ships Postgres 16. Use the official PGDG repository for 18.

```bash
# Add the PGDG signing key and repository
sudo install -d /usr/share/postgresql-common/pgdg
sudo curl -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
  --fail https://www.postgresql.org/media/keys/ACCC4CF8.asc

sudo sh -c 'echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] \
  https://apt.postgresql.org/pub/repos/apt $(lsb_release -cs)-pgdg main" \
  > /etc/apt/sources.list.d/pgdg.list'

sudo apt-get update
sudo apt-get install -y postgresql-18 postgresql-client-18 postgresql-server-dev-18

# Verify
psql --version
# postgresql 18.x
```

The installer creates the `postgres` system user and starts the `postgresql@18-main`
service automatically. Data directory is `/var/lib/postgresql/18/main`.

**Postgres 18 notable changes relevant to this deployment:**

- **Asynchronous I/O (AIO)** — sequential scans and bitmap heap scans (both common
  in FTS queries) are significantly faster. No configuration change needed; it is
  enabled by default.
- **Data checksums on by default** — `initdb` now enables checksums automatically.
  This is desirable; leave it as-is. It means any future `pg_upgrade` from this
  cluster requires the destination cluster to also have checksums enabled.
- **`uuidv7()`** — native UUID v7 generation available if needed for future tables.

---

## 3. Install extensions

### 3.1 Bundled extensions

These ship with the Postgres packages and need only be enabled in the database (§6).
No additional install step required:

- `pg_trgm` — trigram similarity; used by the reconciliation sweep and fuzzy matching
- `btree_gin` — GIN support for scalar types; used on composite indexes
- `pgcrypto` — `gen_random_uuid()` and SHA256 helpers

### 3.2 pgvector

Required for phase-9 semantic search. Install now so the extension is available when
that phase begins; it will not consume resources until you create a vector column.

```bash
# Build from source — the apt package lags releases
sudo apt-get install -y build-essential git

git clone --branch v0.8.0 https://github.com/pgvector/pgvector.git
cd pgvector
make
sudo make install
cd .. && rm -rf pgvector
```

Verify the shared object installed correctly:

```bash
ls /usr/lib/postgresql/18/lib/vector.so
```

---

## 4. Configure PostgreSQL

### 4.1 postgresql.conf

Edit `/etc/postgresql/18/main/postgresql.conf`. The values below assume the
recommended 8GB RAM / 4 vCPU VM from the architecture document.

```ini
# ── Connections ────────────────────────────────────────────────────
listen_addresses         = 'localhost,db-host'   # replace db-host with the real IP or hostname
max_connections          = 50                    # worker(1) + dashboard(~10) + admin headroom

# ── Memory ─────────────────────────────────────────────────────────
shared_buffers           = 2GB        # 25% of RAM; GIN indexes cache here
effective_cache_size     = 6GB        # 75% of RAM; planner hint only, not allocated
work_mem                 = 64MB       # per sort/hash node per query; see note below
maintenance_work_mem     = 512MB      # GIN builds, VACUUM, HNSW index creation
huge_pages               = try

# ── WAL and checkpoints ────────────────────────────────────────────
wal_buffers                  = 16MB
checkpoint_completion_target = 0.9
max_wal_size                 = 2GB
min_wal_size                 = 256MB

# ── Planner ────────────────────────────────────────────────────────
random_page_cost         = 1.1        # SSD/NVMe-backed local disk
effective_io_concurrency = 200        # SSD; set to 2 if rotational

# ── Logging ────────────────────────────────────────────────────────
log_min_duration_statement = 500      # log queries > 500ms; catches slow dashboard queries
log_line_prefix            = '%t [%p] %u@%d '
logging_collector          = on
log_directory              = 'pg_log'

# ── Autovacuum ─────────────────────────────────────────────────────
autovacuum_vacuum_scale_factor   = 0.05   # more aggressive for append-heavy tables
autovacuum_analyze_scale_factor  = 0.02
```

**`work_mem` note.** 64MB is conservative by design. It applies per sort node per
active query — a complex dashboard query with three sort nodes running across ten
connections can briefly consume 64MB × 3 × 10 = 1.9GB. Monitor with
`EXPLAIN (ANALYZE, BUFFERS)` and raise only if you see `Sort Method: external merge`
in query plans.

Reload after edits (no restart needed for most parameters):

```bash
sudo systemctl reload postgresql@18-main
# For parameters requiring restart (e.g. shared_buffers, huge_pages):
sudo systemctl restart postgresql@18-main
```

### 4.2 pg_hba.conf

Edit `/etc/postgresql/18/main/pg_hba.conf`. Add entries for the hosts that connect:

```
# TYPE  DATABASE  USER     ADDRESS              METHOD
# Local admin access
local   all       postgres                      peer

# Application connections — scoped to the scanner database and user only
host    scanner   scanner  <MicroPC-IP>/32       scram-sha-256
host    scanner   scanner  <gpu-host-IP>/32      scram-sha-256
host    scanner   scanner  127.0.0.1/32          scram-sha-256

# Dashboard read-only user
host    scanner   scanner_ro  <db-host-IP>/32    scram-sha-256
host    scanner   scanner_ro  127.0.0.1/32       scram-sha-256

# Admin access from your management workstation
host    all       postgres  <admin-IP>/32         scram-sha-256
```

Replace `<MicroPC-IP>`, `<gpu-host-IP>`, `<db-host-IP>`, and `<admin-IP>` with real
addresses. Reload after changes:

```bash
sudo systemctl reload postgresql@18-main
```

---

## 5. Create roles and database

```bash
sudo -u postgres psql <<'SQL'

-- Application user (read/write)
CREATE ROLE scanner WITH LOGIN PASSWORD 'CHANGE_ME_scanner';

-- Read-only user for dashboard queries
CREATE ROLE scanner_ro WITH LOGIN PASSWORD 'CHANGE_ME_scanner_ro';

-- Database
CREATE DATABASE scanner
  OWNER scanner
  ENCODING 'UTF8'
  LC_COLLATE 'en_US.UTF-8'
  LC_CTYPE 'en_US.UTF-8'
  TEMPLATE template0;

COMMENT ON DATABASE scanner IS 'Loudoun County P25 scanner transcription corpus';

SQL
```

---

## 6. Enable extensions

Connect as the `scanner` user and enable extensions inside the database:

```bash
sudo -u postgres psql -d scanner <<'SQL'

CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS btree_gin;
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS vector;   -- pgvector; no-op until a vector column is added

SQL
```

Verify:

```sql
SELECT extname, extversion FROM pg_extension ORDER BY extname;
--  btree_gin | 1.3
--  pg_trgm   | 1.6
--  pgcrypto  | 1.3
--  plpgsql   | 1.0
--  vector    | 0.8.0
```

---

## 7. Schema

Run this as the `scanner` user against the `scanner` database.

### 7.1 Job queue

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

CREATE INDEX jobs_pending_idx ON jobs (enqueued_at)
  WHERE state = 'pending';

CREATE INDEX jobs_state_idx ON jobs (state, finished_at DESC);

COMMENT ON TABLE jobs IS 'Enqueue hook inserts here; worker claims rows with SKIP LOCKED';
COMMENT ON COLUMN jobs.state IS 'pending | running | done | skipped | failed';
```

### 7.2 Calls (primary corpus)

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
  src_list        jsonb,          -- unit IDs + per-transmission time offsets
  freq_list       jsonb,          -- per-transmission freq + error rate
  audio_path      text,           -- absolute path on the NFS mount
  audio_sha256    text UNIQUE,    -- deduplication safety net
  transcript      text,
  segments        jsonb,          -- per-segment logprob, no_speech_prob, timings
  avg_logprob     real,
  model           text,
  prompt_version  int,            -- increment when DOMAIN_PROMPT changes
  transcribed_at  timestamptz,
  suspect         boolean DEFAULT false,
  alerted         boolean DEFAULT false,
  tsv tsvector GENERATED ALWAYS AS (
        to_tsvector('english', coalesce(transcript, ''))) STORED
);

-- Full-text search
CREATE INDEX calls_tsv_idx ON calls USING gin (tsv);

-- Primary access patterns
CREATE INDEX calls_tg_time_idx  ON calls (talkgroup, call_start DESC);
CREATE INDEX calls_time_idx     ON calls (call_start DESC);

-- Unit ID lookups within src_list JSONB
CREATE INDEX calls_src_idx ON calls USING gin (src_list jsonb_path_ops);

-- Alerter partial index — only unprocessed non-suspect calls
CREATE INDEX calls_alert_idx ON calls (call_start)
  WHERE NOT alerted AND NOT suspect;

COMMENT ON COLUMN calls.audio_path    IS 'Absolute path, valid on any host with /vol1/sdr-scanner mounted';
COMMENT ON COLUMN calls.prompt_version IS 'Matches prompt_versions.id; identifies which DOMAIN_PROMPT produced this transcript';
COMMENT ON COLUMN calls.suspect        IS 'True if avg_logprob < -1.0, no_speech_prob high, or hallucination detected';
COMMENT ON COLUMN calls.alerted        IS 'True once the gazetteer matcher has processed this call';
```

### 7.3 Reference tables

```sql
-- Loaded from the RadioReference CSV used by trunk-recorder
CREATE TABLE talkgroups (
  talkgroup   int PRIMARY KEY,
  alpha_tag   text,
  description text,
  tag         text,
  category    text,
  is_data     boolean DEFAULT false
);

COMMENT ON COLUMN talkgroups.is_data IS 'True = exclude from transcription (data/telemetry talkgroups)';

-- Unit IDs learned from unitTagsFile and audio content over time
CREATE TABLE units (
  unit_id   bigint PRIMARY KEY,
  alias     text,
  agency    text,
  confirmed boolean DEFAULT false,
  last_seen timestamptz
);

-- Prompt version tracking — increment when DOMAIN_PROMPT changes
CREATE TABLE prompt_versions (
  id          serial PRIMARY KEY,
  introduced  timestamptz NOT NULL DEFAULT now(),
  description text,
  prompt_text text NOT NULL
);
```

### 7.4 Alerting tables

```sql
CREATE TABLE gazetteer (
  id       serial PRIMARY KEY,
  term     text NOT NULL,
  kind     text NOT NULL CHECK (kind IN ('street', 'subdivision', 'landmark')),
  priority int DEFAULT 1
);

CREATE INDEX gazetteer_term_idx ON gazetteer USING gin (to_tsvector('english', term));

CREATE TABLE alert_keywords (
  id       serial PRIMARY KEY,
  term     text NOT NULL,        -- 'structure fire', 'shots fired', 'entrapment'
  priority int DEFAULT 1
);

-- Alert history: one row per call+term combination that triggered a notification
CREATE TABLE alerts_sent (
  id            bigserial PRIMARY KEY,
  call_id       bigint NOT NULL REFERENCES calls (id),
  matched_term  text NOT NULL,
  sent_at       timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX alerts_sent_term_time_idx ON alerts_sent (matched_term, sent_at DESC);

COMMENT ON TABLE alerts_sent IS 'Used for debounce: suppress repeat alerts on the same term within 20 minutes';
```

### 7.5 Seed the gazetteer

Initial seed for the South Riding / Arcola area. Extend as incidents surface new
relevant terms.

```sql
INSERT INTO gazetteer (term, kind, priority) VALUES
  -- Subdivisions
  ('South Riding',            'subdivision', 2),
  ('Arcola',                  'subdivision', 2),
  ('Brambleton',              'subdivision', 1),
  ('Stone Ridge',             'subdivision', 1),
  ('Willowsford',             'subdivision', 1),
  ('Broadlands',              'subdivision', 1),
  -- Streets
  ('Braddock Road',           'street',      1),
  ('Gum Spring Road',         'street',      1),
  ('Belmont Ridge Road',      'street',      1),
  ('Loudoun County Parkway',  'street',      1),
  ('Ryan Road',               'street',      1),
  ('Evergreen Mills Road',    'street',      1),
  ('Waxpool Road',            'street',      1),
  ('Route 50',                'street',      1),
  ('Tall Cedars Parkway',     'street',      1),
  -- Landmarks
  ('South Riding Fire',       'landmark',    2),
  ('Station 18',              'landmark',    2);
```

### 7.6 Seed known data talkgroups

```sql
INSERT INTO talkgroups (talkgroup, alpha_tag, is_data) VALUES
  (5010, 'LCSO Data', true);
-- Add additional data/telemetry talkgroups as identified
```

---

## 8. Roles and grants

```bash
sudo -u postgres psql -d scanner <<'SQL'

-- scanner user owns all objects; grant full access explicitly
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO scanner;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO scanner;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO scanner;

-- Default privileges for objects scanner creates in the future
ALTER DEFAULT PRIVILEGES FOR ROLE scanner IN SCHEMA public
  GRANT ALL ON TABLES TO scanner;
ALTER DEFAULT PRIVILEGES FOR ROLE scanner IN SCHEMA public
  GRANT ALL ON SEQUENCES TO scanner;

-- Read-only role for dashboard
GRANT CONNECT ON DATABASE scanner TO scanner_ro;
GRANT USAGE ON SCHEMA public TO scanner_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO scanner_ro;

ALTER DEFAULT PRIVILEGES FOR ROLE scanner IN SCHEMA public
  GRANT SELECT ON TABLES TO scanner_ro;

SQL
```

---

## 9. Connection string references

Store credentials in environment variables or a secrets manager, never in source code.

| Consumer | Connection string |
|---|---|
| Worker (`gpu-host`) | `postgresql://scanner:PASSWORD@db-host/scanner` |
| Enqueue hook (`MicroPC`) | `host=db-host user=scanner dbname=scanner` (via `PGPASSWORD` env var) |
| Dashboard (read) | `postgresql://scanner_ro:PASSWORD@localhost/scanner` |
| Admin | `sudo -u postgres psql -d scanner` |

---

## 10. Maintenance

### 10.1 Autovacuum

The `calls` and `jobs` tables are append-heavy. The scaled-down autovacuum thresholds
set in `postgresql.conf` (§4.1) keep bloat in check without manual intervention.
Monitor with:

```sql
SELECT relname,
       n_live_tup,
       n_dead_tup,
       round(n_dead_tup::numeric / nullif(n_live_tup + n_dead_tup, 0) * 100, 1) AS dead_pct,
       last_autovacuum,
       last_autoanalyze
FROM pg_stat_user_tables
ORDER BY n_dead_tup DESC;
```

A `dead_pct` above 10% that persists between checks suggests autovacuum cannot keep up;
lower `autovacuum_vacuum_scale_factor` further or run `VACUUM ANALYZE calls` manually.

### 10.2 Index bloat

GIN indexes bloat over time. Rebuild quarterly during a low-traffic window:

```sql
REINDEX INDEX CONCURRENTLY calls_tsv_idx;
REINDEX INDEX CONCURRENTLY calls_src_idx;
```

`CONCURRENTLY` allows reads and writes during the rebuild. It takes longer but does
not lock the table.

### 10.3 Backups

Minimum viable backup: `pg_dump` nightly to the NAS.

```bash
# /opt/scanner/backup-db.sh — run via cron as postgres user
#!/usr/bin/env bash
set -euo pipefail
DEST="/vol1/sdr-scanner/backups/postgres"
mkdir -p "$DEST"
FNAME="scanner-$(date +%Y%m%d).dump"
pg_dump -Fc -d scanner -f "$DEST/$FNAME"
# Retain 30 daily backups
find "$DEST" -name "scanner-*.dump" -mtime +30 -delete
```

```
# crontab -u postgres -e
0 3 * * * /opt/scanner/backup-db.sh >> /var/log/scanner/backup.log 2>&1
```

Restore:

```bash
pg_restore -d scanner -j 4 /vol1/sdr-scanner/backups/postgres/scanner-YYYYMMDD.dump
```

---

## 11. Verification checklist

Run these after completing the install to confirm the environment is correct before
connecting the worker.

```sql
-- 1. Extensions present
SELECT extname, extversion FROM pg_extension ORDER BY extname;

-- 2. All tables created
SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename;
-- Expected: alert_keywords, alerts_sent, calls, gazetteer, jobs,
--           prompt_versions, talkgroups, units

-- 3. All indexes present
SELECT indexname, tablename FROM pg_indexes
WHERE schemaname = 'public' ORDER BY tablename, indexname;

-- 4. Role grants correct
\dp calls
\dp jobs

-- 5. scanner_ro cannot write
SET ROLE scanner_ro;
INSERT INTO calls (talkgroup, call_start) VALUES (1, now());
-- Should return: ERROR:  permission denied for table calls
RESET ROLE;

-- 6. FTS working
INSERT INTO calls (talkgroup, call_start, transcript)
  VALUES (9999, now(), 'Engine 606 on scene structure fire Braddock Road');
SELECT transcript FROM calls
  WHERE tsv @@ websearch_to_tsquery('english', 'structure fire "Braddock Road"');
-- Should return the test row
DELETE FROM calls WHERE talkgroup = 9999;

-- 7. NFS path reachable
COPY (SELECT 1) TO '/vol1/sdr-scanner/.pg-write-test';
-- Should succeed (no error)
\! rm /vol1/sdr-scanner/.pg-write-test
```

---

## 12. pgvector activation (phase 9)

When phase 9 begins, add the vector column and HNSW index to `calls`:

```sql
-- Add the embedding column (1536 dimensions; adjust to match your model)
ALTER TABLE calls ADD COLUMN embedding vector(1536);

-- Build the HNSW index (schedule during off-hours; takes 2–5 min per 100k rows)
CREATE INDEX CONCURRENTLY calls_embedding_idx
  ON calls USING hnsw (embedding vector_cosine_ops)
  WITH (m = 16, ef_construction = 64);

-- Add the parallel worker setting to postgresql.conf at the same time:
-- max_parallel_workers_per_gather = 2
```

The extension is already installed (§6); no package changes are needed at this point.

Similarity search:

```sql
-- Nearest 10 calls by semantic similarity to a query embedding
SELECT id, call_start, talkgroup_tag, transcript,
       1 - (embedding <=> $1::vector) AS similarity
FROM calls
WHERE NOT suspect
ORDER BY embedding <=> $1::vector
LIMIT 10;
```
