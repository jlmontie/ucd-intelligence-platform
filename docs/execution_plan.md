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

## Status

Update this checklist as work lands. The headings mirror the section
structure below; tick a parent only when every child is done.

### Part 1 — Repo restructure
- [x] Monorepo laid out (`db/`, `core/`, `ingest_corpus/`, `ingest_public/`, …)
- [x] `core/db.py` extracted from `pipeline/db_utils.py`
- [x] `infra/` paths updated and committed atomically

### Part 2 — Foundation sprint

**Closure criteria.** A Part 2 item is "done" only when its artifact exists in
the repo *and* the operational behavior it represents has been observed in a
running environment. A migration file isn't done until it's applied to the
dev DB; a resolver isn't done until it has populated rows; a workflow isn't
done until CI has run it green. Artifacts without runtime verification are how
gaps reach Part 3.

- [x] **2.1 Schema hardening** — every column from §2.1 + per-change
      migration files in `db/migrations/`, applied to dev DB; sanity cell of
      `notebooks/schema_smoke.ipynb` runs clean
- [x] **2.2 Entity resolution** — `resolve_firms`, `resolve_projects`,
      `resolve_people` (added; quotes now write `speaker_person_id`), all
      idempotent with deterministic match → LLM tiebreaker. Wired into
      the ingest materialize path so reingest is resolver-aware
      (`upsert_firm` and `_write_project` were the bypass paths — both
      replaced). `merge_projects()` primitive implemented + tested with
      collision cases on every child table; `consolidate(--apply)` wired
      through it. `classify_firms` rule-based pass populates
      `firms.firm_type` from roles (the deferred §2.1 decision). All
      three standalone resolvers ran end-to-end on the live DB:
      100 firm mentions resolved deterministic, 38 person mentions
      resolved deterministic, 64+ firms classified.
- [x] **2.3 Probe runner** — `core/probes/runner.py` + three probes
      exercised end-to-end against the existing issue. v2 of
      `project_panel_v1` ships with explicit body-copy mining for
      `year_completed` and adds an `author` extraction; defensive
      byline cleanup (`_clean_byline`) in materialize handles
      "By X" / "Author: X" variants without bumping the version.
      Cost parsing fixed for "$N million/billion" forms (`_parse_int`).
      Migration 012 fixes a real schema bug — `probes.name UNIQUE`
      blocked v1 + v2 coexistence; replaced with `UNIQUE (name, version)`.
      `column`-type articles now skipped in both runner and ingest
      (`NON_PROBED_ARTICLE_TYPES`).
- [x] **2.4 Non-destructive reingest** — cascade-delete removed;
      extraction routed through probe runner; `--reprocess` against an
      already-ingested issue preserves prior `probe_runs` rows and
      content-hash caching works as designed (no-op when nothing
      changed; new run only on probe-version bump or content_hash
      change). Verified twice: once after v1 seed, again after the v2
      prompt iteration.
- [x] **2.6 Child-row uniqueness** — UNIQUE constraints encoding the natural
      keys of `roles` (project_id, firm_id, role, team), `claims`
      (project_id, article_id, md5(text)), `quotes` (project_id, article_id,
      md5(text), speaker_name) added in migration 011 and `schema.sql`;
      one-shot dedup pass cleared 55 legacy role duplicates;
      `merge_projects` pre-deletes loser rows that would collide on the
      natural key before re-pointing; collision behavior covered in
      `tests/test_merge_projects.py`. Surfaced by Q2 of
      `notebooks/schema_smoke.ipynb`.
- [ ] **2.5 CI / test contract** — `tests/test_schema_contract.py` +
      `.github/workflows/ci.yml` (lint + contract on every PR); workflow
      has run green on ≥1 PR

### Part 3 — Parallel phase

**Track A — UCD corpus → "Ask UCD"**
- [x] Re-ingest full corpus (~100 issues) via probe-based extraction
      *(complete: 104 issues, 4,860 articles, ~$35 actual)*
