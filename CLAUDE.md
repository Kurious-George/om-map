# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Internal Starwood Capital Streamlit app. Employees upload real estate Offering Memorandum PDFs; Claude Sonnet 4.6 extracts address / building_type / square_footage / cap_rate / valuation; Google Maps geocodes the address; everything lands in Postgres with the raw PDF in Azure Blob; a shared Folium map shows all properties as color-coded markers with a PDF link in each popup. See `README.md` for setup.

## Common commands

Local dev with Docker (default path):
```bash
docker compose up --build -d
docker compose run --rm app alembic upgrade head     # first run / after schema changes
docker compose logs -f app                            # tail logs
docker compose restart app                            # force-reload after module-level edits
```

The compose file bind-mounts the working tree into the container, so `.py` edits take effect on the next Streamlit rerun without rebuilding. `requirements.txt` changes still need `--build`.

New migration after a schema change:
```bash
alembic revision -m "describe change"                 # prefer --autogenerate when possible
# edit the file, then:
docker compose run --rm app alembic upgrade head
```

## Architecture

Single Streamlit process; each module has a tight public surface:

- `app.py` — entry point. Owns the upload pipeline (dedup → extract → blob → geocode → insert), review queue, map, summary table. **The only module with Streamlit calls.**
- `db.py` — SQLAlchemy 2.x ORM (`Property`), three Postgres enums, `get_session()` context manager.
- `storage.py` — Azure Blob upload/download keyed by SHA-256, plus `get_pdf_url()` which mints a 1-hour user-delegation SAS URL so the UI can link to raw PDFs without exposing account keys. Uses `DefaultAzureCredential`: dev picks up `AZURE_CLIENT_*` env vars (service principal); prod picks up managed identity.
- `extractor.py` — Claude Sonnet 4.6 via tool use with `cache_control: ephemeral` on system prompt AND tool schema. `pypdf` preflight enforces Claude's 32MB / 100-page limits before the API call.
- `geocoder.py` — Google Maps only (Nominatim was dropped for ToS reasons). Low-quality matches (`APPROXIMATE`, `GEOMETRIC_CENTER`, `partial_match`) set `needs_review=True`. Uses the server-side `GOOGLE_MAPS_API_KEY`.
- `streetview.py` — Pure URL builder for the Google Street View Static API. No HTTP call; the URL is rendered in popup `<img>` tags and the browser loads it on demand. Reads `GOOGLE_STREETVIEW_API_KEY` with a fallback to `GOOGLE_MAPS_API_KEY`.
- `map_builder.py` — Folium map with `MarkerCluster` + `CircleMarker`s using the Tableau 10 palette. Popups include a Street View image at the top and a SAS-signed PDF link at the bottom. Also owns `filter_review_queue()` and `BUILDING_TYPE_COLORS`.

### Data flow constants to preserve

- **SHA-256 of file bytes is the dedup key** everywhere — DB unique index, blob name, in-memory check. `filename + size` is *not* used; don't reintroduce it.
- **Soft delete only** (`deleted_at` timestamp). Every read query filters `deleted_at IS NULL`.
- **Pipeline order**: dedup → extract → blob → geocode → insert. Extraction runs first because it's the most likely failure and the cheapest to unwind (no storage side effects). Blob upload is idempotent on its `{sha256}.pdf` name.
- **Cache strategy**: `load_properties()` is `@st.cache_data(ttl=30)`. After every DB mutation in the uploader's session, call `load_properties.clear()` + `st.rerun()` so the uploader sees their change immediately. Other sessions see it within 30s via TTL.
- **`load_properties()` returns detached ORM instances** (`session.expunge_all()`). They must have no relationships requiring lazy loading — if you add one, either eager-load it or switch to a DTO.
- **`needs_review` is OR-ed** across extractor and geocoder signals. Server-side sanity checks in `extractor._build_result` can override Claude's self-report upward, never downward.

### Conventions

- Every module sits at the repo root and imports directly (`from db import ...`).
- `load_dotenv()` must run before any first-party import, because `db` builds its engine at import time.
- Log via `logging`, never `print`. Each module has its own `logger = logging.getLogger(__name__)`.
- No emojis in code or UI. One exception: `st.toast(..., icon="⚠")` in `app.py` — Streamlit's toast `icon` kwarg only accepts emoji.
- Environment target: Python 3.12 (set in `Dockerfile`). Code uses PEP 604 / PEP 585 generics.

### Decisions worth remembering before you "fix" them

- **Lat/long is NOT extracted by Claude** — OMs rarely state it. Always geocode the address.
- **Nominatim is NOT a fallback** — intentionally dropped for OSM ToS reasons.
- **Building types are a seven-value Postgres enum** (`office`, `residential`, `retail`, `industrial`, `mixed_use`, `hospitality`, `multifamily`). Adding a value is an Alembic migration, not a code-only change.
- **PDF storage is Azure Blob, not Postgres BYTEA** — don't "simplify" by moving bytes into the DB.
- **`values_callable=lambda e: [m.value for m in e]`** is required on every `SqlEnum(...)` column in `db.py`. Without it, SQLAlchemy serializes the Python member *name* (`"MULTIFAMILY"`) but Postgres expects the *value* (`"multifamily"`). Don't remove.
- **The initial Alembic migration uses raw `op.execute` SQL** for `CREATE TYPE` / `CREATE TABLE`. This avoids a long-standing SQLAlchemy bug where enum types double-fire CREATE via the `before_create` event (both `sa.Enum(create_type=False)` and `postgresql.ENUM(create_type=False)` have been unreliable across versions). Future enum migrations should continue to prefer `op.execute`.
- **`httpx<0.28` is pinned** in `requirements.txt` because `anthropic` 0.39.0 passes the removed `proxies=` kwarg. Bump `anthropic` and drop the pin when you have time.
- **Two Google Maps keys, not one.** `GOOGLE_MAPS_API_KEY` is server-side (geocoder) and should be IP-restricted; `GOOGLE_STREETVIEW_API_KEY` is embedded in popup `<img src>` and must be HTTP-referrer-restricted. The two Google restriction types are mutually exclusive per key, so collapsing back to one key forces you to either (a) break server calls by referrer-restricting, or (b) leave a client-visible key unrestricted. `streetview.py` falls back to the server key only so the feature keeps working before the split is rolled out — not as a long-term configuration.

### Auth model

- **Local dev:** service principal via `AZURE_CLIENT_ID` / `AZURE_CLIENT_SECRET` / `AZURE_TENANT_ID` in `.env`. `DefaultAzureCredential`'s `EnvironmentCredential` picks these up first. Mounting the Windows `.azure` folder does NOT work (DPAPI-encrypted tokens can't be decrypted on Linux).
- **Production on Azure:** leave `AZURE_CLIENT_*` unset, assign a managed identity to the container, grant it `Storage Blob Data Contributor` on the blob container. `ManagedIdentityCredential` takes over.
- **Dev bind mount:** `docker-compose.yml` mounts `.:/home/app/app`. Remove before deploying to prod — the deployed image should run baked-in code.

## Current gaps (don't silently fill these)

- No test suite. `pytest` + a compose-spawned Postgres + mocked Claude and Google clients is the expected shape.
- No edit UI for extracted fields. Only `Reviewed` and `Delete` actions exist on the review queue.
- No user identity tracking — the app is unauthenticated and uploads are anonymous. If that ever needs to change, it should come in via a reverse-proxy identity header, not a re-added selector.
