"""
Database layer for the Starwood OM Map app.

Responsibilities:
  - Declare the `properties` table (single-table schema for v1).
  - Build the SQLAlchemy engine from DATABASE_URL.
  - Expose a `get_session()` context manager that every caller uses for DB access.

Design notes:
  - PDFs live in Azure Blob Storage; this table only stores the blob path and
    a SHA-256 of the file bytes. The hash is the dedup anchor (unique index).
  - Extraction and geocoding have separate status columns so either stage can
    be retried without re-uploading.
  - Soft delete only: `deleted_at IS NULL` is the "alive" predicate used by
    every read query. No row is ever hard-deleted from the app.
"""

from __future__ import annotations

import enum
import logging
import os
from contextlib import contextmanager
from datetime import datetime
from typing import Iterator, Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum as SqlEnum,
    Float,
    Integer,
    String,
    Text,
    create_engine,
    func,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    sessionmaker,
)
from sqlalchemy.sql import expression

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class BuildingType(str, enum.Enum):
    """Asset categories Claude is allowed to return during extraction."""

    OFFICE = "office"
    RESIDENTIAL = "residential"
    RETAIL = "retail"
    INDUSTRIAL = "industrial"
    MIXED_USE = "mixed_use"
    HOSPITALITY = "hospitality"
    MULTIFAMILY = "multifamily"


class ExtractionStatus(str, enum.Enum):
    PENDING = "pending"
    SUCCESS = "success"
    FAILED = "failed"


class GeocodeStatus(str, enum.Enum):
    PENDING = "pending"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"  # extraction produced no address to geocode


# ---------------------------------------------------------------------------
# ORM models
# ---------------------------------------------------------------------------


class Base(DeclarativeBase):
    pass


class Property(Base):
    __tablename__ = "properties"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # File identity -----------------------------------------------------------
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    # SHA-256 of the raw PDF bytes; the uniqueness key for dedup.
    sha256_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, index=True
    )
    file_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # Relative Azure Blob path, e.g. "om-pdfs/<sha256>.pdf". Construct full
    # URLs at read time so we can move storage accounts without a data migration.
    pdf_blob_path: Mapped[str] = mapped_column(String(1024), nullable=False)

    # Extracted fields --------------------------------------------------------
    address: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    building_type: Mapped[Optional[BuildingType]] = mapped_column(
        SqlEnum(
            BuildingType,
            name="building_type",
            native_enum=True,
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=True,
        index=True,
    )
    square_footage: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # Geocoded coordinates ----------------------------------------------------
    latitude: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    longitude: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Pipeline status ---------------------------------------------------------
    extraction_status: Mapped[ExtractionStatus] = mapped_column(
        SqlEnum(
            ExtractionStatus,
            name="extraction_status",
            native_enum=True,
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
        default=ExtractionStatus.PENDING,
        server_default=ExtractionStatus.PENDING.value,
    )
    extraction_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    geocode_status: Mapped[GeocodeStatus] = mapped_column(
        SqlEnum(
            GeocodeStatus,
            name="geocode_status",
            native_enum=True,
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
        default=GeocodeStatus.PENDING,
        server_default=GeocodeStatus.PENDING.value,
    )
    geocode_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Review + audit ----------------------------------------------------------
    needs_review: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=expression.false(),
        index=True,
    )
    upload_timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    deleted_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )

    def __repr__(self) -> str:
        return (
            f"<Property id={self.id} address={self.address!r} "
            f"deleted={self.deleted_at is not None}>"
        )


# ---------------------------------------------------------------------------
# Engine + session factory
# ---------------------------------------------------------------------------


def _build_engine():
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set. Copy .env.example to .env and fill it in."
        )
    return create_engine(
        url,
        # pool_pre_ping avoids 'server closed the connection' errors on
        # long-lived Streamlit processes whose pooled connections can go stale.
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=10,
        future=True,
    )


engine = _build_engine()
SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
    # Keep attributes loaded after commit so Streamlit code can read them
    # without triggering a fresh SELECT on every access.
    expire_on_commit=False,
    future=True,
)


@contextmanager
def get_session() -> Iterator[Session]:
    """Context-managed session. Commits on clean exit, rolls back on error."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        logger.exception("DB transaction failed; rolling back")
        raise
    finally:
        session.close()