- [x] `core/embeddings/embed.py` populates article/project/claim vectors
      *(module + CLI built; sweep run end-to-end; idempotent on rows
      where the column is still NULL — re-run after each ingest)*
- [x] `core/geocode/geocode.py` populates `projects.lat/lng/county`
      *(scaffolded module made real; sweep run end-to-end; idempotent;
      Google Maps Geocoding backend, pluggable)*
- [x] Automated entity-consolidation passes (Tier 1 + Tier 2)
      *(`core/resolution/consolidate.py`: paren-only firm merges +
      paren-only role dedup + LLM-tiebreaker fuzzy firm merge. After
      both: firms 4,186 → 3,367 (-20%); LLM cost ~$1.50 in Flash.)*
- [ ] **Manual firm consolidation pass** — see §3.A.1 below; required
      before the chat agent goes live to subscribers
- [ ] **Column extraction + embedding (deferred)** — see §3.A.2.
      Editorial columns (Publisher's Message, Industry News, A/E/C
      People) are currently excluded from semantic search because
      probes don't run on them. Path documented for when client
      demand justifies the work.
- [ ] Langfuse instance stood up + LiteLLM callback + redaction hook
- [ ] API endpoints: `sql_query`, `graph_query`, `semantic_search`, `get_page_image`
- [ ] Chat agent in `api/` wired to the four tools
- [ ] Frontend chat UI + page-image citation viewer

**Track B — Utah project feed MVP**
- [ ] `up3.py` (state procurement)
- [ ] `transparent_utah.py` (contract awards)
- [ ] `dfcm.py` (state building projects)
- [ ] `udot_stip.py` (infrastructure pipeline)
- [ ] `appropriations.py` (legislature line items)
- [ ] Major county/city portals (Salt Lake, Utah, Davis, Weber)
- [ ] School district board minutes (41 districts)
- [ ] Triage UI + subscriber feed + alert model
- [ ] `subscribers`, `saved_searches`, `alerts` tables in `db/schema.sql` + migration applied to dev DB

### Part 4 — Merger inflection
- [ ] Entity pages render mixed corpus + public-data content
- [ ] "New UCD coverage on tracked project" notification wired
- [ ] Teaming graph reads `roles` from both tracks
- [ ] Dossier generator (Stage 4) cites both narrative + pursuit signal

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

Every schema change gets a migration file so downstream work can pin. The
migration must also be applied to the dev DB and the corresponding fixture in
`notebooks/schema_smoke.ipynb` re-run before §2.1 closes — the static contract
test (§2.5) only validates `schema.sql`, not the live DB.

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

1. Re-ingest full corpus (~100 issues) using new probe-based extraction. Actual cost $35 on Gemini 2.5 Flash via Vertex.
2. `core/embeddings/embed.py` populates `articles.embedding`, `projects.embedding`, `claims.embedding`.
3. Run the automated consolidation passes:
   `python -m core.resolution.consolidate firms --apply` (paren-merge),
   `python -m core.resolution.consolidate roles --apply` (paren-dedup),
   `python -m core.resolution.consolidate firms-fuzzy --apply` (LLM tiebreaker for trgm-medium pairs).
4. **§3.A.1 — Manual firm consolidation pass (see below).** Required
   before the chat agent ships. Automation has a residual ~5–10% error
   rate that domain expertise is the only reliable filter for.
5. Stand up a **Langfuse** instance (self-hosted on Cloud Run + the existing
   Cloud SQL) and wire it as a LiteLLM callback. The chat agent must not go
   live to subscribers without traces flowing — every prompt/completion,
   token cost, and tool call needs to be recoverable for evals and
   debugging. Build a redaction hook before persistence (firm names and
   project intent are PII-adjacent in this domain).
6. API endpoints: `sql_query`, `graph_query` (teaming), `semantic_search`, `get_page_image`.
7. Chat agent in `api/` wired to the four tools above.
8. Minimal frontend chat UI + page-image citation viewer.

