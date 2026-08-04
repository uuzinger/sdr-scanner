# sdr-scanner — AI Feature Roadmap

Status as of August 2026: capture, transcription, and the web interface are complete.
Stages 1–4 (trunk-recorder capture → `enqueue.sh` → Postgres jobs queue → gpu-host
Whisper worker → `transcripts`) are in production, and the MicroPC-hosted web
interface covers transcript search, inline audio playback, live feed, and talkgroup
stats.

This document covers the next phase: the AI/LLM layer built on top of `transcripts`.

---

## Architectural principles

These constraints apply to every stage below.

1. **Enrichment is a separate service with its own queue.** Do not extend
   `worker.py`. Whisper transcription and LLM enrichment have different failure
   modes, different models, and different iteration speeds. They get separate
   systemd units and separate claim loops.
2. **Every AI output is reprocessable.** Store `model` and `prompt_version` on every
   generated row. Prompts will change dozens of times; history must be re-runnable
   without touching the transcription path.
3. **The LLM extracts; SQL decides.** Models populate fields. Deterministic rules
   evaluate those fields to fire alerts. A model that silently declines to alert is
   undebuggable.
4. **Every generated field is inspectable in the UI** next to its source audio and
   source transcript. Failure attribution — misheard audio vs. hallucinated
   extraction vs. bad geocode — must take one click.
5. **Queue claims use `SKIP LOCKED`**, matching the existing `jobs` table pattern.
6. **Column conventions follow the existing schema**: `state`, `started_at`,
   `finished_at`.

---

## Stage 5 — Structured extraction

Convert free-text transcripts into queryable fields. This is the prerequisite for
everything downstream.

### Output shape

```json
{
  "incident_type": "structure_fire",
  "address": "25100 block of Riding Center Dr",
  "cross_streets": ["Braddock Rd"],
  "units": ["Engine 606", "Medic 6"],
  "priority": "emergency",
  "is_dispatch": true,
  "confidence": 0.8
}
```

### Schema

New table `enrichments`:

| column | type | notes |
|---|---|---|
| `id` | bigserial | |
| `transcript_id` | bigint FK | |
| `model` | text | e.g. `qwen3.6-8b` |
| `prompt_version` | text | semver or git short SHA |
| `payload` | jsonb | the extracted object |
| `state` | text | `pending` / `running` / `done` / `failed` |
| `started_at` / `finished_at` | timestamptz | |

Index `payload` with a GIN index. Expose `incident_type`, `priority`, and `address`
as generated columns for cheap filtering.

### Model and serving

- Run a small model — 4B to 8B — in a dedicated llama-swap slot. Extraction is a
  formatting task, not a reasoning task, and it must not contend with the
  production Qwen3.6-35B-A3B slots on `gpu.zinger.org`.
- Constrain output with llama.cpp's `response_format: json_schema` or a GBNF
  grammar. Free-form JSON parsing fails unattended.
- Enumerate `incident_type` in the schema. An open string field produces forty
  spellings of "vehicle accident."

### Tasks

- [ ] Define the JSON schema and `incident_type` enum
- [ ] Create `enrichments` table and indexes
- [ ] Stand up the extraction model slot in llama-swap
- [ ] Write `enricher.py` + `scanner-enricher.service`
- [ ] Add an enrichment panel to the web UI showing payload beside the transcript
- [ ] Backfill against existing transcripts

---

## Stage 5.5 — ASR vocabulary tuning

Highest value-per-hour item in this document. Whisper has never encountered Loudoun
County street names or LCFR unit designators, and a hallucinated street name
corrupts every stage after it.

- [ ] Build an `initial_prompt` from the gazetteer: local place names (Ashburn,
      Sterling, South Riding, Aldie, Brambleton), major roads (Braddock, Route 50,
      Loudoun County Pkwy, Belmont Ridge), unit designators, and common radio
      phrasing
- [ ] Pass it through `faster-whisper` in `worker.py`
- [ ] Complete `loudoun-units.csv` (previously deferred) — it now has direct value
      as vocabulary input and as an extraction validation list
- [ ] Spot-check a fixed sample of calls before/after to confirm improvement

---

## Stage 6 — Call clustering into incidents

A single event produces 20+ calls across several minutes on one talkgroup.
Per-call alerting means twenty notifications for one fire.

- Group calls by `(talkgroup, gap < ~90s)` into an `incidents` table.
- Run extraction and summarization once over the concatenated cluster rather than
  per call.
- Keep incidents open while calls continue arriving; regenerate a rolling summary
  as new calls land. This is where a larger model is justified.
- Timeline view in the UI: incident header, rolling summary, per-call transcripts
  and audio beneath it.

### Tasks

- [ ] `incidents` table + clustering job
- [ ] Cluster-level extraction and summarization prompt
- [ ] Rolling-summary regeneration on new call arrival
- [ ] Incident timeline view in the web UI

---

## Stage 7 — Geocoding and proximity alerting

The original goal of the project. Requires almost no AI.

1. Feed the extracted address and cross streets to a geocoder. Options: the Census
   Bureau geocoder (free, good Virginia coverage) or a local Nominatim container
   (no external dependency, preferred long-term).
2. Store `lat`, `lon`, and `geocode_confidence` on the incident.
3. Compute distance from 25785 Spectacular Run Pl.
4. Evaluate deterministic rules, e.g.
   `distance_mi < 1.5 AND priority = 'emergency'`.
5. Dispatch notification — ntfy, Pushover, or Home Assistant, which is already
   running.

Handle the "block of" and cross-street-only cases explicitly; scanner traffic rarely
gives clean street addresses.

### Tasks

- [ ] Stand up the geocoder
- [ ] Geocoding stage with confidence scoring and address normalization
- [ ] Rules table — distance radius, incident types, talkgroups, quiet hours
- [ ] Notification dispatch + dedupe per incident
- [ ] Alert history view with fired/suppressed reasoning

---

## Stage 8 — Semantic search and Q&A

Once 5–7 are stable.

- [ ] `pgvector` extension; embed transcripts and incident summaries
- [ ] Hybrid retrieval — Postgres full-text search combined with vector similarity
- [ ] Chat pane in the web UI over the retrieval layer
- [ ] Text-to-SQL path for aggregate questions ("how many EMS calls to the Route 50
      corridor last month") alongside RAG for narrative questions ("what happened on
      Braddock Rd last Tuesday night")
- [ ] Constrain generated SQL to a read-only role against a set of views

---

## Later candidates

- **Anomaly detection.** Baseline call volume per talkgroup per hour; flag
  statistical deviation. Catches large events without any keyword list.
- **Daily digest.** Morning summary of overnight activity within the alert radius.
- **Speaker/unit attribution.** Map voices or unit IDs to responding apparatus.
- **Multi-site expansion.** Additional trunk-recorder sites feeding the same queue.
- **Cross-source correlation.** VDOT incident feeds, weather, power outage maps
  joined against incident timestamps and locations.
