"""
Streamlit entry point for the Starwood OM Map.

Flow:
  - Sidebar: upload widget.
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

import html
import logging
import os
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
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
from storage import compute_sha256, get_pdf_url, upload_pdf

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Branding
# ---------------------------------------------------------------------------

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
            /* Upload result cards: chevron toggle + dismiss × sit in the
               two narrow right-hand columns of the card's header row. Both
               buttons use type="secondary" (Streamlit 1.40 doesn't support
               "tertiary"); the chevron is overridden below with transparent
               ghost styling to match the sidebar collapse chevron, and the
               × keeps secondary behavior (neutral at rest, red on hover,
               matching the upload button). The st-key-toggle_* and
               st-key-close_* classes come from the button's `key` argument. */
            section[data-testid="stSidebar"] div[class*="st-key-upload_card_"] .stButton > button {{
                padding: 0 0.3rem !important;
                min-width: 0 !important;
                min-height: 1.5rem !important;
                height: 1.5rem !important;
                line-height: 1 !important;
                font-size: 13px !important;
                font-weight: 600 !important;
            }}
            section[data-testid="stSidebar"] div[class*="st-key-upload_card_"]
                [data-testid="stHorizontalBlock"] {{
                gap: 0.2rem !important;
            }}
            /* Chevron: strip border + background so it reads as a minimal
               glyph, like the sidebar's own collapse chevron. */
            section[data-testid="stSidebar"] div[class*="st-key-toggle_"] button {{
                background: transparent !important;
                border: none !important;
                box-shadow: none !important;
                color: #6B6B6B !important;
            }}
            section[data-testid="stSidebar"] div[class*="st-key-toggle_"] button:hover {{
                background: rgba(15, 37, 68, 0.06) !important;
                color: {BRAND_NAVY} !important;
                border: none !important;
            }}
            section[data-testid="stSidebar"] div[class*="st-key-toggle_"] button:focus,
            section[data-testid="stSidebar"] div[class*="st-key-toggle_"] button:active {{
                box-shadow: none !important;
                outline: none !important;
            }}
            .sw-card-label {{
                font-size: 10px;
                font-weight: 600;
                letter-spacing: 0.08em;
                text-transform: uppercase;
            }}
            .sw-card-filename {{
                font-size: 13px;
                font-weight: 500;
                color: {BRAND_NAVY};
                word-break: break-word;
                margin-top: 2px;
            }}
        </style>
        """,
        unsafe_allow_html=True,
    )


