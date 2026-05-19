# UCD Intelligent Research Platform

A B2B research tool for *Utah Construction & Design* magazine. Ingests 100+ issues of the magazine (PDF), extracts structured project data using a multimodal LLM pipeline, and serves a conversational research interface embedded in the client's Duda website.

---

## Architecture

```mermaid
flowchart TD
    PDF["issues/*.pdf"] --> ingest

    subgraph ingest["ingest_corpus/ingest.py"]
        A1["1. Render pages to JPEG\n150 DPI, max 1024px"]
        A2["2. Upload to GCS\ngs://uc-and-d-assets/page_images/"]
        A3["3. LLM: segment issue\n→ article list"]
        A4["4. LLM: extract each article\n→ structured JSON"]
        A5["5. Write to Cloud SQL"]
        A1 --> A2 --> A3 --> A4 --> A5
    end

    A2 --> GCS["Cloud Storage\nPage images"]
    A5 --> DB

    subgraph gcp["Google Cloud Platform"]
        DB["Cloud SQL\nPostgreSQL 16\n+ pgvector + pg_trgm"]
        GCS
        SM["Secret Manager\nDATABASE_URL · SECRET_KEY"]
        VAI["Vertex AI\nModel Garden (Claude)"]
        AR["Artifact Registry\nDocker images"]

        subgraph cloudrun["Cloud Run"]
            API["ucd-api\nFastAPI agent"]
            FE["ucd-frontend\nNext.js"]
        end

        API --> DB
        API --> GCS
        API --> VAI
        FE --> API
        SM --> API
        SM --> FE
    end

    FE --> Duda["Duda website\niframe / widget.js embed"]
```

### GCP Services

| Service | Purpose |
|---|---|
| Cloud SQL (PostgreSQL 16) | Primary database — `ucd-db` in `us-central1` |
| Cloud Storage | Page images — `gs://uc-and-d-assets/page_images/{issue_id}/page_NNNN.jpg` |
| Cloud Run | API (`ucd-api`) and frontend (`ucd-frontend`) — scales to zero |
| Artifact Registry | Docker images — `us-central1-docker.pkg.dev/uc-and-d/ucd` |
| Secret Manager | `DATABASE_URL`, `SECRET_KEY` |
| Vertex AI Model Garden | Claude (LLM) — billing stays on the GCP project |

Infrastructure is managed with Terraform. State lives in `gs://uc-and-d-tf-state/terraform/state`.

---

## Repository Layout

```
.
├── ingest_corpus/          # UCD magazine ingestion pipeline
│   ├── ingest.py           # Primary ingestion pipeline (PDF → DB)
│   ├── download_issues.py  # Scrapes utahcdmag.com/archive to download PDFs
│   ├── extract_projects.py # Legacy text-only extractor (kept for reference)
│   ├── make_spreadsheet.py # Exports projects.xlsx from extracted/ JSONs
│   └── requirements.txt
│
├── ingest_public/          # Utah project feed scrapers (UP3, DFCM, STIP, ...)
│
├── core/                   # Shared across both ingestion tracks
│   ├── db.py               # PostgreSQL connection helpers
│   ├── llm.py              # LiteLLM wrapper + retry policy + JSON parsing
│   ├── resolution/         # Firm / project / person entity resolution
│   │   ├── normalize.py        # Deterministic name normalizers
│   │   ├── resolve_firms.py    # firm_mentions → canonical firms
│   │   ├── resolve_people.py   # person_mentions → canonical people
│   │   ├── resolve_projects.py # candidate → canonical projects + merge_projects()
│   │   └── classify_firms.py   # firm_type from roles (rule-based)
│   ├── embeddings/embed.py # Article / project / claim vector population
│   ├── probes/             # Probe registry + runner + definitions/
│   └── geocode/geocode.py  # lat/lng/county enrichment
│
├── db/
│   ├── schema.sql          # PostgreSQL schema (source of truth)
│   └── migrations/         # Forward-only migrations (001-012)
│
├── api/                    # FastAPI backend (Cloud Run)
├── frontend/               # Next.js frontend (Cloud Run)
│
├── infra/                  # Terraform
│   ├── main.tf             # Provider, backend, API enablement
│   ├── database.tf         # Cloud SQL instance, DB, user, secret
│   ├── storage.tf          # GCS bucket, Artifact Registry
│   ├── iam.tf              # Service account and roles
│   ├── services.tf         # Cloud Run services
│   ├── variables.tf
│   └── outputs.tf
│
├── tests/                  # pytest suite
│   ├── test_schema_contract.py   # static parse — runs in CI without a DB
│   ├── test_parse_issue_filename.py
│   ├── test_parse_int.py
│   ├── test_classify_firms.py
│   ├── test_merge_projects.py    # integration; needs DATABASE_URL
│   └── test_ingestion.py         # integration; row counts + spot checks
│
├── notebooks/              # Ad-hoc analysis (e.g. schema_smoke.ipynb)
├── docs/execution_plan.md  # Project plan + status checklist
├── issues/                 # PDF source files (gitignored)
├── extracted/              # Per-issue JSON cache (gitignored)
├── pyproject.toml          # ruff + pytest config
├── .env.example
└── .gitignore
```

