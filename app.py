"""
Streamlit entry point for the Starwood OM Map.

Flow:
  - Sidebar: logo, upload widget.
  - Main area (in order): review queue (if non-empty), map, summary table.

Concurrency model:
  - `load_properties()` is cached with ttl=30 and shared across sessions; it is
    explicitly cleared after any mutation (insert, mark-reviewed, soft-delete)
    so the mutating session sees changes instantly. Other sessions see them
    within the TTL.
"""

from __future__ import annotations

# dotenv must load before any module that reads env at import time (e.g. db).
from dotenv import load_dotenv

load_dotenv()

import base64
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd
import streamlit as st
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from streamlit_folium import st_folium

from db import (
    BuildingType,
    ExtractionStatus,
    GeocodeStatus,
    Property,
    get_session,
)
from extractor import ExtractionError, extract_property
from geocoder import GeocodeResult, GeocoderConfigError, geocode
from map_builder import (
    BUILDING_TYPE_COLORS,
    build_map,
    filter_review_queue,
)
from storage import compute_sha256, upload_pdf

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Branding
# ---------------------------------------------------------------------------

# Embed the logo as a base64 data URI. Streamlit's static file handler serves
# .svg as text/plain with nosniff, so <img src="app/static/...svg"> fails
# silently in the browser. Inlining bypasses the HTTP route entirely.
_LOGO_BYTES = (Path(__file__).parent / "static" / "StarwoodCapitalLogo.svg").read_bytes()
STARWOOD_LOGO_DATA_URI = (
    f"data:image/svg+xml;base64,{base64.b64encode(_LOGO_BYTES).decode('ascii')}"
)
# Conservative corporate navy palette. Adjust to the official brand guide when
# available — these three variables drive every branded accent in the app.
BRAND_NAVY = "#0F2544"
BRAND_ACCENT = "#C9A449"
BRAND_LIGHT = "#F5F3EF"


# ---------------------------------------------------------------------------
# Page config + theme
# ---------------------------------------------------------------------------


