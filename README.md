# Starwood OM Map

Internal Streamlit app for Starwood Capital. Upload real estate Offering
Memorandum PDFs; Claude extracts the key property fields, Google Maps
geocodes the address, and every property appears as a color-coded pin on a
shared world map.

[![Watch the video](https://youtube.com)](https://youtu.be/DrqSYGGK9_c)

## Stack

- **Streamlit** (UI, single-process, session-isolated state)
- **Anthropic Claude Sonnet 4.6** (PDF extraction via tool use + prompt caching)
- **Google Maps Geocoding**
- **PostgreSQL + SQLAlchemy 2.x + Alembic** (metadata + audit)
- **Azure Blob Storage** (raw PDFs, `DefaultAzureCredential` auth)
- **Folium + streamlit-folium** (Leaflet map with `MarkerCluster`)

---

## Quick start — local dev (Docker Compose)

### Prerequisites

- **Docker Desktop**
- **Azure CLI:** `winget install Microsoft.AzureCLI` (close + reopen PowerShell)
- **An Azure subscription** — free tier is sufficient: https://azure.microsoft.com/free
- **Anthropic API key:** https://console.anthropic.com
- **Google Maps API key(s)** with the **Geocoding API** and **Street View
  Static API** enabled. Two keys recommended — see `.env.example` for the
  server-side vs client-side split and why.

### 1. Provision the Azure resources

PowerShell, step by step. Customize the first three variables.

```powershell
az login
# If you have multiple subscriptions, pin the one you want to use:
# az account set --subscription "<SubscriptionId>"

$RG        = "starwood-om-dev"
$LOCATION  = "eastus"
$STORAGE   = "swomdev$(Get-Random -Maximum 99999)"   # must be globally unique
$CONTAINER = "om-pdfs"

az group create -n $RG -l $LOCATION

az storage account create `
  --name $STORAGE `
  --resource-group $RG `
  --location $LOCATION `
  --sku Standard_LRS --kind StorageV2 `
  --allow-blob-public-access false

az storage container create `
  --account-name $STORAGE `
  --name $CONTAINER `
  --auth-mode login

# Create a service principal scoped to the container. The output JSON has
# the three values you need for .env (appId, password, tenant).
$ACCOUNT_ID = az storage account show -n $STORAGE -g $RG --query id -o tsv
$SCOPE      = "$ACCOUNT_ID/blobServices/default/containers/$CONTAINER"
az ad sp create-for-rbac `
  --name "starwood-om-dev-sp" `
  --role "Storage Blob Data Contributor" `
  --scopes $SCOPE
```

### 2. Configure `.env`

```powershell
cp .env.example .env
```

Fill in:
- `ANTHROPIC_API_KEY`
- `GOOGLE_MAPS_API_KEY` (server-side: geocoding)
- `GOOGLE_STREETVIEW_API_KEY` (client-side: Street View in marker popups).
  If unset, the app falls back to `GOOGLE_MAPS_API_KEY`.
- `AZURE_STORAGE_ACCOUNT` — the `$STORAGE` value from step 1
- `AZURE_BLOB_CONTAINER` — keep as `om-pdfs` (matches above)
- `AZURE_CLIENT_ID` / `AZURE_CLIENT_SECRET` / `AZURE_TENANT_ID` — from the
  service principal JSON (`appId` / `password` / `tenant`)

Leave `DATABASE_URL` as the default; compose already points it at the `db` service.

### 3. (Optional) drop in the Starwood logo

Save the Starwood SVG logo as `static/StarwoodCapitalLogo.svg`. The app
renders a text header if the file is missing, so this step is optional.

### 4. Bring everything up

```powershell
docker compose up --build -d
docker compose run --rm app alembic upgrade head
```

Open http://localhost:8501.

---

## Daily dev workflow

The compose file mounts your working tree into the container, so `.py` edits
take effect on Streamlit's next rerun with no rebuild.

```powershell
docker compose logs -f app          # tail logs
docker compose restart app          # force-reload after module-level changes
docker compose down                 # stop, keep DB data
docker compose down -v              # stop, wipe DB (then re-run alembic upgrade head)
docker compose up --build -d        # rebuild image (needed after requirements.txt changes)
```

---

## Production deployment (Azure)

The same `Dockerfile` is the prod artifact. Point `DATABASE_URL` at your
managed **Azure Database for PostgreSQL** and drop the `db` service.

- Run the container on Azure infra (App Service / Container Apps / AKS) with
  a system-assigned **managed identity**. `DefaultAzureCredential` picks it
  up automatically — **leave `AZURE_CLIENT_ID` / `AZURE_CLIENT_SECRET` /
  `AZURE_TENANT_ID` unset** so `EnvironmentCredential` steps aside and
  `ManagedIdentityCredential` handles auth.
- Grant the managed identity **Storage Blob Data Contributor** on the
  target blob container.
- Remove the `- .:/home/app/app` volume from `docker-compose.yml` before
  deploying (it's a dev-only convenience).
- Run `alembic upgrade head` as a one-shot job before the app starts.

---

## Project layout

```
app.py             Streamlit entry — UI, upload pipeline, review queue
db.py              SQLAlchemy models, engine, session factory
storage.py         Azure Blob upload/download by SHA-256
extractor.py       Claude PDF extraction (tool use + prompt caching + preflight)
geocoder.py        Google Maps geocoding with match-quality review flags
streetview.py      Google Street View Static URL builder (popup images)
map_builder.py     Folium map + MarkerCluster + review-queue filter
migrations/        Alembic environment + versioned migrations (raw SQL)
static/            Served by Streamlit at /app/static/<filename>
.streamlit/        Streamlit config (static serving enabled)
Dockerfile         Non-root Python 3.12 slim image with health check
docker-compose.yml Local dev stack (app + postgres)
```

---

## Operational notes

- **Dedup:** SHA-256 of the file bytes. Uploading the same PDF twice is a
  no-op in both the DB (unique index) and Blob (`overwrite=False`).
- **Soft delete only:** the `Delete` button sets `deleted_at`; rows are
  never hard-removed from the app.
- **Review queue:** anything with `needs_review=True`, missing coordinates,
  a failed/skipped geocode, or a failed extraction. Clear by clicking
  **Reviewed** on each row.
- **Caching:** `load_properties()` is `@st.cache_data(ttl=30)` and is
  cleared after every mutation in the acting session. Uploaders see their
  changes instantly; other sessions see them within 30s.
- **Claude cost:** Sonnet 4.6 runs ~$0.05-$0.10 per 15-page OM with prompt
  caching on the system prompt + tool schema.

---

## Troubleshooting

- **`The "HOME" variable is not set`** (compose warning) — You're on
  Windows and `$HOME` isn't an env var. The compose file now uses
  `${USERPROFILE}` where it's needed; if you still see this after pulling,
  re-check `docker-compose.yml`.
- **`DefaultAzureCredential failed`** at blob upload — `.env` is missing
  or has empty `AZURE_CLIENT_ID` / `AZURE_CLIENT_SECRET` / `AZURE_TENANT_ID`.
  Run `docker compose exec app env | findstr AZURE` to confirm they're
  making it into the container.
- **`invalid input value for enum building_type`** — the ORM enum serialization
  drifted from the DB enum values. `db.py` uses
  `values_callable=lambda e: [m.value for m in e]` on each Enum column; if
  that line is missing, SQLAlchemy sends `"MULTIFAMILY"` and Postgres expects
  `"multifamily"`.
- **`TypeError: Client.__init__() got an unexpected keyword argument 'proxies'`** —
  `httpx` 0.28+ dropped the `proxies` kwarg that older `anthropic` SDKs
  still pass. `requirements.txt` pins `httpx<0.28` to sidestep this; bump
  `anthropic` and drop the pin when you have time.
- **Migration fails with `type "building_type" already exists`** — the
  initial migration uses raw `op.execute` SQL specifically to avoid
  SQLAlchemy's enum event machinery, which has repeatedly double-fired
  CREATE TYPE across versions. If you add future migrations, prefer
  `op.execute` for enum changes too.

---

## Known gaps / future work

- No edit UI for extracted property fields (only **Reviewed** and **Delete**
  in v1). Add a form-style editor when human-in-the-loop corrections
  become common.
- No user identity. Uploads are anonymous — there is no login and no
  per-uploader attribution. If identity ever becomes a requirement,
  inject it via a reverse-proxy header (e.g. `X-Forwarded-User`) rather
  than re-adding a client-side selector.
- No test suite yet. `pytest` + a compose-spawned Postgres + mocked Claude
  and Google clients is the expected shape.
- No background-worker extraction. All Claude/geocoding work runs inline
  on the uploader's session thread. If upload volume ever pushes above
  ~20 concurrent heavy users, move extraction to Celery/RQ.
