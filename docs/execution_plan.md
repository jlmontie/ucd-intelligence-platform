# UCD Platform — Execution Plan

*Captured 2026-04-23. Governs how the UCD Intelligent Research Platform and
the Utah Project Intelligence Feed are built and merged.*

---

## Strategy in one paragraph

Build both products in a single monorepo with one canonical schema and one
shared `core/` library (entity resolution, embeddings, probe runner). Do **not**
start the two product tracks in parallel from day zero. Run a 2–3 week
foundation sprint first to harden the shared data model and resolution layer;
then fork into two parallel tracks (UCD corpus → chat agent; Utah feed →
public-data scrapers) that write into the same tables. The merger is then a
non-event — the products share physical tables from the first row of Utah data
ingested.

---

## Part 1 — Repo restructure

### Target layout

```
ucd-platform/
├── db/
│   ├── schema.sql              # single source of truth for all tables
│   └── migrations/             # forward-only migrations once schema stabilizes
├── core/                       # shared across both ingestion tracks
│   ├── resolution/             # firm + project entity resolution
│   ├── embeddings/             # embed.py — populates vector columns
│   ├── probes/                 # probe registry + runner
│   ├── geocode/                # lat/lng enrichment
│   └── db.py                   # connection helpers (from pipeline/db_utils.py)
├── ingest_corpus/              # UCD magazine pipeline (was pipeline/)
│   ├── download_issues.py
│   ├── ingest.py               # segmentation + extraction
│   ├── extract_projects.py     # legacy, kept for reference
│   └── requirements.txt
├── ingest_public/              # Utah project feed scrapers (new)
│   ├── up3.py
│   ├── transparent_utah.py
│   ├── dfcm.py
│   ├── udot_stip.py
│   ├── appropriations.py
│   ├── county_portals/
│   ├── school_districts/
│   └── requirements.txt
├── api/                        # single FastAPI service, both data sources
├── frontend/                   # single Next.js app, both surfaces
├── infra/                      # Terraform — one state, multiple services
├── tests/
│   ├── test_ingestion.py
│   ├── test_resolution.py
│   └── test_schema_contract.py # enforces that core/ stays compatible with both tracks
└── docs/
```

### Migration steps

1. Rename `~/Code/ucd-database/` → `~/Code/ucd-platform/`.
2. `git mv pipeline/ ingest_corpus/`.
3. Extract `pipeline/db_utils.py` → `core/db.py` and update imports.
4. Create empty `core/resolution/`, `core/embeddings/`, `core/probes/`,
   `core/geocode/`, `ingest_public/` scaffolding.
5. Update `infra/` service definitions to reference new paths.
6. Commit the restructure as one atomic change before any new work starts.
7. Optional: rename the GitHub repo to match (already `ucd-platform`).

---

## Part 2 — Foundation sprint (2–3 weeks, sequential)

**Gate:** no `ingest_public/` scraper work begins until this sprint closes. The
Utah feed's value proposition depends on the shared schema being trustworthy.

### 2.1 Schema hardening (`db/schema.sql`)

- Add `projects.source` (`'corpus' | 'public_data' | 'merged'`).
- Add `projects.phase` (`planning | design | approved | bidding | construction | completed`) — distinct from the existing `status`.
- Add `projects.lat`, `projects.lng`, `projects.county`.
- Add `projects.estimated_cost_usd` separate from `projects.cost_usd`
  (corpus is usually final; public data is usually estimate).
- Add `articles.content_hash` to support probe result caching across reprocess.
- Replace `articles.primary_project_id` semantics: add `article_projects`
  (many-to-many, with `is_primary BOOL`) to capture multi-project articles.
- Add `project_sources` join table: `(project_id, source_type, source_ref, confidence, first_seen, last_seen)`.
  Source types: `article`, `up3_solicitation`, `dfcm_listing`, `stip_entry`,
  `appropriation_line`, `recorder_filing`, `planning_agenda`.
- Promote `people` to first-class usage (currently unpopulated).
- Add `owners` and `developers` tables OR keep as firms with a `firm_type` enum —
  pick one and commit. Recommend `firm_type` to avoid table sprawl.

Every schema change gets a migration file so downstream work can pin.

### 2.2 Entity resolution (`core/resolution/`)

- `resolve_firms.py` — LLM pass over `firm_mentions.raw_text`, populates
  `firms.aliases`, sets `firm_mentions.canonical_id` + `confidence`.
  Replace the `upsert_firm` exact-match in `ingest_corpus/ingest.py:254`.
- `resolve_projects.py` — match incoming project records (from either track)
  to existing `projects` rows via `(name_similarity, location, cost_range,
  year)` + LLM tiebreaker. This is the hard one; budget 1 week of the sprint.
- Both resolvers are re-runnable and idempotent.

### 2.3 Probe runner (`core/probes/`)

- `runner.py` — reads `probes`, iterates unseen `(probe_id, article_hash,
  probe_version)`, writes `probe_runs`.
- Decompose the monolithic extraction in `ingest_corpus/ingest.py:183` into
  three probes: `project_panel_v1`, `claims_v1`, `quotes_v1`.
