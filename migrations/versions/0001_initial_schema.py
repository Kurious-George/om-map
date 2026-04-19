"""initial schema: properties table + enums

Revision ID: 0001_initial
Revises:
Create Date: 2026-04-19

DDL is emitted as raw SQL via op.execute() to sidestep SQLAlchemy's
enum event machinery, which has a long history of double-firing CREATE
TYPE across versions. Subsequent migrations can go back to the usual
op.create_table / op.add_column helpers — this module is only odd
because it bootstraps the enum types themselves.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TYPE building_type AS ENUM (
            'office', 'residential', 'retail', 'industrial',
            'mixed_use', 'hospitality', 'multifamily'
        );
        """
    )
    op.execute(
        """
        CREATE TYPE extraction_status AS ENUM (
            'pending', 'success', 'failed'
        );
        """
    )
    op.execute(
        """
        CREATE TYPE geocode_status AS ENUM (
            'pending', 'success', 'failed', 'skipped'
        );
        """
    )

    op.execute(
        """
        CREATE TABLE properties (
            id                 SERIAL PRIMARY KEY,
            filename           VARCHAR(512)  NOT NULL,
            sha256_hash        VARCHAR(64)   NOT NULL,
            file_size_bytes    BIGINT        NOT NULL,
            pdf_blob_path      VARCHAR(1024) NOT NULL,
            address            VARCHAR(512),
            building_type      building_type,
            square_footage     INTEGER,
            latitude           DOUBLE PRECISION,
            longitude          DOUBLE PRECISION,
            extraction_status  extraction_status NOT NULL DEFAULT 'pending',
            extraction_error   TEXT,
            geocode_status     geocode_status    NOT NULL DEFAULT 'pending',
            geocode_error      TEXT,
            needs_review       BOOLEAN       NOT NULL DEFAULT FALSE,
            uploaded_by        VARCHAR(128)  NOT NULL,
            upload_timestamp   TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
            deleted_at         TIMESTAMPTZ
        );
        """
    )

    op.execute(
        "CREATE UNIQUE INDEX ix_properties_sha256_hash "
        "ON properties (sha256_hash);"
    )
    op.execute(
        "CREATE INDEX ix_properties_building_type "
        "ON properties (building_type);"
    )
    op.execute(
        "CREATE INDEX ix_properties_needs_review "
        "ON properties (needs_review);"
    )
    op.execute(
        "CREATE INDEX ix_properties_uploaded_by "
        "ON properties (uploaded_by);"
    )
    op.execute(
        "CREATE INDEX ix_properties_deleted_at "
        "ON properties (deleted_at);"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS properties;")
    op.execute("DROP TYPE IF EXISTS geocode_status;")
    op.execute("DROP TYPE IF EXISTS extraction_status;")
    op.execute("DROP TYPE IF EXISTS building_type;")
