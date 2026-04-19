# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Internal Starwood Capital Streamlit app. Employees upload real estate Offering Memorandum PDFs; Claude Sonnet 4.6 extracts address / building_type / square_footage; Google Maps geocodes the address; everything lands in Postgres with the raw PDF in Azure Blob; a shared Folium map shows all properties as color-coded markers. See `README.md` for setup, `Notes.md` for the original product brief.

## Common commands

Local dev with Docker (default path):
```bash
docker compose up --build
docker compose run --rm app alembic upgrade head     # first run / after schema changes
```

Bare-metal dev:
```bash
pip install -r requirements.txt
alembic upgrade head
streamlit run app.py
```

New migration after a schema change:
```bash
alembic revision --autogenerate -m "describe change"
alembic upgrade head
```

## Architecture

The app is a single Streamlit process with these modules, each with a tight public surface:

- `app.py` — entry point. Owns the upload pipeline (dedup → extract → blob → geocode → insert), the review queue, the map, and the summary table. The *only* module with Streamlit calls.
- `db.py` — SQLAlchemy 2.x ORM (`Property`), three native Postgres enums (`building_type`, `extraction_status`, `geocode_status`), and a `get_session()` context manager every caller uses.
- `storage.py` — Azure Blob upload/download keyed by SHA-256. Uses `DefaultAzureCredential` so one code path covers local dev (`az login`) and prod (managed identity).
- `extractor.py` — Claude Sonnet 4.6 via tool use with `cache_control: ephemeral` on both system prompt and tool schema. `pypdf` preflight enforces the 32MB / 100-page Claude limits before the API call.
- `geocoder.py` — Google Maps only (Nominatim was dropped for ToS reasons). Low-quality matches (`APPROXIMATE`, `GEOMETRIC_CENTER`, `partial_match`) set `needs_review=True`.
- `map_builder.py` — Folium map with `MarkerCluster` + `CircleMarker`s using ColorBrewer Set1. Also owns `filter_review_queue()`.
- `auth.py` — `get_current_user()` seam. Currently a sidebar selectbox fed from `APP_USERS`; will swap to a reverse-proxy header without changing callers.

### Data flow constants to preserve

- **SHA-256 of file bytes is the dedup key** everywhere — DB unique index, Blob name, in-memory check. `filename + size` is *not* used; don't reintroduce it.
- **Soft delete only** (`deleted_at` timestamp). Every read query filters `deleted_at IS NULL`.
- **Pipeline order**: extract → blob → geocode → insert. Extraction runs first because it's the most likely failure and the cheapest to unwind (no storage side effects). Blob upload is idempotent on its `{sha256}.pdf` name, so retries are safe.
- **Cache strategy**: `load_properties()` is `@st.cache_data(ttl=30)`. After every DB mutation in the uploader's session, call `load_properties.clear()` + `st.rerun()` so the uploader sees their change immediately. Other sessions see it within 30s via TTL.
- **`load_properties()` returns detached ORM instances** (`session.expunge_all()`). They must have no relationships requiring lazy loading — if you add one, either eager-load it or switch to a DTO.
- **`needs_review` is OR-ed** across extractor and geocoder signals. Server-side sanity checks in `extractor._build_result` can override Claude's self-report upward, never downward.

### Conventions

- One blank `__init__.py`-free layout: every module sits at the repo root and imports directly (`from db import ...`).
- `load_dotenv()` must run before any first-party import, because `db` builds its engine at import time.
- Log via `logging`, never `print`. Each module has its own `logger = logging.getLogger(__name__)`.
- No emojis in code or UI. The single exception is `st.toast(..., icon="⚠")` in `app.py` — Streamlit's toast `icon` kwarg only accepts emoji.
- Pin versions with `~=` in `requirements.txt` so patch updates flow but minors are explicit.
- Environment target: Python 3.12 (set in `Dockerfile`). Code uses PEP 604 / PEP 585 generics.

### Decisions worth remembering before you "fix" them

- Lat/long is **not** extracted by Claude — OMs rarely state it. Always geocode the address.
- Nominatim is **not** a fallback — intentionally dropped.
- Building types are a **seven-value Postgres enum** (`office`, `residential`, `retail`, `industrial`, `mixed_use`, `hospitality`, `multifamily`). Adding a value is an Alembic migration, not a code-only change.
- PDF storage is **Azure Blob, not Postgres BYTEA** — don't "simplify" by moving bytes into the DB.

## Current gaps (don't silently fill these)

- No test suite yet. If you add one, `pytest` + `pytest-asyncio` + a fixture that spins an ephemeral Postgres is the expected shape.
- No edit UI for extracted fields. Only `Reviewed` and `Delete` actions exist on the review queue.
- Alembic migrations are hand-written so far (`0001_initial_schema.py`). Autogenerate should work for future changes — `env.py` has `compare_type=True, compare_server_default=True`.