- Segmentation stays as a dedicated pass (it's not a probe; it's an index over
  the issue).

### 2.4 Non-destructive reingest

- Stop cascade-deleting on reprocess (`ingest_corpus/ingest.py:360`).
- Track extraction runs by `(article_id, model, prompt_version, ran_at)`;
  keep history.

### 2.5 CI / test contract

- Add `tests/test_schema_contract.py`: loads schema, asserts the columns
  required by both tracks exist.
- Add GitHub Actions workflow: lint + schema contract test on every PR.
  Gates both tracks from breaking the shared model.

---

## Part 3 — Parallel phase (weeks 4+)

Two tracks, separate owners if possible, both writing into the hardened schema.

### Track A — UCD corpus → Stage 1 "Ask UCD"

1. Re-ingest full corpus (~100 issues) using new probe-based extraction. One-time cost ~$64.
2. `core/embeddings/embed.py` populates `articles.embedding`, `projects.embedding`, `claims.embedding`.
3. API endpoints: `sql_query`, `graph_query` (teaming), `semantic_search`, `get_page_image`.
4. Chat agent in `api/` wired to the four tools above.
5. Minimal frontend chat UI + page-image citation viewer.

**Deliverable:** demoable Ask UCD. Roughly 6–8 weeks after foundation sprint.

### Track B — Utah project feed MVP (public-sector, Wasatch Front)

Order by signal quality / build ease:

1. `up3.py` — state procurement (highest signal, cleanest data).
2. `transparent_utah.py` — contract awards.
3. `dfcm.py` — state building projects.
4. `udot_stip.py` — infrastructure pipeline.
5. `appropriations.py` — legislature line items (LLM extract from PDF).
6. Major county/city portals (Salt Lake, Utah, Davis, Weber).
7. School district board minutes (41 districts, LLM extract).
8. Triage UI + subscriber feed + alert model.

Each scraper writes `projects` rows with `source='public_data'`, goes through
`core/resolution/resolve_projects.py`, and records provenance in `project_sources`.

**Deliverable:** subscriber-facing Utah feed MVP. Roughly 14–18 weeks after
foundation sprint (honest estimate; the proposal's 10–14 assumed infra that
doesn't exist).

### New tables required for Track B only

- `subscribers`, `saved_searches`, `alerts` — watch/notification model.
  Can be scoped to `ingest_public/` initially but should live in `db/schema.sql`
  so UCD features (§3.6 standing watches) share them later.

---

## Part 4 — Merger inflection (corpus Stage 2+)

Once both tracks are populating the shared tables:

- Entity pages (UCD Stage 2) render mixed content: corpus claims + public-data pipeline status on the same firm/project.
- "New UCD coverage available for your tracked project" notification fires when a corpus ingest creates a `project_sources` row on a `project_id` that a subscriber has watched.
- Teaming graph (UCD Stage 3) uses `roles` populated by both tracks.
- Dossier generator (Stage 4) cites corpus narrative + public-data pursuit signal.

No data migration. No code merge. The merger is simply turning on joins that
already work because of the shared schema.

---

## Part 5 — Deferred / explicitly not in scope yet

- Private development tracking (recorder deep-dive, developer network) — scope in after public-sector MVP validates.
- Full-state coverage beyond Wasatch Front + St. George + Logan.
- Quarterly market briefs (UCD Stage 5) — needs trend materialized views;
  defer until corpus has 2+ years of ingest data.
- Subscriber-uploaded private project overlay — decided against for v1 per plan §9.
- Standalone-vs-bundled pricing — business decision, does not affect build order.

---

## Part 6 — Working conventions for safe parallel work

- **One schema file, one migration history.** Never branch schema per track.
- **Shared code goes in `core/`; track-specific code does not.** If both
  `ingest_corpus/` and `ingest_public/` need a helper, it moves to `core/` in
  the same PR.
- **Schema changes require updating `tests/test_schema_contract.py`.** CI blocks
  merges that break the contract.
- **Provenance is mandatory.** Every row in `projects`, `firms`, `roles`,
  `claims`, `quotes` traces to a `project_sources` entry or an `article_id`.
  No orphaned canonical entities.
- **Reingest is non-destructive.** Extractions are versioned, not overwritten.
- **Confidence scores are populated.** `roles.confidence`, `firm_mentions.confidence`,
  `project_sources.confidence` — downstream features rely on these; don't leave
  them at the default.

---

## Timeline summary

| Phase | Weeks | Deliverable |
|---|---|---|
| Repo restructure | 0–1 | Monorepo laid out; one atomic commit |
| Foundation sprint | 1–3 | Hardened schema, resolution, probe runner, CI gates |
| Track A (corpus Stage 1) | 4–11 | Ask UCD chat agent, demoable |
| Track B (Utah MVP) | 4–20 | Public-sector Wasatch Front feed with alerts |
| Merger inflection | 12+ | Entity pages, teaming graph, cross-product notifications |

Track A and Track B run in parallel starting week 4. Track B is longer because
the surface area is larger (8 scrapers + subscriber UX) and it starts from
less existing code.
