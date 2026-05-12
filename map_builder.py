"""
Folium map construction.

Public surface:
  - `BUILDING_TYPE_COLORS` — the single source of truth for the palette, so
    the legend, the map markers, and any future chart all use the same hex.
  - `build_map(properties)` — returns a Folium Map with one CircleMarker per
    located property, wrapped in a MarkerCluster, auto-fit to the data.
  - `filter_review_queue(properties)` — the subset needing human attention.

Design notes:
  - `CircleMarker` (not `Icon`) so we can use ColorBrewer Set1 hexes directly.
    Folium's pin icons only accept a fixed named-color palette that doesn't
    line up with ColorBrewer.
  - MarkerCluster (not FastMarkerCluster) — slightly slower to render but
    keeps per-marker popup customization, which we need for property details.
    FastMarkerCluster is the upgrade path if render time becomes a problem.
  - Review queue predicate is permissive: anything with needs_review=True,
    extraction failed, or no coordinates. A property with a clean extraction
    and clean geocode should be the only thing that does NOT appear in the
    queue — reflecting that the queue is "things a human should look at."
"""

from __future__ import annotations

import html
import logging
from typing import Iterable, Optional

import folium
from folium.plugins import MarkerCluster

from db import BuildingType, ExtractionStatus, GeocodeStatus, Property
from storage import get_pdf_url
from streetview import streetview_image_url

logger = logging.getLogger(__name__)


# Tableau 10 — qualitative palette tuned for BI dashboards; softer than
# ColorBrewer Set1 but still high-contrast. `None` (unknown) gets a neutral gray.
BUILDING_TYPE_COLORS: dict[Optional[BuildingType], str] = {
    BuildingType.OFFICE: "#4e79a7",       # blue
    BuildingType.RESIDENTIAL: "#59a14f",  # green
    BuildingType.RETAIL: "#76b7b2",       # teal
    BuildingType.INDUSTRIAL: "#b07aa1",   # purple
    BuildingType.MIXED_USE: "#f28e2b",    # orange
    BuildingType.HOSPITALITY: "#edc948",  # gold
    BuildingType.MULTIFAMILY: "#e15759",  # red
    None: "#999999",                       # gray (unknown)
}

_BUILDING_TYPE_LABELS: dict[Optional[BuildingType], str] = {
    BuildingType.OFFICE: "Office",
    BuildingType.RESIDENTIAL: "Residential",
    BuildingType.RETAIL: "Retail",
    BuildingType.INDUSTRIAL: "Industrial",
    BuildingType.MIXED_USE: "Mixed use",
    BuildingType.HOSPITALITY: "Hospitality",
    BuildingType.MULTIFAMILY: "Multifamily",
    None: "Unknown",
}

# Map defaults when the dataset has no located properties yet.
_EMPTY_MAP_CENTER = (20.0, 0.0)
_EMPTY_MAP_ZOOM = 2


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_map(properties: Iterable[Property]) -> folium.Map:
    """
    Build a Folium map with markers for every located property.

    Properties without lat/lng are skipped silently — they surface via
    `filter_review_queue` instead. The map auto-fits to the markers it does
    have, or falls back to a world view when there are none.
    """
    located = [p for p in properties if p.latitude is not None and p.longitude is not None]

    fmap = folium.Map(
        location=_EMPTY_MAP_CENTER,
        zoom_start=_EMPTY_MAP_ZOOM,
        tiles="OpenStreetMap",
        control_scale=True,
    )

    cluster = MarkerCluster(
        name="Properties",
        options={"disableClusteringAtZoom": 3, "spiderfyOnMaxZoom": False},
    ).add_to(fmap)
    for prop in located:
        _add_marker(cluster, prop)

    if located:
        bounds = [
            [min(p.latitude for p in located), min(p.longitude for p in located)],
            [max(p.latitude for p in located), max(p.longitude for p in located)],
        ]
        fmap.fit_bounds(bounds, padding=(30, 30))

    _inject_legend(fmap)
    return fmap


def filter_review_queue(properties: Iterable[Property]) -> list[Property]:
    """Return properties that need human attention, newest first."""
    queue = [p for p in properties if _needs_attention(p)]
    queue.sort(key=lambda p: p.upload_timestamp, reverse=True)
    return queue


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _needs_attention(prop: Property) -> bool:
    if prop.deleted_at is not None:
        return False
    if prop.needs_review:
        return True
    if prop.extraction_status == ExtractionStatus.FAILED:
        return True
    if prop.latitude is None or prop.longitude is None:
        return True
    if prop.geocode_status in (GeocodeStatus.FAILED, GeocodeStatus.SKIPPED):
        return True
    return False