def _render_header() -> None:
    st.markdown(
        f"""
        <div class="sw-header">
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

# Concurrent per-file pipelines. Claude extraction is the dominant cost
# (~10-30s per 100-page OM); 6 in-flight keeps wall time low without
# bumping into Anthropic rate limits on typical paid tiers.
_UPLOAD_WORKERS = 6


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
            "details": [
                f"Duplicate of a file uploaded {existing.upload_timestamp:%Y-%m-%d}",
            ],
        }

    try:
        extraction = extract_property(data, filename)
    except ExtractionError as exc:
        logger.warning("Extraction failed for %s: %s", filename, exc)
        return {
            "status": "failed",
            "filename": filename,
            "details": [f"Extraction: {exc}"],
        }
    except Exception as exc:
        logger.exception("Unexpected error extracting %s", filename)
        return {
            "status": "failed",
            "filename": filename,
            "details": [f"Unexpected: {exc}"],
        }

    try:
        blob_path = upload_pdf(data, sha256, filename)
    except Exception as exc:
        logger.exception("Blob upload failed for %s", filename)
        return {
            "status": "failed",
            "filename": filename,
            "details": [f"Blob upload: {exc}"],
        }

    geo = _geocode_or_failed(extraction.address)

    prop = Property(
        filename=filename,
        sha256_hash=sha256,
        file_size_bytes=len(data),
        pdf_blob_path=blob_path,
        address=extraction.address,
        building_type=extraction.building_type,
        square_footage=extraction.square_footage,
        cap_rate=extraction.cap_rate,
        valuation=extraction.valuation,
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
            "details": ["Raced with a concurrent duplicate upload"],
        }
    except Exception as exc:
        logger.exception("DB insert failed for %s", filename)
        return {
            "status": "failed",
            "filename": filename,
            "details": [f"DB insert: {exc}"],
        }

    return {
        "status": "success",
        "filename": filename,
        "property_id": new_id,
        "details": _success_details(new_id, extraction, geo),
    }


def _success_details(property_id: int, extraction, geo: GeocodeResult) -> list[str]:
    lines: list[str] = [extraction.address or "No address extracted"]

    metrics: list[str] = []
    if extraction.building_type is not None:
        metrics.append(extraction.building_type.value)
    if extraction.square_footage:
        metrics.append(f"{extraction.square_footage:,} sq ft")
    if extraction.cap_rate is not None:
        metrics.append(f"{extraction.cap_rate:.2f}% cap")
    if extraction.valuation:
        metrics.append(f"${extraction.valuation:,}")
    if metrics:
        lines.append(" · ".join(metrics))

    if geo.latitude is not None and geo.longitude is not None:
        lines.append(f"Geocoded: {geo.latitude:.4f}, {geo.longitude:.4f}")
    elif geo.error:
        lines.append(f"Geocode: {geo.error}")

    if extraction.needs_review or geo.needs_review:
        lines.append(
            "Flagged for review"
            + (f": {extraction.review_reason}" if extraction.review_reason else "")
        )

    lines.append(f"Property #{property_id}")
    return lines


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
        _render_upload_cards()


def _render_uploader() -> None:
    with st.form("upload_form", clear_on_submit=True):
        uploaded = st.file_uploader(
            "Drop Offering Memoranda (PDFs)",
            type=["pdf"],
            accept_multiple_files=True,
            label_visibility="collapsed",
        )
        submitted = st.form_submit_button(
            "Upload and process", use_container_width=True
        )

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
        with ThreadPoolExecutor(max_workers=_UPLOAD_WORKERS) as pool:
            futures = [pool.submit(_process_upload, f) for f in files]
            try:
                for future in as_completed(futures):
                    result = future.result()
                    summary[result["status"]] += 1
                    name = result["filename"]
                    st.write(f"**{name}**")
                    label = _STATUS_LABELS.get(
                        result["status"], (result["status"].title(),)
                    )[0]
                    st.write(f"- {label}")
                    for detail in result.get("details", []):
                        st.write(f"  - {detail}")
                    if result["status"] == "skipped":
                        first = (result.get("details") or ["duplicate"])[0]
                        st.toast(f"Skipped {name}: {first}", icon="⚠")
                    _push_upload_card(result)
            except GeocoderConfigError:
                # Config error affects every file; cancel anything not yet
                # started so we don't burn Claude calls on doomed uploads.
                # (In-flight futures can't be cancelled and will finish.)
                for pending in futures:
                    pending.cancel()
                raise
        status.update(
            label=(
                f"Done — {summary['success']} imported, "
                f"{summary['skipped']} skipped, {summary['failed']} failed"
            ),
            state="complete",
        )


# ---------------------------------------------------------------------------
# Upload result cards (sidebar queue, dismiss-on-X)
# ---------------------------------------------------------------------------

# Newest-first queue of finished uploads. Entries persist across reruns until
# the user clicks the × on a card; the × is CSS-hidden until the card is
# hovered (or focus enters via keyboard), keeping the sidebar uncluttered.
_UPLOAD_CARDS_KEY = "upload_cards"

_STATUS_LABELS = {
    "success": ("Imported", "#2E8B57"),
    "skipped": ("Skipped", "#B8860B"),
    "failed": ("Failed", "#B22222"),
}


def _push_upload_card(result: dict) -> None:
    cards = st.session_state.setdefault(_UPLOAD_CARDS_KEY, [])
    cards.insert(
        0,
        {
            "id": uuid.uuid4().hex,
            "filename": result["filename"],
            "status": result["status"],
            "details": list(result.get("details", [])),
            "expanded": False,
        },
    )


def _dismiss_upload_card(card_id: str) -> None:
    cards = st.session_state.get(_UPLOAD_CARDS_KEY, [])
    st.session_state[_UPLOAD_CARDS_KEY] = [c for c in cards if c["id"] != card_id]


def _toggle_upload_card(card_id: str) -> None:
    for card in st.session_state.get(_UPLOAD_CARDS_KEY, []):
        if card["id"] == card_id:
            card["expanded"] = not card.get("expanded", False)
            return


@st.fragment
def _render_upload_cards() -> None:
    """
    Fragment-scoped so dismissing a card reruns only this block — the map and
    summary table on the main page are not re-executed. Streamlit reruns a
    fragment whenever a widget inside it is interacted with; the explicit
    ``st.rerun(scope="fragment")`` below is needed because the button we pressed
    is already rendered when the dismiss handler fires, so we force one more
    fragment-only pass to redraw without the removed card.
    """
    cards = st.session_state.get(_UPLOAD_CARDS_KEY, [])
    if not cards:
        return
    for card in list(cards):
        _render_upload_card(card)


def _render_upload_card(card: dict) -> None:
    label, color = _STATUS_LABELS.get(card["status"], (card["status"], "#6B6B6B"))
    expanded = bool(card.get("expanded", False))
    with st.container(border=True, key=f"upload_card_{card['id']}"):
        head, chevron_col, close_col = st.columns(
            [6, 1, 1], vertical_alignment="top"
        )
        with head:
            st.markdown(
                f'<div class="sw-card-label" style="color:{color};">{label}</div>'
                f'<div class="sw-card-filename">{html.escape(card["filename"])}</div>',
                unsafe_allow_html=True,
            )
        with chevron_col:
            if st.button(
                "▾" if expanded else "▴",
                key=f"toggle_{card['id']}",
                help="Hide details" if expanded else "Show details",
                type="secondary",
            ):
                _toggle_upload_card(card["id"])
                st.rerun(scope="fragment")
        with close_col:
            if st.button(
                "×",
                key=f"close_{card['id']}",
                help="Dismiss",
                type="secondary",
            ):
                _dismiss_upload_card(card["id"])
                st.rerun(scope="fragment")
        if expanded and card.get("details"):
            st.markdown(
                "".join(
                    f'<div class="sw-caption">• {html.escape(d)}</div>'
                    for d in card["details"]
                ),
                unsafe_allow_html=True,
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
            meta = f"{prop.filename} · uploaded {prop.upload_timestamp:%Y-%m-%d %H:%M}"
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
            if st.button(
                "Reviewed", key=f"review_ok_{prop.id}", use_container_width=True
            ):
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
            "Cap rate": f"{p.cap_rate:.2f}%" if p.cap_rate is not None else None,
            "Valuation": f"${p.valuation:,}" if p.valuation is not None else None,
            "Needs review": p.needs_review,
            "PDF": _pdf_url_or_none(p.pdf_blob_path),
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

    st.dataframe(
        df,
        use_container_width=True,
        hide_index=True,
        column_config={
            "PDF": st.column_config.LinkColumn(
                "PDF", display_text="PDF Link", width="small"
            ),
        },
    )


def _pdf_url_or_none(blob_path: Optional[str]) -> Optional[str]:
    if not blob_path:
        return None
    try:
        return get_pdf_url(blob_path)
    except Exception:
        logger.exception("Failed to build PDF URL for blob %s", blob_path)
        return None


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