**Deliverable:** demoable Ask UCD. Roughly 6–8 weeks after foundation sprint.

#### §3.A.1 — Manual firm consolidation (domain-expert pass)

The automated consolidation pipeline catches the easy wins (parenthetical
qualifiers, typos, plural/possessive variants, "& vs and") but cannot
resolve cases that require knowing the Utah construction industry:

- **Subsidiary / parent-brand identity:** `Delta` and `Delta Air Lines`
  are the same client entity. The LLM doesn't know the abbreviation
  convention.
- **Acquisition history:** `Morrison Hershfield` is now `Stantec` —
  same firm, different name post-2024.
- **Geographic specifiers that don't imply separate companies:**
  `FFKR Architects of Salt Lake` and `FFKR Architects` are the same
  firm; geographic qualifier is just a regional office disclosure.
- **Trade-name vs legal-name:** `Big-D Construction` vs `Big-D
  Construction Corp` vs `Big-D` — same firm, three surface forms.

**Suggested workflow:**

1. **Surface candidates.** Query the top 30–50 firms by role count.
   These are the highest-leverage merges because each one collapses
   many roles, claims, and quotes into a single canonical entity.
   ```sql
   SELECT f.id, f.name, f.firm_type,
          (SELECT COUNT(*) FROM roles WHERE firm_id = f.id) AS n_roles,
          f.aliases
   FROM firms f
   ORDER BY n_roles DESC LIMIT 50;
   ```
   Domain expert eyeballs the list for visible duplicates (`Delta`
   alongside `Delta Air Lines`; `Stantec` alongside `Morrison
   Hershfield`; etc.).

2. **Spot-check the low-sim auto-merges.** The LLM tiebreaker pass had
   a ~1% false-positive rate at the sim=0.55 floor. Re-run with the
   `--limit` flag against a CSV-export of the merge log, eyeball any
   merge at sim < 0.65, flag wrong ones.

3. **Apply each confirmed merge.** Call `merge_firms(conn, winner_id,
   loser_id)` directly from a Python shell, one merge at a time:
   ```python
   from core.db import get_conn
   from core.resolution.consolidate import merge_firms
   conn = get_conn()
   merge_firms(conn, winner_id=42, loser_id=1037)   # Delta <- Delta Air Lines
   merge_firms(conn, winner_id=189, loser_id=2304)  # Stantec <- Morrison Hershfield
   conn.close()
   ```
   The merge primitive handles all FK re-pointing (roles, people,
   firm_mentions, quotes) and captures the loser's name as an alias
   on the winner.

4. **Track decisions in a flat file.** Keep a `docs/manual_firm_merges.md`
   or similar with one line per merge: `winner_id, loser_id, rationale,
   reviewed_by, date`. Provides an audit trail for client questions
   about why two names became one.

5. **Cadence:** Run this pass after every major corpus reingest and
   monthly thereafter as new issues land. Track A's `semantic_search`
   tool will surface visible duplicates fast — those become inputs to
   the next manual pass.

**False-positive recovery is non-trivial.** `merge_firms` destroys the
loser firm row and preserves its name only as an alias. There is no
inverse `split_firm` primitive yet. If a manual reviewer determines an
auto-merge was wrong:

- Re-extract the loser's identity from the alias: insert a new `firms`
  row with the alias's name.
- Find which of the survivor's roles / mentions / quote-attributions
  should belong to the resurrected firm. This typically requires
  reading the source articles, which is why the safer path is to
  catch false-positive merges *before* trusting the automation
  (review the candidate log before re-running with `--apply` next
  time, or set `sim_floor=0.65` to lose recall in exchange for
  precision).

If recovery work becomes recurrent, build a proper `split_firm`
primitive that takes `(firm_id, alias_to_extract, role_id_filter)`
and reverses the merge for the listed roles. Defer until then.

#### §3.A.2 — Editorial column embeddings (deferred)