---

## Database Design

The schema (`db/schema.sql`) uses PostgreSQL 16 with the `pgvector` and `pg_trgm` extensions.

### Entity–Relationship Overview

```mermaid
erDiagram
    issues ||--o{ articles : "contains"
    articles ||--o{ claims : "has"
    articles ||--o{ quotes : "has"
    articles }o--o| projects : "primary_project"
    articles ||--o{ probe_runs : "analyzed by"
    probes ||--o{ probe_runs : "defines"
    projects ||--o{ roles : "staffed by"
    firms ||--o{ roles : "plays role in"
    firms ||--o{ people : "employs"
    firms ||--o{ firm_mentions : "canonical for"
    people ||--o{ quotes : "attributed to"

    issues {
        serial id PK
        text filename
        int year
        text month_label
        int page_count
        timestamptz ingested_at
    }
    articles {
        serial id PK
        int issue_id FK
        int page_start
        int page_end
        text title
        text article_type
        text summary
        int primary_project_id FK
        vector_1536 embedding
        tsvector search_vector
    }
    projects {
        serial id PK
        text name
        text typology
        text location
        bigint cost_usd
        int year_completed
        text status
        vector_1536 embedding
    }
    firms {
        serial id PK
        text name
        jsonb aliases
    }
    roles {
        serial id PK
        int project_id FK
        int firm_id FK
        text role
        text team
        real confidence
    }
    claims {
        serial id PK
        int article_id FK
        int project_id FK
        text text
        text type
        vector_1536 embedding
        tsvector search_vector
    }
    quotes {
        serial id PK
        int article_id FK
        text speaker_name
        text speaker_firm
        text text
    }
    firm_mentions {
        serial id PK
        text raw_text
        int canonical_id FK
        real confidence
        bool corrected
    }
    probes {
        serial id PK
        text name
        int version
        text prompt
        jsonb schema_json
    }
    probe_runs {
        serial id PK
        int probe_id FK
        int article_id FK
        jsonb output_json
        timestamptz ran_at
    }
    people {
        serial id PK
        text name
        text title
        int firm_id FK
    }
```

### Tables

#### Source material

**`issues`** — one row per PDF file.

| Column | Type | Notes |
|---|---|---|
| `id` | SERIAL | |
| `filename` | TEXT | unique, e.g. `UC-D+February+2026-spreads.pdf` |
| `year`, `month_label` | INTEGER / TEXT | parsed from filename |
| `page_count` | INTEGER | set after GCS upload |
| `ingested_at` | TIMESTAMPTZ | |

**`articles`** — one row per article/segment within an issue.

| Column | Type | Notes |
|---|---|---|
| `id` | SERIAL | |
| `issue_id` | INTEGER | FK → issues |
| `page_start`, `page_end` | INTEGER | inclusive page range |
| `title`, `author` | TEXT | |
| `article_type` | TEXT | `project_feature \| column \| advertisement \| other` |
| `summary` | TEXT | LLM-generated |
| `primary_project_id` | INTEGER | FK → projects (deferred, NOT VALID) |
| `embedding` | vector(1536) | populated by `embed.py` |
| `search_vector` | tsvector | auto-updated by trigger |

#### Canonical entities

**`projects`** — a construction project mentioned in one or more articles.

| Column | Type | Notes |
|---|---|---|
| `id` | SERIAL | |
| `name` | TEXT | |
| `typology` | TEXT | e.g. `office`, `hospitality`, `infrastructure` |
| `location`, `city`, `state` | TEXT | |
| `cost` | TEXT | original string, e.g. `$45,900,000` |
| `cost_usd` | BIGINT | parsed integer for range queries |
| `square_footage` / `sq_ft` | TEXT / INTEGER | both forms |
| `delivery_method` | TEXT | e.g. `Design-Build`, `GC/CM` |
| `year_completed` | INTEGER | |
| `status` | TEXT | `completed \| under_construction \| announced` |
| `source_article_id` | INTEGER | FK → articles |
| `embedding` | vector(1536) | |

**`firms`** — canonical firm records (populated by `resolve.py`).

