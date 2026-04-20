"""
Google Street View Static image URLs.

Single public entry point: `streetview_image_url(lat, lng) -> str | None`.

Design notes:
  - Pure URL builder; no HTTP call. The browser loads the image when the
    marker popup opens, so work is deferred to render time.
  - Prefers GOOGLE_STREETVIEW_API_KEY (a client-side key with HTTP-referrer
    restrictions), falling back to GOOGLE_MAPS_API_KEY so the feature keeps
    working before the key split is rolled out. The geocoder uses the
    server-side key directly and ignores this fallback.
  - `return_error_code=true` makes Google return a 404 for locations with
    no imagery rather than the default "Sorry" placeholder. The popup uses
    an `onerror` handler to hide the <img> in that case.
"""

from __future__ import annotations

import logging
import os
from typing import Optional
from urllib.parse import urlencode

logger = logging.getLogger(__name__)

_ENDPOINT = "https://maps.googleapis.com/maps/api/streetview"

# Sized for the 320px-max popup. 600x320 @ 2x density keeps it sharp on
# retina without blowing past the free-tier 640px-wide limit.
_DEFAULT_SIZE = "600x320"


def streetview_image_url(
    latitude: Optional[float],
    longitude: Optional[float],
    size: str = _DEFAULT_SIZE,
) -> Optional[str]:
    """
    Build a Street View Static API URL for the given coordinates.

    Returns None when coordinates are missing or the API key is unset —
    callers should treat None as "no image, skip the <img> tag."
    """
    if latitude is None or longitude is None:
        return None

    api_key = os.environ.get("GOOGLE_STREETVIEW_API_KEY") or os.environ.get(
        "GOOGLE_MAPS_API_KEY"
    )
    if not api_key:
        logger.warning(
            "Neither GOOGLE_STREETVIEW_API_KEY nor GOOGLE_MAPS_API_KEY is set; "
            "skipping Street View image"
        )
        return None

    params = {
        "size": size,
        "location": f"{latitude},{longitude}",
        "fov": "90",
        "pitch": "0",
        "return_error_code": "true",
        "key": api_key,
    }
    return f"{_ENDPOINT}?{urlencode(params)}"
