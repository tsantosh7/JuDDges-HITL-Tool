# HITL Tool

Human-in-the-loop document search, annotation, review, and model-evaluation tooling.

This repository contains a FastAPI web application backed by Postgres and Solr. It is designed around a shared document corpus: documents are ingested once, indexed for search, grouped into projects, annotated by humans or model outputs, reviewed, and exported for downstream analysis.

## What This Project Does

The app supports:

- Document ingestion from JSONL into Postgres and Solr.
- Full-text search, filtering, faceting, and random sampling through Solr.
- User accounts, sessions, access codes, roles, and basic access control.
- Projects and project document membership.
- Human annotation workflows through Hypothesis integration.
- Canonical code/tag management, including aliases.
- Model prediction ingestion and comparison workflows.
- Topic assignment runs with per-document topic labels.
- FAISS-backed embedding search helpers.
- CSV/export workflows for analysis.

## Architecture

The application is composed of these services:

| Service | Purpose |
| --- | --- |
| `api` | FastAPI web app, HTML UI, API endpoints, auth, Solr/Hypothesis integration |
| `postgres` | Canonical relational data store |
| `solr` | Search index and faceting engine |
| `redis` | Queue/cache dependency for future/background workflows |
| `worker` | Worker container placeholder; currently only prints environment information |

The app uses a **global Solr core model**. Documents live in one shared Solr core, and project membership is represented with indexed fields such as `project_ids_ss` rather than separate Solr cores per project.

## Repository Layout

```text
.
+-- api/
|   +-- Dockerfile
|   +-- requirements.txt
|   +-- app/
|       +-- main.py              # FastAPI entry point and main API endpoints
|       +-- db.py                # SQLAlchemy engine/session setup
|       +-- init_db.py           # Startup table creation
|       +-- models.py            # Core SQLAlchemy models
|       +-- auth/                # User/session/access-code auth
|       +-- topics/              # Topic-run helper service
|       +-- ui/                  # Server-rendered UI routes
|       +-- templates/           # Jinja templates
|       +-- static/              # CSS, images, static assets
+-- data/                        # Local data files mounted into the API container
+-- postgres/init/               # Postgres init SQL
+-- scripts/                     # Operational scripts for ingest, sync, Solr recompute, embeddings
+-- solr/configsets/             # Solr configset for the HITL core
+-- training/                    # Model/evaluation/training-data utilities
+-- worker/                      # Worker container
+-- docker-compose.yml           # Local multi-service environment
```

## Key Runtime Paths

- Web landing page: `GET /`
- Health check: `GET /health`
- UI routes: `/ui/...`
- Auth routes: `/auth/...`
- Search API: `GET /search`
- Ingest API: `POST /ingest_batch/{core}`
- Solr commit API: `POST /solr/{core}/commit`
- Project APIs: `/projects/...`
- Code/tag APIs: `/codes/...`
- Topic APIs: `/topics/...`
- Hypothesis APIs: `/hypothesis/...`
- Export APIs: `/export/...`

## Data Model Overview

Core tables are declared in `api/app/models.py`:

- `teams`: organizations or groups that own projects.
- `projects`: named review/search workspaces.
- `documents`: canonical document records.
- `project_documents`: many-to-many project/document membership.
- `hypothesis_groups`: synced Hypothesis groups.
- `project_hypothesis_review_groups`: selected shared/project review group per project.
- `hypothesis_annotations`: synced Hypothesis annotations.
- `user_hypothesis_workspaces`: legacy per-user review mapping, retained for compatibility.
- `codes`: canonical code/tag registry.
- `code_aliases`: alias-to-canonical-code mapping.
- `project_document_reviews`: per-project document review state.
- `topic_runs`: topic assignment runs.
- `document_topics`: document-level topic assignments.
- `doc_embeddings`: stored document embeddings.

Auth tables are declared in `api/app/auth/models.py`:

- `users`
- `access_codes`
- `password_reset_tokens`

Additional init SQL exists in `postgres/init/020_model_predictions.sql` for model predictions, topic tables, and embeddings.

## Configuration

The Docker Compose setup defines most local defaults. Important environment variables:

