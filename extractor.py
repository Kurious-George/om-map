"""
Claude-based OM extraction.

Pipeline per PDF:
  1. Preflight size + page count with pypdf; reject up front if we can't
     send this to Claude (saves a wasted API call and gives a clean UI error).
  2. Base64-encode and send to Claude Sonnet 4.6 with a forced tool call.
  3. Read the structured args back; normalize and validate the building_type
     against our enum; apply server-side sanity checks on top of Claude's
     self-reported `needs_review`.

Design decisions:
  - Tool use (not JSON prompting) for structured output. The schema is
    enforced server-side, so malformed responses are rare and easy to detect.
  - The system prompt and tool schema are identical on every call, so both
    carry `cache_control: ephemeral` — after the first call in a 5-minute
    window, ~90% of the prefix is cache-read and input cost drops sharply.
  - The PDF itself is never cached (every document is different) — caching
    it would be pure overhead.
  - `Anthropic(max_retries=3)` handles 429s with exponential backoff via
    the SDK. No hand-rolled retry loop.
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from functools import lru_cache
from io import BytesIO
from typing import Optional

from anthropic import Anthropic
from pypdf import PdfReader
from pypdf.errors import PdfReadError

from db import BuildingType

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Limits and constants
# ---------------------------------------------------------------------------

MODEL = "claude-sonnet-4-6"
MAX_RETRIES = 3
MAX_TOKENS = 1024

# Claude's PDF document-input ceilings. We enforce them ourselves so the user
# sees a clean error instead of a 400 from the API.
MAX_PDF_SIZE_BYTES = 32 * 1024 * 1024  # 32 MB
MAX_PDF_PAGES = 100

# Short addresses are almost certainly incomplete ("123 Main" without a city).
# Force human review below this length regardless of what Claude said.
_MIN_ADDRESS_LENGTH = 10


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ExtractionError(Exception):
    """Base class for all extractor errors. Caller can catch this to skip a file."""


class PdfTooLargeError(ExtractionError):
    pass


class PdfTooManyPagesError(ExtractionError):
    pass


class PdfUnreadableError(ExtractionError):
    """pypdf couldn't parse the file (encrypted, corrupt, or not a PDF)."""


class ClaudeExtractionError(ExtractionError):
    """Claude returned no usable tool call (rare — usually an API-side issue)."""


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExtractionResult:
    address: Optional[str]
    building_type: Optional[BuildingType]
    square_footage: Optional[int]
    needs_review: bool
    review_reason: Optional[str]


# ---------------------------------------------------------------------------
# Tool schema (shared across all calls — cache anchor)
# ---------------------------------------------------------------------------


_TOOL_NAME = "record_property"

_TOOL_SCHEMA = {
    "name": _TOOL_NAME,
    "description": (
        "Record the structured property data extracted from an Offering "
        "Memorandum. Call this exactly once per document."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "address": {
                "type": ["string", "null"],
                "description": (
                    "Full primary property address as it appears in the OM: "
                    "street, city, state, ZIP when present. Null if not stated."
                ),
            },
            "building_type": {
                "type": ["string", "null"],
                "enum": [bt.value for bt in BuildingType] + [None],
                "description": (
                    "Primary asset class. Use 'mixed_use' only when two or "
                    "more categories each form a substantial part of the "
                    "property. Null if not determinable."
                ),
            },
            "square_footage": {
                "type": ["integer", "null"],
                "description": (
                    "Total building square footage as an integer. Prefer "
                    "rentable square footage (RSF) over gross when both are "
                    "stated. Null for land-only deals or when not stated."
                ),
                "minimum": 0,
            },
            "needs_review": {
                "type": "boolean",
                "description": (
                    "True if any field is ambiguous, inferred, partial, or "
                    "the OM describes multiple properties. False only when "
                    "every field was read directly and unambiguously."
                ),
            },
            "review_reason": {
                "type": ["string", "null"],
                "description": (
                    "Brief (<=120 chars) explanation of why needs_review is "
                    "true. Null when needs_review is false."
                ),
                "maxLength": 120,
            },
        },
        "required": ["address", "building_type", "square_footage", "needs_review"],
    },
}

_SYSTEM_PROMPT = """You are a real estate data extraction assistant for Starwood Capital. \
You receive an Offering Memorandum (OM) for a commercial real estate property and \
extract structured fields using the record_property tool.

Rules:
- Never fabricate. If a field is not stated in the OM, return null and set \
needs_review=true with review_reason explaining what was missing.
- Return exactly one call to record_property. Do not write prose.
- For address: include street, city, state, and ZIP when any are present in the OM. \
If the OM covers a portfolio of multiple properties, return the primary/headline \
address only and set needs_review=true.
- For building_type, interpret the categories as:
    office         = office buildings
    residential    = single-family or general residential that is NOT multifamily rental
    retail         = shopping centers, storefronts, freestanding retail
    industrial     = warehouse, distribution, manufacturing, flex
    mixed_use      = two or more of the above each forming a substantial share
    hospitality    = hotels, resorts, other transient lodging
    multifamily    = apartment buildings and other multi-unit rental housing
- For square_footage: prefer rentable square footage (RSF) when both RSF and \
gross are stated. Return null for land-only deals or when only lot/land size is given."""


