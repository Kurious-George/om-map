# Starwood OM Map

Internal Streamlit app for Starwood Capital. Upload real estate Offering
Memorandum PDFs; Claude extracts the key property fields, Google Maps
geocodes the address, and every property appears as a color-coded pin on a
shared world map.

## Stack

- **Streamlit** (UI, single-process, session-isolated state)
- **Anthropic Claude Sonnet 4.6** (PDF extraction via tool use + prompt caching)
- **Google Maps Geocoding**
- **PostgreSQL + SQLAlchemy 2.x + Alembic** (metadata and audit)
- **Azure Blob Storage** (raw PDFs, authenticated via `DefaultAzureCredential`)
- **Folium + streamlit-folium** (Leaflet map with `MarkerCluster`)

## Quick start — local dev with Docker Compose

Prerequisites:
- Docker Desktop
- `az login` (so `DefaultAzureCredential` inside the container picks up your
  Azure CLI token for Blob access)
- A Postgres-capable machine (the compose file spins one up for you)

Setup:
```bash
cp .env.example .env
# Fill in ANTHROPIC_API_KEY, GOOGLE_MAPS_API_KEY, AZURE_STORAGE_ACCOUNT,
# APP_USERS. DATABASE_URL already points at the compose `db` service.
```

Start the stack:
```bash
docker compose up --build
```

Apply migrations (first run only, or whenever the schema changes):
```bash
docker compose run --rm app alembic upgrade head
```

Open http://localhost:8501.

## Quick start — bare-metal dev (no Docker)

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
# Point DATABASE_URL at a local or shared Postgres.

alembic upgrade head
streamlit run app.py
```

## Production deployment (Azure)

The same `Dockerfile` is the prod artifact. The compose `db` service is
for local dev only — in prod, point `DATABASE_URL` at your managed
**Azure Database for PostgreSQL** instance.

Checklist:
- Container runs on Azure infra (App Service / Container Apps / AKS) with a
  system-assigned **managed identity**. `DefaultAzureCredential` picks it up
  automatically — no connection string needed for Blob access.
- Grant the managed identity the **Storage Blob Data Contributor** role on
  the target container.
- Provide the same env vars as local dev (`ANTHROPIC_API_KEY`,
  `GOOGLE_MAPS_API_KEY`, `AZURE_STORAGE_ACCOUNT`, `AZURE_BLOB_CONTAINER`,
  `APP_USERS`, `DATABASE_URL`). Don't set the Azure CLI mount.
- Run `alembic upgrade head` as a one-shot job before the app starts.

## Project layout

```
app.py            Streamlit entry point — UI, upload pipeline, review queue
db.py             SQLAlchemy models, engine, session factory
storage.py        Azure Blob upload/download by SHA-256
extractor.py      Claude PDF extraction (tool use + prompt caching + preflight)
geocoder.py       Google Maps geocoding with match-quality review flags
map_builder.py    Folium map + MarkerCluster + review-queue filter
auth.py           Current-user seam (selectbox today, proxy-header swap later)
migrations/       Alembic environment and versioned SQL migrations
Dockerfile        Non-root Python 3.12 slim image with health check
docker-compose.yml Local dev stack (app + postgres)
```

## Operational notes

- **Dedup:** SHA-256 of the file bytes. Uploading the same PDF a second time
  is a no-op in both the DB (unique index) and Blob (`overwrite=False`).
- **Soft delete only:** the `Delete` button sets `deleted_at`; rows are
  never hard-removed from the app.
- **Review queue:** anything with `needs_review=True`, missing coordinates,
  a failed/skipped geocode, or a failed extraction. Clear it by clicking
  **Reviewed** on each row.
- **Caching:** `load_properties()` is cached with `ttl=30` and explicitly
  cleared after every mutation in the acting session, so uploaders see
  their changes immediately and other sessions see them within 30s.
- **Claude cost:** Sonnet 4.6 at the input/output prices Anthropic lists
  works out to roughly $0.05–$0.10 per 15-page OM with prompt caching on
  the system prompt + tool schema.

## Known gaps / future work

- No edit UI for extracted property fields (only `Reviewed` and `Delete`
  in v1). Add a form-style editor when human-in-the-loop corrections
  become common.
- `auth.py` uses a sidebar selectbox. Swap for a reverse-proxy header
  (e.g. `X-Forwarded-User`) once IT confirms the production ingress setup.
- Logo is loaded from Starwood's public CDN. Commit a local copy to
  `assets/` if you want the app to work without outbound HTTP to that host.
- No background-worker extraction. All Claude/geocoding work runs inline
  on the uploader's session thread. If upload volume ever pushes above
  ~20 concurrent heavy users, move extraction to Celery/RQ.