| Column | Type | Notes |
|---|---|---|
| `id` | SERIAL | |
| `name` | TEXT | unique canonical name |
| `aliases` | JSONB | array of alternate names |
| `website`, `notes` | TEXT | |

**`people`** — individuals mentioned in the magazine.

| Column | Type | Notes |
|---|---|---|
| `id` | SERIAL | |
| `name`, `title` | TEXT | |
| `firm_id` | INTEGER | FK → firms |
| `aliases` | JSONB | |

#### Relationships

**`roles`** — which firm played which role on a project.

| Column | Type | Notes |
|---|---|---|
| `id` | SERIAL | |
| `project_id` | INTEGER | FK → projects |
| `firm_id` | INTEGER | FK → firms |
| `role` | TEXT | e.g. `Architect`, `General Contractor` |
| `team` | TEXT | `design \| construction \| owner` |
| `raw_name` | TEXT | original extracted string before resolution |
| `confidence` | REAL | entity resolution confidence score |

#### Extracted content

**`claims`** — factual statements extracted from articles.

| Column | Type | Notes |
|---|---|---|
| `id` | SERIAL | |
| `article_id` | INTEGER | FK → articles |
| `project_id` | INTEGER | FK → projects (optional) |
| `text` | TEXT | the claim |
| `type` | TEXT | `stat \| milestone \| challenge \| award \| first \| other` |
| `page` | INTEGER | |
| `confidence` | REAL | |
| `embedding` | vector(1536) | for semantic search |
| `search_vector` | tsvector | for FTS |

**`quotes`** — direct quotations with speaker attribution.

| Column | Type | Notes |
|---|---|---|
| `id` | SERIAL | |
| `article_id`, `project_id` | INTEGER | FKs |
| `speaker_name`, `speaker_title`, `speaker_firm` | TEXT | as extracted |
| `speaker_person_id` | INTEGER | FK → people (after resolution) |
| `text` | TEXT | |
| `page` | INTEGER | |

#### Probe system

Probes are reusable LLM extraction templates that can be re-run over articles when requirements evolve.

**`probes`** — a versioned prompt + output schema.

**`probe_runs`** — results of running a probe against an article. Unique on `(probe_id, article_id, probe_version)`.

#### Entity resolution

**`firm_mentions`** — every raw firm name string extracted from the magazine, linked to a canonical `firms` record once resolved.

| Column | Notes |
|---|---|
| `raw_text` | e.g. `"Big-D Construction"`, `"Big D"` |
| `canonical_id` | FK → firms (null until resolved) |
| `confidence` | similarity score |
| `corrected` | manually verified flag |

### Indexes

| Index | Type | Purpose |
|---|---|---|
| `idx_articles_embedding` | ivfflat (cosine) | ANN semantic search on articles |
| `idx_projects_embedding` | ivfflat (cosine) | ANN semantic search on projects |
| `idx_claims_embedding` | ivfflat (cosine) | ANN semantic search on claims |
| `idx_articles_fts` | GIN (tsvector) | Full-text search on article titles + summaries |
| `idx_claims_fts` | GIN (tsvector) | Full-text search on claim text |
| `idx_quotes_fts` | GIN (tsvector) | Full-text search on quote text + speaker |
| `idx_firms_name_trgm` | GIN (pg_trgm) | Fuzzy firm name matching during entity resolution |
| `idx_firm_mentions_trgm` | GIN (pg_trgm) | Fuzzy raw mention matching |

`search_vector` columns are maintained automatically by `BEFORE INSERT OR UPDATE` triggers.

---

## Local Development

### Prerequisites

