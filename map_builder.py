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

logger = logging.getLogger(__name__)


# ColorBrewer Set1 — qualitative palette designed for categorical data with
# ~8 distinct classes. `None` (unknown building type) gets a neutral gray.
BUILDING_TYPE_COLORS: dict[Optional[BuildingType], str] = {
    BuildingType.OFFICE: "#377eb8",       # blue
    BuildingType.RESIDENTIAL: "#4daf4a",  # green
    BuildingType.RETAIL: "#e41a1c",       # red
    BuildingType.INDUSTRIAL: "#984ea3",   # purple
    BuildingType.MIXED_USE: "#ff7f00",    # orange
    BuildingType.HOSPITALITY: "#a65628",  # brown
    BuildingType.MULTIFAMILY: "#f781bf",  # pink
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

    cluster = MarkerCluster(name="Properties").add_to(fmap)
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
    popup_html = _popup_html(prop)
    folium.CircleMarker(
        location=(prop.latitude, prop.longitude),
        radius=7,
        color=color,
        weight=1,
        fill=True,
        fill_color=color,
        fill_opacity=0.85,
        popup=folium.Popup(popup_html, max_width=320),
        tooltip=prop.address or prop.filename,
    ).add_to(cluster)


def _popup_html(prop: Property) -> str:
    """HTML for the marker popup. All dynamic fields are escape-safed."""
    address = html.escape(prop.address or "(address not extracted)")
    building = html.escape(_BUILDING_TYPE_LABELS.get(prop.building_type, "Unknown"))
    sqft = f"{prop.square_footage:,}" if prop.square_footage else "—"
    uploaded_on = prop.upload_timestamp.strftime("%Y-%m-%d")

    review_badge = ""
    if prop.needs_review:
        review_badge = (
            '<div style="margin-top:6px;display:inline-block;padding:2px 8px;'
            'background:#f59e0b;color:#fff;border-radius:4px;font-size:11px;'
            'font-weight:600;letter-spacing:0.04em;">NEEDS REVIEW</div>'
        )

    return f"""
    <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; min-width: 220px;">
        <div style="font-weight:600;font-size:14px;margin-bottom:4px;">{address}</div>
        <div style="font-size:12px;color:#444;line-height:1.5;">
            <div><b>Type:</b> {building}</div>
            <div><b>SF:</b> {sqft}</div>
            <div><b>Uploaded:</b> {uploaded_on}</div>
        </div>
        {review_badge}
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