**Current state.** Embedding pass excludes articles where
`article_type` is `column`, `advertisement`, or `other`. The chat
agent's semantic search therefore can't surface Publisher's Message
editorials, Industry News roundups, A/E/C People career-move
announcements, or other recurring departmental content.

**Why we don't embed columns today.** The probe runner only fires on
`project_feature` and null-type articles (`NON_PROBED_ARTICLE_TYPES`
filters columns / ads / other out of probe scope). Columns therefore
have NULL `summary`, no `claims`, no `quotes`. If we embedded them
now, the input text would be just `articles.title` — and every
"Publisher's Message" across 104 issues shares that title verbatim.
The resulting vectors would cluster identically per department,
making semantic search across columns effectively useless and the
top-K results dominated by collisions.

**Why we might want them later.** A research platform that pitches
itself as "industry intelligence" should be able to answer:
- "What's Bradley Fullmer said about Utah's labor shortage over time?"
- "Track A/E/C People announcements — who moved to which firm in 2024?"
- "Find Industry News items mentioning Big-D Construction."

Editorial content carries this signal; the project-feature corpus
doesn't.

**Path to include them — a self-contained workstream:**

1. **Add a `column_v1` probe** in `core/probes/definitions/`. Prompt
   should extract a 2–3 sentence summary, key themes, named
   firms / people / projects mentioned, and the column type
   (Publisher's Message / Industry News / A/E/C People / other).
   Schema mirrors `project_panel_v1` but lightweight — no project
   info panel.
2. **Add a `column_summary_v1` extension to `claims_v1`** OR
   broaden the existing claims probe to also fire on columns,
   capturing standalone industry-news facts.
3. **Remove `column` from `NON_PROBED_ARTICLE_TYPES`** in
   `ingest_corpus/ingest.py` and `core/probes/runner.py:fetch_articles`.
4. **Run probes against the 522 column articles** — text-only is
   fine since column layouts are simpler. ~$5 estimated, ~30 min.
5. **Update `_articles_sql` in `core/embeddings/embed.py`** to
   include `article_type = 'column'` alongside `project_feature` and
   NULL.
6. **Re-embed the column subset.** Idempotent; cheap.
7. **Update the chat agent's `semantic_search` tool** so it can
   filter or include columns based on the query (the agent should
   probably default to project_features and offer column search as a
   distinct mode, since the content shape is different).

**`other`-type articles** are a mixed bag — Tables of Contents and
Indexes are pure noise, but "2026 Utah Economic Outlook" and "AGC of
Utah Awards" are gold. Re-classifying them before deciding is the
right move; defer until after columns are settled.

**Trigger condition:** revisit when a client demo specifically asks
to surface editorial / commentary content, or when corpus volume
makes the project-feature-only search feel narrow.

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
- **Schema changes require updating `tests/test_schema_contract.py` AND
  applying the migration to the dev DB before merge.** CI blocks merges that
  break the static contract; the dev-DB apply step catches real-world drift
  (constraints, backfills, existing data) the static parser misses.
- **Provenance is mandatory.** Every row in `projects`, `firms`, `roles`,
  `claims`, `quotes` traces to a `project_sources` entry or an `article_id`.
  No orphaned canonical entities.
- **Reingest is non-destructive.** Extractions are versioned, not overwritten.
- **Confidence scores are populated.** `roles.confidence`, `firm_mentions.confidence`,
  `project_sources.confidence` — downstream features rely on these; don't leave
  them at the default.
- **Every LLM call is traced.** Prompts, completions, token costs, tool
  calls, and latency persist to Langfuse. Subscriber-facing prompts go
  through the redaction hook before persistence. Traces feed the eval
  dataset that drives prompt and probe-version bumps — "continual
  improvement" lives off this stream. Track B scrapers are exempt for
  v1 (their LLM use is structured extraction, already covered by
  `probe_runs`), but should opt in once they touch free-form prompts.

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