- Python 3.12+, `poppler` (`brew install poppler`), `psql`
- [Cloud SQL Auth Proxy](https://cloud.google.com/sql/docs/postgres/sql-proxy)
- `gcloud auth application-default login`

### Setup

```bash
python3 -m venv ~/environments/ucd-platform --prompt ucd-platform
source ~/environments/ucd-platform/bin/activate
pip install -r ingest_corpus/requirements.txt

cp .env.example .env
# fill in DATABASE_URL, ANTHROPIC_API_KEY or VERTEXAI_* vars
```

If `python` is "command not found" after `source ... activate`, the venv is
broken (typically because the Python interpreter it was created against
moved). Recreate it with the command above, or call the venv's interpreter
directly: `~/environments/ucd-platform/bin/python ...`.

### Connect to Cloud SQL locally

```bash
cloud-sql-proxy uc-and-d:us-central1:ucd-db --port 5433 &
psql -h 127.0.0.1 -p 5433 -U ucd_user -d ucd_db
```

### Run the ingestion pipeline

All commands run from the **repo root** and invoke the package as a module
(`python -m ingest_corpus.ingest`), so imports resolve correctly without
fiddling with `PYTHONPATH`. Set `DATABASE_URL` in your shell or `.env`.

```bash
# Download all issues (writes into issues/)
python -m ingest_corpus.download_issues

# Smoke-test re-extraction against an already-ingested issue. --reprocess
# is non-destructive (probes are cached by content_hash, prior probe_runs
# are preserved). --no-images skips GCS image fetch and runs probes
# text-only — cheap, fine for prompt validation.
python -m ingest_corpus.ingest \
  --pdfs issues/UC-D+February+2026-spreads.pdf \
  --reprocess --no-images

# Production: ingest a fresh issue (with images) via Vertex.
python -m ingest_corpus.ingest --issues_dir issues/

# Full-corpus run, validation-mode capped at the first 3 issues.
python -m ingest_corpus.ingest --issues_dir issues/ --limit 3
```

Default model is `vertex_ai/gemini-2.5-flash` (chosen via three-way audit
against Sonnet 4.5 and Gemini 2.5 Pro — Flash matched/beat Pro on every
quality metric at ~half the runtime and ~5–10× lower token cost; Anthropic
on Vertex was zero-quota across every region for this project). Override
the model per call:

```bash
# Use Sonnet via Anthropic direct (requires ANTHROPIC_API_KEY)
python -m ingest_corpus.ingest --issues_dir issues/ \
  --model anthropic/claude-sonnet-4-5-20250929

# Or set LITELLM_MODEL in the env (per-environment default)
export LITELLM_MODEL=anthropic/claude-sonnet-4-5-20250929
```

Flag summary:
- `--reprocess` — re-runs probes on already-ingested issues. Safe; cached
  by `content_hash` so it's a no-op when nothing has changed.
- `--no-images` — skip GCS image fetch; probes run text-only. ~3× cheaper.
- `--model` — LiteLLM model string. Defaults to `vertex_ai/gemini-2.5-flash`;
  see provider notes above.
- `--limit N` — cap the number of *issues* processed. For single-PDF
  validation pass `--pdfs <file>` instead.

### Run tests

```bash
pytest                                         # full suite
pytest tests/test_schema_contract.py           # CI-equivalent (no DB)
DATABASE_URL=... pytest tests/test_merge_projects.py  # integration tests
```

### Post-ingest sweeps

After each reingest, run the standalone passes to populate
fields the ingest path leaves null/placeholder:

```bash
# 1. LLM-resolve unresolved firm_mentions (catches ambiguous trigram matches).
python -m core.resolution.resolve_firms

# 2. LLM-resolve unresolved person_mentions.
python -m core.resolution.resolve_people

# 3. Classify firms by firm_type from their roles (rule-based, no LLM cost).
python -m core.resolution.classify_firms

# 4. Embeddings for articles / projects / claims. Requires OPENAI_API_KEY.
python -m core.embeddings.embed

# 5. Geocode projects with NULL lat/lng. Requires GOOGLE_MAPS_API_KEY.
python -m core.geocode.geocode
```

All five are idempotent — only re-process rows that haven't been resolved /
embedded / geocoded yet.

---

## Infrastructure

```bash
cd infra
cp terraform.tfvars.example terraform.tfvars
# fill in secret_key

terraform init
terraform plan
terraform apply
```

After first Docker build+push, set `api_image` and `frontend_image` in `terraform.tfvars` and re-apply.

### Deployed URLs

| Service | URL |
|---|---|
| API | https://ucd-api-sekbs73mtq-uc.a.run.app |
| Frontend | https://ucd-frontend-sekbs73mtq-uc.a.run.app |

---

## LLM Cost Estimate (full corpus)

104 issues · 4,291 pages.

Sonnet 4.5 reference (was the original target before the Vertex quota wall):

| Pass | Input | Output | Cost @ Sonnet 4.5 ($3/$15 per 1M) |
|---|---|---|---|
| Segmentation (all pages) | ~6.4M tokens | ~860K tokens | ~$32 |
| Extraction (~35% of pages) | ~4.5M tokens | ~1.2M tokens | ~$32 |
| **Total** | | | **~$64** |

Current default — Gemini 2.5 Flash on Vertex ($0.30 input / $2.50 output
per 1M tokens; "thinking" output is billed at the same rate as visible
output) — measured against a real 29-page issue traced through Langfuse:
**~$0.0074 per page**, so **~$30–35 for the full corpus** with images.
About half that with `--no-images`. Billed against the GCP project.

Earlier drafts of this doc cited a $5–10 estimate using Google AI Studio
rates ($0.15/$0.60). Vertex pricing is different and that estimate was
wrong.