def _add_marker(cluster: MarkerCluster, prop: Property) -> None:
    color = BUILDING_TYPE_COLORS.get(prop.building_type, BUILDING_TYPE_COLORS[None])
    pdf_url: Optional[str] = None
    if prop.pdf_blob_path:
        try:
            pdf_url = get_pdf_url(prop.pdf_blob_path)
        except Exception:
            # Don't drop the marker over a bad SAS; the link is a nice-to-have.
            logger.exception("Failed to build PDF URL for property %s", prop.id)
    popup_html = _popup_html(prop, pdf_url)
    folium.CircleMarker(
        location=(prop.latitude, prop.longitude),
        radius=4,
        color=color,
        weight=1,
        fill=True,
        fill_color=color,
        fill_opacity=0.85,
        popup=folium.Popup(popup_html, max_width=320),
        tooltip=prop.address or prop.filename,
    ).add_to(cluster)


def _popup_html(prop: Property, pdf_url: Optional[str] = None) -> str:
    """HTML for the marker popup. All dynamic fields are escape-safed."""
    address = html.escape(prop.address or "(address not extracted)")
    building = html.escape(_BUILDING_TYPE_LABELS.get(prop.building_type, "Unknown"))
    sqft = f"{prop.square_footage:,}" if prop.square_footage else "—"
    cap_rate = f"{prop.cap_rate:.2f}%" if prop.cap_rate is not None else "—"
    valuation = f"${prop.valuation:,}" if prop.valuation is not None else "—"

    review_badge = ""
    if prop.needs_review:
        review_badge = (
            '<div style="margin-top:6px;display:inline-block;padding:2px 8px;'
            'background:#f59e0b;color:#fff;border-radius:4px;font-size:11px;'
            'font-weight:600;letter-spacing:0.04em;">NEEDS REVIEW</div>'
        )

    pdf_link = ""
    if pdf_url:
        safe_url = html.escape(pdf_url, quote=True)
        pdf_link = (
            f'<div style="margin-top:8px;font-size:12px;">'
            f'<a href="{safe_url}" target="_blank" rel="noopener noreferrer">PDF Link</a>'
            f"</div>"
        )

    image_tag = ""
    sv_url = streetview_image_url(prop.latitude, prop.longitude)
    if sv_url:
        safe_sv = html.escape(sv_url, quote=True)
        # onerror hides the <img> when Google returns 404 (no imagery at
        # this location), collapsing the popup cleanly.
        image_tag = (
            f'<img src="{safe_sv}" alt="Street view" '
            f'style="display:block;width:100%;height:auto;border-radius:4px;'
            f'margin-bottom:8px;" '
            f"onerror=\"this.style.display='none'\" />"
        )

    return f"""
    <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; min-width: 220px;">
        {image_tag}
        <div style="font-size:12px;color:#444;line-height:1.5;">
            <div><b>Address:</b> {address}</div>
            <div><b>Type:</b> {building}</div>
            <div><b>SF:</b> {sqft}</div>
            <div><b>Cap rate:</b> {cap_rate}</div>
            <div><b>Valuation:</b> {valuation}</div>
        </div>
        {review_badge}
        {pdf_link}
    </div>
    """


def _inject_legend(fmap: folium.Map) -> None:
    """Pin a small HTML legend to the top-right corner of the map."""
    rows = "".join(
        f'<div style="display:flex;align-items:center;margin:2px 0;">'
        f'<span style="display:inline-block;width:12px;height:12px;border-radius:50%;'
        f'background:{color};margin-right:8px;"></span>'
        f'<span style="font-size:12px;color:#222;">{label}</span>'
        f"</div>"
        for bt, label in _BUILDING_TYPE_LABELS.items()
        for color in [BUILDING_TYPE_COLORS[bt]]
    )
    legend_html = f"""
    <div style="
        position: fixed; top: 12px; right: 12px; z-index: 9999;
        background: rgba(255,255,255,0.95); padding: 10px 12px;
        border: 1px solid #ddd; border-radius: 6px;
        font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
        box-shadow: 0 2px 6px rgba(0,0,0,0.08);">
        <div style="font-size:11px;font-weight:600;letter-spacing:0.06em;
                    text-transform:uppercase;color:#555;margin-bottom:6px;">
            Building Type
        </div>
        {rows}
    </div>
    """
    fmap.get_root().html.add_child(folium.Element(legend_html))