| Variable | Purpose |
| --- | --- |
| `DATABASE_URL` | SQLAlchemy Postgres URL |
| `SESSION_SECRET` | Secret used for signed browser sessions |
| `SOLR_BASE_URL` | Base URL for Solr, usually `http://localhost:8983/solr` locally |
| `SOLR_GLOBAL_CORE` | Shared Solr core name; defaults to `hitl_test` |
| `REDIS_URL` | Redis connection string |
| `FAISS_DIR` | Directory containing FAISS index files |
| `DATA_DIR` | Mounted data directory used by scripts and snapshots |
| `EMBEDDING_MODEL` | Embedding model name |
| `HYPOTHESIS_API_TOKEN` | Hypothesis API token for sync/push workflows |
| `HYPOTHESIS_EXCLUDE_PUBLIC` | Whether to exclude the public Hypothesis group by default |
| `PUBLIC_BASE_URL` | Public app URL used in emails and links |
| `ADMIN_EMAIL` | Admin/user-testing contact email |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`, `SMTP_FROM` | Email delivery settings |

Do not commit real secrets. Use local `.env` or deployment-specific secret storage for tokens, SMTP credentials, and session secrets.

## Local Setup

Prerequisites:

- Docker and Docker Compose.
- Enough memory for Solr; the current compose file configures a 4 GB Solr heap.
- A Hypothesis token if you need annotation sync.

Start the stack:

```bash
docker compose up --build
```

The API should be available at:

```text
http://localhost:8000
```

Health check:

```bash
curl http://localhost:8000/health
```

Solr should be available at:

```text
http://localhost:8983/solr
```

Postgres listens on:

```text
localhost:5432
```

## First-Run Notes

On API startup, `api/app/main.py` calls `init()` from `api/app/init_db.py`, which runs SQLAlchemy `create_all()` for registered models. It also seeds the initial code registry and code aliases.

Postgres also runs SQL files from:

```text
postgres/init/
```

Those init scripts only run when the Postgres Docker volume is first created. If you already have a `pgdata` volume, changes to `postgres/init/*.sql` will not automatically re-run.

## Solr Core Setup

The app assumes a Solr core exists, commonly named:

```text
hitl_test
```

The configset lives under:

```text
solr/configsets/hitl_configset
```

If the core does not exist, create it in Solr before ingesting documents. One common local approach is to exec into the Solr container and create the core from the mounted configset.

```bash
docker compose exec solr solr create_core -c hitl_test -d /var/solr/data/configsets/hitl_configset
```

If a core already exists and you need to rebuild it, be careful: deleting a Solr core removes the search index, not the canonical Postgres documents.

## Document Ingestion

The main document ingest script is:

```text
scripts/ingest_jsonl.py
```

Example:

```bash
python scripts/ingest_jsonl.py \
  --file data/normalised_data.jsonl \
  --api http://localhost:8000 \
  --solr http://localhost:8983/solr \
  --core hitl_test \
  --batch 250 \
  --final-solr-commit
```

Ingestion posts batches to:

```text
POST /ingest_batch/{core}
```

The API then:

1. Normalizes/upserts the document in Postgres.
2. Converts the record into a Solr document.
3. Adds it to the selected Solr core.
4. Optionally commits or uses Solr `commitWithin`.

## Search

Search is backed by Solr:

```bash
curl "http://localhost:8000/search?q=*:*&core=hitl_test&rows=10"
```

Search supports:

- `q`: Solr query text.
- `fq`: filter queries.
- `project_id`: project scoping.
- `rows` and `start`: pagination.
- `fl`: requested Solr fields.
- `include_facets`: facet counts.
- `include_hypothesis_links`: adds Hypothesis links for results.

## UI

The UI is server-rendered with Jinja templates.

Active UI routes are mounted under:

```text
/ui
```

The UI route layer makes internal ASGI calls back into the FastAPI API endpoints and forwards browser cookies so protected API calls see the same session.

Useful pages include:

- `/ui/about`
- `/ui/dashboard`
- `/ui/search`
- `/ui/projects`
- `/ui/add_to_project`
- `/ui/export`
- `/ui/codes`
- `/ui/settings/hypothesis`

## Authentication and Access

Auth routes are mounted under:

```text
/auth
```

Supported flows include:

- Login/logout.
- Registration.
- Access-code redemption.
- Admin access-code management.
- Admin user management.
- Forgot/reset password.
- Access requests.

The session user is stored in the Starlette session cookie. Roles and access plans are enforced by dependencies in `api/app/auth/deps.py`.

## Hypothesis Workflow

Hypothesis support is used to sync annotation groups and annotations into the local database and to generate document annotation links.

Relevant workflows:

- Configure a Hypothesis token through `HYPOTHESIS_API_TOKEN`.
- Select a shared or project-specific Hypothesis review group for each project.
- Copy model suggestions and gold references into that review group, tagged by project and document.
- Sync the selected review group through the API or scripts.
- Use document links to open Hypothesis in-context annotation views.

Public Hypothesis syncing is guarded by default. The code treats `__world__` as public and excludes it unless explicitly configured otherwise.

## Codes and Model Predictions

The app maintains a canonical code registry:

- `codes`: canonical labels.
- `code_aliases`: legacy/alternate labels mapped to canonical labels.

The training and scripts folders include utilities for:

- Normalizing data keys.
- Converting JSONL for fine-tuning.
- Running few-shot model responses.
- Ingesting model predictions.
- Recomputing model/human values into Solr.
- Comparing and scoring prediction outputs.

Model predictions are stored in the `model_predictions` table created by `postgres/init/020_model_predictions.sql`.

## Topic Assignment

Topic assignment is represented by:

- `topic_runs`
- `document_topics`

The current app behavior is user-scoped: topic runs are owned by users and are not automatically pushed into the shared Solr core. This avoids private topic labels leaking into global search facets.

Important endpoints:

- `POST /topics/runs`
- `GET /topics/runs`
- `POST /topics/runs/{run_id}/activate`
- `POST /topics/ingest`
- `GET /documents/{document_id}/topics`
- `POST /topics/label`
- `POST /topics/reject`

## Embeddings and FAISS

Document embeddings are stored in Postgres in `doc_embeddings`.

The FAISS helper expects:

```text
$FAISS_DIR/docs.index
$FAISS_DIR/docs_ids.jsonl
```

Build the FAISS index from stored embeddings:

```bash
DATABASE_URL="postgresql+psycopg://corpus:corpuspass@localhost:5432/corpusdb" \
FAISS_DIR="./data/faiss" \
python scripts/build_faiss_index.py
```

The API helper in `api/app/faiss_store.py` lazy-loads that index and performs cosine-similarity search against normalized vectors.

## Common Operational Scripts

| Script | Purpose |
| --- | --- |
| `scripts/ingest_jsonl.py` | Batch-ingest normalized JSONL into API/Solr |
| `scripts/sync_hypothesis.py` | Sync Hypothesis annotations |
| `scripts/sync_hypothesis_stream_client.py` | Streaming Hypothesis sync client |
| `scripts/ingest_model_predictions.py` | Load model predictions into Postgres |
| `scripts/recompute_human_values_from_db_to_solr.py` | Recompute human annotation values into Solr |
| `scripts/recompute_model_values_to_solr.py` | Recompute model prediction values into Solr |
| `scripts/build_faiss_index.py` | Build FAISS index from DB embeddings |
| `scripts/ingest_doc_embeddings.py` | Ingest document embeddings |
| `scripts/push_predictions_to_hypothesis_latest.py` | Push latest predictions to Hypothesis |
| `scripts/cleanup_rejected_model_annotations.py` | Cleanup rejected model annotations |

Training utilities live in `training/` and cover fine-tune conversion, few-shot prediction generation, normalization, validation, scoring, and comparison CSV generation.

## Development

Install API dependencies locally:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r api/requirements.txt
```

Run the API locally, assuming Postgres and Solr are already running:

```bash
export DATABASE_URL="postgresql+psycopg://corpus:corpuspass@localhost:5432/corpusdb"
export SOLR_BASE_URL="http://localhost:8983/solr"
export SESSION_SECRET="dev-secret-change-me"
export DATA_DIR="./data"
uvicorn app.main:app --app-dir api --reload --host 0.0.0.0 --port 8000
```

Check formatting and syntax manually as needed. This repository does not currently define a test suite, formatter config, or CI workflow.

## Backup and Generated Data

The repository contains helper backup scripts and local data folders. Treat these carefully:

- `_backup/`
- `backup.sh`
- `api/app/backup.sh`
- `backup.log`
- `training/runs/`
- `data/emb_out/`
- `codebase_dump.txt`

Some of these may be generated, large, or environment-specific. Confirm what should be versioned before committing additional outputs.

## Known Caveats

- The worker container is currently a placeholder and does not process jobs.
- There is no migration framework such as Alembic; startup uses `create_all()` plus Postgres init SQL.
- Postgres init SQL only runs on first volume creation.
- Some tables used by raw SQL paths may need to be verified against a fresh database before production use.
- Docker Compose currently contains local-development defaults. Move secrets and deployment-specific values into environment-specific secret management before public or production use.
- Solr core creation is not fully automated by the app startup path.

## Quick Start Summary

```bash
docker compose up --build
docker compose exec solr solr create_core -c hitl_test -d /var/solr/data/configsets/hitl_configset
curl http://localhost:8000/health
python scripts/ingest_jsonl.py --file data/normalised_data.jsonl --core hitl_test --final-solr-commit
```

Then open:

```text
http://localhost:8000
```
