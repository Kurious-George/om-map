"""
Google Maps geocoder.

Single public entry point: `geocode(address) -> GeocodeResult`.

Design notes:
  - No Nominatim fallback. Dropped during scoping due to OSM usage-policy
    concerns at corporate volume. If Google is unreachable the property is
    saved with geocode_status=failed and can be retried later.
  - Match-quality policy (agreed with user):
        ROOFTOP, RANGE_INTERPOLATED    -> accept, clean
        GEOMETRIC_CENTER, APPROXIMATE  -> accept, needs_review=True
        partial_match=True (any level) -> accept, needs_review=True
    i.e. the pin still lands on the map, but we flag it for human verification
    and stash the reason in `error` so it surfaces in the review queue.
  - Terminal config errors (bad API key, request denied) raise. Everything
    else — no results, transient failures after retries — is returned as a
    `FAILED` result so the caller can write the row and move on.
  - The googlemaps SDK retries 429 / 5xx with exponential backoff internally;
    we don't layer our own retry loop on top.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Optional

import googlemaps
from googlemaps.exceptions import ApiError, HTTPError, Timeout, TransportError

from db import GeocodeStatus

logger = logging.getLogger(__name__)


# Location types we treat as "clean" for corporate real estate use.
_CLEAN_LOCATION_TYPES = {"ROOFTOP", "RANGE_INTERPOLATED"}

# Google statuses that indicate config/auth problems — surface these as
# exceptions so they are noticed in logs/ops, not quietly buried per-row.
_CONFIG_ERROR_STATUSES = {"REQUEST_DENIED", "INVALID_REQUEST"}


@dataclass(frozen=True)
class GeocodeResult:
    status: GeocodeStatus
    latitude: Optional[float]
    longitude: Optional[float]
    needs_review: bool
    # Populated when status=FAILED (why it failed) or when quality is low
    # (what the quality issue was). Null on a fully clean success.
    error: Optional[str]


class GeocoderConfigError(RuntimeError):
    """Raised on auth / configuration problems with the Google Maps API."""


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _client() -> googlemaps.Client:
    api_key = os.environ.get("GOOGLE_MAPS_API_KEY")
    if not api_key:
        raise GeocoderConfigError(
            "GOOGLE_MAPS_API_KEY is not set. See .env.example."
        )
    # retry_over_query_limit=True (default) + queries_per_second=50 (default)
    # gives us automatic backoff on 429s without our own loop.
    return googlemaps.Client(key=api_key)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def geocode(address: Optional[str]) -> GeocodeResult:
    """
    Geocode `address` via Google Maps.

    Returns:
        GeocodeResult with:
          - status=SKIPPED if `address` is empty/None (nothing to geocode).
          - status=SUCCESS with coordinates if Google returned a match. In
            this case `needs_review` is True when match quality is low.
          - status=FAILED if Google returned no results or the call failed
            after the SDK's internal retries.

    Raises:
        GeocoderConfigError: on auth/config issues (bad key, request denied).
    """
    if not address or not address.strip():
        return GeocodeResult(
            status=GeocodeStatus.SKIPPED,
            latitude=None,
            longitude=None,
            needs_review=False,
            error=None,
        )

    try:
        results = _client().geocode(address)
    except ApiError as exc:
        status_value = getattr(exc, "status", None) or str(exc)
        if status_value in _CONFIG_ERROR_STATUSES:
            logger.error("Google Maps config error (%s) for %r", status_value, address)
            raise GeocoderConfigError(
                f"Google Maps geocoding failed with {status_value!r}. "
                "Check GOOGLE_MAPS_API_KEY and its restrictions."
            ) from exc
        logger.warning("Google Maps ApiError for %r: %s", address, status_value)
        return _failed(f"google api error: {status_value}")
    except (HTTPError, Timeout, TransportError) as exc:
        logger.warning("Google Maps transport error for %r: %s", address, exc)
        return _failed(f"transport error: {exc}")

    if not results:
        logger.warning("Google Maps returned no results for %r", address)
        return _failed("no results")

    return _result_from_response(results[0], address)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _failed(reason: str) -> GeocodeResult:
    return GeocodeResult(
        status=GeocodeStatus.FAILED,
        latitude=None,
        longitude=None,
        needs_review=False,
        error=reason,
    )


def _result_from_response(result: dict, address: str) -> GeocodeResult:
    geometry = result.get("geometry") or {}
    location = geometry.get("location") or {}
    lat = location.get("lat")
    lng = location.get("lng")
    if lat is None or lng is None:
        logger.warning("Google Maps result missing coordinates for %r", address)
        return _failed("coordinates missing in response")

    location_type = geometry.get("location_type") or "UNKNOWN"
    partial = bool(result.get("partial_match", False))

    needs_review = partial or location_type not in _CLEAN_LOCATION_TYPES
    quality_note: Optional[str] = None
    if needs_review:
        quality_note = (
            f"low-quality match: location_type={location_type}"
            + (", partial_match=true" if partial else "")
        )
        logger.warning(
            "Low-quality geocode for %r: %s",
            address,
            quality_note,
        )
    else:
        logger.info("Geocoded %r: (%.6f, %.6f) [%s]", address, lat, lng, location_type)

    return GeocodeResult(
        status=GeocodeStatus.SUCCESS,
        latitude=float(lat),
        longitude=float(lng),
        needs_review=needs_review,
        error=quality_note,
    )