def _configure_page() -> None:
    st.set_page_config(
        page_title="Starwood OM Map",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    st.markdown(
        f"""
        <style>
            .stApp {{
                background-color: {BRAND_LIGHT};
            }}
            header[data-testid="stHeader"],
            header.stAppHeader,
            div[data-testid="stToolbar"],
            div[data-testid="stDecoration"],
            div[data-testid="stStatusWidget"],
            #MainMenu {{
                display: none !important;
                visibility: hidden !important;
                height: 0 !important;
            }}
            .stApp > div:first-child {{ padding-top: 0 !important; }}
            .block-container {{ padding-top: 1rem !important; }}
            section[data-testid="stSidebar"] {{
                background-color: #FFFFFF;
                border-right: 1px solid #E5E1D8;
            }}
            h1, h2, h3 {{ color: {BRAND_NAVY}; }}
            .sw-header {{
                background: {BRAND_NAVY};
                color: #FFFFFF;
                padding: 14px 24px;
                border-radius: 6px;
                margin-bottom: 18px;
                display: flex;
                align-items: center;
                gap: 18px;
            }}
            .sw-header h1 {{
                color: #FFFFFF;
                font-size: 20px;
                font-weight: 500;
                margin: 0;
                letter-spacing: 0.04em;
                text-transform: uppercase;
            }}
            .sw-header .sw-accent {{
                width: 2px; height: 40px; background: {BRAND_ACCENT};
            }}
            .sw-caption {{
                color: #6B6B6B; font-size: 12px;
            }}
        </style>
        """,
        unsafe_allow_html=True,
    )


def _render_header() -> None:
    st.markdown(
        f"""
        <div class="sw-header">
            <img src="{STARWOOD_LOGO_DATA_URI}" height="48" alt="Starwood Capital" />
            <div class="sw-accent"></div>
            <h1>Offering Memorandum Map</h1>
        </div>
        """,
        unsafe_allow_html=True,
    )


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------


@st.cache_data(ttl=30)
def load_properties() -> list[Property]:
    """
    Fetch all non-deleted properties. Detached from the session so the cached
    list can be reused across Streamlit reruns without SQLAlchemy errors.
    """
    with get_session() as session:
        stmt = (
            select(Property)
            .where(Property.deleted_at.is_(None))
            .order_by(Property.upload_timestamp.desc())
        )
        rows = list(session.scalars(stmt).all())
        session.expunge_all()
    return rows


# ---------------------------------------------------------------------------
# Upload pipeline
# ---------------------------------------------------------------------------


def _find_duplicate(sha256: str) -> Optional[Property]:
    with get_session() as session:
        existing = session.scalars(
            select(Property).where(Property.sha256_hash == sha256)
        ).first()
        if existing is not None:
            session.expunge(existing)
        return existing


def _process_upload(file) -> dict:
    """
    Run one file through the full pipeline. Never raises for per-file
    failures — returns a result dict the caller uses to render status.

    Stages (ordered so the cheapest / most diagnostic step runs first):
      1. Hash + dedup check
      2. Claude extraction (most common failure mode; fail fast before blob I/O)
      3. Azure Blob upload
      4. Google Maps geocoding
      5. Insert row
    """
    filename = file.name
    data = file.getvalue()
    sha256 = compute_sha256(data)

    existing = _find_duplicate(sha256)
    if existing is not None:
        return {
            "status": "skipped",
            "filename": filename,
            "reason": (
                f"duplicate of a file uploaded "
                f"{existing.upload_timestamp:%Y-%m-%d}"
            ),
        }

    try:
        extraction = extract_property(data, filename)
    except ExtractionError as exc:
        logger.warning("Extraction failed for %s: %s", filename, exc)
        return {"status": "failed", "filename": filename, "reason": f"extraction: {exc}"}
    except Exception as exc:
        logger.exception("Unexpected error extracting %s", filename)
        return {"status": "failed", "filename": filename, "reason": f"unexpected: {exc}"}

    try:
        blob_path = upload_pdf(data, sha256, filename)
    except Exception as exc:
        logger.exception("Blob upload failed for %s", filename)
        return {"status": "failed", "filename": filename, "reason": f"blob: {exc}"}

    geo = _geocode_or_failed(extraction.address)

    prop = Property(
        filename=filename,
        sha256_hash=sha256,
        file_size_bytes=len(data),
        pdf_blob_path=blob_path,
        address=extraction.address,
        building_type=extraction.building_type,
        square_footage=extraction.square_footage,
        latitude=geo.latitude,
        longitude=geo.longitude,
        extraction_status=ExtractionStatus.SUCCESS,
        extraction_error=extraction.review_reason if extraction.needs_review else None,
        geocode_status=geo.status,
        geocode_error=geo.error,
        needs_review=extraction.needs_review or geo.needs_review,
    )
    try:
        with get_session() as session:
            session.add(prop)
            session.flush()
            new_id = prop.id
    except IntegrityError:
        # Lost the race against a concurrent upload of the same PDF.
        return {
            "status": "skipped",
            "filename": filename,
            "reason": "raced with a concurrent duplicate upload",
        }
    except Exception as exc:
        logger.exception("DB insert failed for %s", filename)
        return {"status": "failed", "filename": filename, "reason": f"db: {exc}"}

    return {"status": "success", "filename": filename, "property_id": new_id}


def _geocode_or_failed(address: Optional[str]) -> GeocodeResult:
    """Wrap geocode() so per-call transport errors don't abort the upload batch.

    GeocoderConfigError (bad API key etc.) is re-raised because it affects
    every subsequent file and should surface at the top level.
    """
    try:
        return geocode(address)
    except GeocoderConfigError:
        raise
    except Exception as exc:
        logger.exception("Unexpected geocode error for %r", address)
        return GeocodeResult(
            status=GeocodeStatus.FAILED,
            latitude=None,
            longitude=None,
            needs_review=False,
            error=f"unexpected: {exc}",
        )


# ---------------------------------------------------------------------------
# Mutation helpers (review actions)
# ---------------------------------------------------------------------------


def _mark_reviewed(property_id: int) -> None:
    with get_session() as session:
        session.execute(
            update(Property)
            .where(Property.id == property_id)
            .values(needs_review=False)
        )
    load_properties.clear()


def _soft_delete(property_id: int) -> None:
    with get_session() as session:
        session.execute(
            update(Property)
            .where(Property.id == property_id)
            .values(deleted_at=datetime.utcnow())
        )
    load_properties.clear()


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------


def _render_sidebar() -> None:
    with st.sidebar:
        st.subheader("Upload OMs")
        _render_uploader()


def _render_uploader() -> None:
    with st.form("upload_form", clear_on_submit=True):
        uploaded = st.file_uploader(
            "Drop Offering Memoranda (PDFs)",
            type=["pdf"],
            accept_multiple_files=True,
            label_visibility="collapsed",
        )
        submitted = st.form_submit_button("Upload and process", use_container_width=True)

    if not submitted or not uploaded:
        return

    try:
        _run_batch(uploaded)
    except GeocoderConfigError as exc:
        st.error(f"Geocoding is unavailable: {exc}")
        return

    load_properties.clear()
    st.rerun()


def _run_batch(files: list) -> None:
    with st.status(f"Processing {len(files)} file(s)…", expanded=True) as status:
        summary = {"success": 0, "skipped": 0, "failed": 0}
        for f in files:
            st.write(f"**{f.name}**")
            result = _process_upload(f)
            summary[result["status"]] += 1
            if result["status"] == "success":
                st.write("- imported")
            elif result["status"] == "skipped":
                st.write(f"- skipped: {result['reason']}")
                st.toast(f"Skipped {f.name}: {result['reason']}", icon="⚠")
            else:
                st.write(f"- failed: {result['reason']}")
        status.update(
            label=(
                f"Done — {summary['success']} imported, "
                f"{summary['skipped']} skipped, {summary['failed']} failed"
            ),
            state="complete",
        )


# ---------------------------------------------------------------------------
# Review queue
# ---------------------------------------------------------------------------


def _render_review_queue(properties: list[Property]) -> None:
    queue = filter_review_queue(properties)
    if not queue:
        return
    with st.expander(f"Review queue — {len(queue)} item(s)", expanded=True):
        st.caption(
            "Properties flagged for human verification: low-confidence extraction, "
            "low-quality geocode, or missing coordinates."
        )
        for prop in queue:
            _render_review_row(prop)


def _render_review_row(prop: Property) -> None:
    with st.container(border=True):
        left, right = st.columns([5, 1])
        with left:
            st.markdown(f"**{prop.address or '(no address extracted)'}**")
            meta = (
                f"{prop.filename} · uploaded "
                f"{prop.upload_timestamp:%Y-%m-%d %H:%M}"
            )
            st.markdown(f'<div class="sw-caption">{meta}</div>', unsafe_allow_html=True)
            reasons: list[str] = []
            if prop.extraction_error:
                reasons.append(f"extraction: {prop.extraction_error}")
            if prop.geocode_error:
                reasons.append(f"geocode: {prop.geocode_error}")
            if prop.latitude is None or prop.longitude is None:
                reasons.append("no coordinates")
            if reasons:
                st.markdown(
                    f'<div class="sw-caption">{" · ".join(reasons)}</div>',
                    unsafe_allow_html=True,
                )
        with right:
            if st.button("Reviewed", key=f"review_ok_{prop.id}", use_container_width=True):
                _mark_reviewed(prop.id)
                st.rerun()
            if st.button(
                "Delete",
                key=f"review_del_{prop.id}",
                use_container_width=True,
                type="secondary",
            ):
                _soft_delete(prop.id)
                st.rerun()


# ---------------------------------------------------------------------------
# Map
# ---------------------------------------------------------------------------


def _render_map(properties: list[Property]) -> None:
    fmap = build_map(properties)
    # returned_objects=[] stops st_folium from triggering reruns on pan/zoom/click.
    st_folium(fmap, height=600, use_container_width=True, returned_objects=[])


# ---------------------------------------------------------------------------
# Summary table
# ---------------------------------------------------------------------------


def _render_summary_table(properties: list[Property]) -> None:
    st.subheader("All properties")

    rows = [
        {
            "Address": p.address or "(none)",
            "Type": p.building_type.value if p.building_type else "unknown",
            "Square ft": p.square_footage,
            "Uploaded": p.upload_timestamp.strftime("%Y-%m-%d %H:%M"),
            "Needs review": p.needs_review,
        }
        for p in properties
    ]
    if not rows:
        st.info("No properties yet. Upload some OMs to get started.")
        return

    df = pd.DataFrame(rows)

    c1, c2, c3 = st.columns([2, 1, 3])
    with c1:
        available_types = sorted({r["Type"] for r in rows})
        type_filter = st.multiselect("Building type", options=available_types)
    with c2:
        review_only = st.checkbox("Needs review only")
    with c3:
        search = st.text_input("Search address", placeholder="e.g. 123 Main")

    if type_filter:
        df = df[df["Type"].isin(type_filter)]
    if review_only:
        df = df[df["Needs review"]]
    if search:
        df = df[df["Address"].str.contains(search, case=False, na=False)]

    st.dataframe(df, use_container_width=True, hide_index=True)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    _configure_page()
    _render_header()
    _render_sidebar()

    properties = load_properties()
    _render_review_queue(properties)
    _render_map(properties)
    _render_summary_table(properties)


if __name__ == "__main__":
    main()