# ---------------------------------------------------------------------------
# Anthropic client (module-singleton; httpx-backed, thread-safe)
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _client() -> Anthropic:
    import os

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set. See .env.example.")
    return Anthropic(api_key=api_key, max_retries=MAX_RETRIES)


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------


def _preflight(pdf_bytes: bytes, filename: str) -> None:
    if len(pdf_bytes) > MAX_PDF_SIZE_BYTES:
        raise PdfTooLargeError(
            f"{filename} is {len(pdf_bytes) / (1024 * 1024):.1f} MB; "
            f"limit is {MAX_PDF_SIZE_BYTES // (1024 * 1024)} MB."
        )
    try:
        reader = PdfReader(BytesIO(pdf_bytes))
        page_count = len(reader.pages)
    except PdfReadError as exc:
        raise PdfUnreadableError(f"{filename} is not a readable PDF: {exc}") from exc
    except Exception as exc:
        # pypdf occasionally raises bare exceptions on malformed inputs.
        raise PdfUnreadableError(f"{filename} could not be parsed: {exc}") from exc

    if page_count > MAX_PDF_PAGES:
        raise PdfTooManyPagesError(
            f"{filename} has {page_count} pages; limit is {MAX_PDF_PAGES}."
        )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def extract_property(pdf_bytes: bytes, filename: str) -> ExtractionResult:
    """
    Extract structured property data from an OM PDF.

    Args:
        pdf_bytes: raw PDF bytes.
        filename: only used for log/error messages.

    Raises:
        PdfTooLargeError, PdfTooManyPagesError, PdfUnreadableError,
        ClaudeExtractionError, or upstream `anthropic` exceptions the SDK
        raises after retries are exhausted (e.g. APIStatusError).
    """
    _preflight(pdf_bytes, filename)

    pdf_b64 = base64.standard_b64encode(pdf_bytes).decode("ascii")

    response = _client().messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=[
            {
                "type": "text",
                "text": _SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        tools=[{**_TOOL_SCHEMA, "cache_control": {"type": "ephemeral"}}],
        tool_choice={
            "type": "tool",
            "name": _TOOL_NAME,
            "disable_parallel_tool_use": True,
        },
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "document",
                        "source": {
                            "type": "base64",
                            "media_type": "application/pdf",
                            "data": pdf_b64,
                        },
                    },
                    {
                        "type": "text",
                        "text": (
                            "Extract the property details from this "
                            "Offering Memorandum."
                        ),
                    },
                ],
            }
        ],
    )

    usage = response.usage
    logger.info(
        "Extracted %s: input=%d output=%d cache_read=%d cache_write=%d",
        filename,
        usage.input_tokens,
        usage.output_tokens,
        getattr(usage, "cache_read_input_tokens", 0) or 0,
        getattr(usage, "cache_creation_input_tokens", 0) or 0,
    )

    tool_use = next(
        (b for b in response.content if getattr(b, "type", None) == "tool_use"),
        None,
    )
    if tool_use is None or tool_use.name != _TOOL_NAME:
        raise ClaudeExtractionError(
            f"{filename}: Claude returned no {_TOOL_NAME} tool call "
            f"(stop_reason={response.stop_reason})."
        )

    return _build_result(tool_use.input, filename)


# ---------------------------------------------------------------------------
# Post-processing + validation
# ---------------------------------------------------------------------------


def _build_result(args: dict, filename: str) -> ExtractionResult:
    address = _clean_str(args.get("address"))
    building_type = _coerce_building_type(args.get("building_type"))
    square_footage = args.get("square_footage")
    if isinstance(square_footage, float):
        square_footage = int(square_footage)

    needs_review = bool(args.get("needs_review", True))
    review_reason = _clean_str(args.get("review_reason"))

    # Server-side sanity checks layered on top of Claude's self-report.
    extra_reasons: list[str] = []
    if not address or len(address) < _MIN_ADDRESS_LENGTH:
        extra_reasons.append("address missing or too short")
    if building_type is None:
        extra_reasons.append("building type not determinable")
    if square_footage is None:
        extra_reasons.append("square footage not stated")

    if extra_reasons:
        needs_review = True
        combined = "; ".join(extra_reasons)
        review_reason = f"{review_reason}; {combined}" if review_reason else combined

    if needs_review:
        logger.warning("%s flagged for review: %s", filename, review_reason)

    return ExtractionResult(
        address=address,
        building_type=building_type,
        square_footage=square_footage,
        needs_review=needs_review,
        review_reason=review_reason,
    )


def _clean_str(value: object) -> Optional[str]:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _coerce_building_type(value: object) -> Optional[BuildingType]:
    if not isinstance(value, str):
        return None
    # Normalize hyphens/spaces Claude might emit despite the enum constraint.
    normalized = value.strip().lower().replace("-", "_").replace(" ", "_")
    try:
        return BuildingType(normalized)
    except ValueError:
        logger.warning("Unknown building_type returned by Claude: %r", value)
        return None
