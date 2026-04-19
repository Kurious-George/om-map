"""initial schema: properties table + enums

Revision ID: 0001_initial
Revises:
Create Date: 2026-04-19
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Enum types are created explicitly here so downgrade() can drop them cleanly.
# When referenced in the table DDL below we pass create_type=False so SQLAlchemy
# does not try to create them a second time.
_building_type = sa.Enum(
    "office",
    "residential",
    "retail",
    "industrial",
    "mixed_use",
    "hospitality",
    "multifamily",
    name="building_type",
)
_extraction_status = sa.Enum(
    "pending", "success", "failed",
    name="extraction_status",
)
_geocode_status = sa.Enum(
    "pending", "success", "failed", "skipped",
    name="geocode_status",
)


def upgrade() -> None:
    bind = op.get_bind()
    _building_type.create(bind, checkfirst=True)
    _extraction_status.create(bind, checkfirst=True)
    _geocode_status.create(bind, checkfirst=True)

    op.create_table(
        "properties",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("filename", sa.String(length=512), nullable=False),
        sa.Column("sha256_hash", sa.String(length=64), nullable=False),
        sa.Column("file_size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("pdf_blob_path", sa.String(length=1024), nullable=False),
        sa.Column("address", sa.String(length=512), nullable=True),
        sa.Column(
            "building_type",
            sa.Enum(
                "office",
                "residential",
                "retail",
                "industrial",
                "mixed_use",
                "hospitality",
                "multifamily",
                name="building_type",
                create_type=False,
            ),
            nullable=True,
        ),
        sa.Column("square_footage", sa.Integer(), nullable=True),
        sa.Column("latitude", sa.Float(), nullable=True),
        sa.Column("longitude", sa.Float(), nullable=True),
        sa.Column(
            "extraction_status",
            sa.Enum(
                "pending", "success", "failed",
                name="extraction_status",
                create_type=False,
            ),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("extraction_error", sa.Text(), nullable=True),
        sa.Column(
            "geocode_status",
            sa.Enum(
                "pending", "success", "failed", "skipped",
                name="geocode_status",
                create_type=False,
            ),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("geocode_error", sa.Text(), nullable=True),
        sa.Column(
            "needs_review",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("uploaded_by", sa.String(length=128), nullable=False),
        sa.Column(
            "upload_timestamp",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_index(
        "ix_properties_sha256_hash", "properties", ["sha256_hash"], unique=True
    )
    op.create_index(
        "ix_properties_building_type", "properties", ["building_type"]
    )
    op.create_index(
        "ix_properties_needs_review", "properties", ["needs_review"]
    )
    op.create_index(
        "ix_properties_uploaded_by", "properties", ["uploaded_by"]
    )
    op.create_index(
        "ix_properties_deleted_at", "properties", ["deleted_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_properties_deleted_at", table_name="properties")
    op.drop_index("ix_properties_uploaded_by", table_name="properties")
    op.drop_index("ix_properties_needs_review", table_name="properties")
    op.drop_index("ix_properties_building_type", table_name="properties")
    op.drop_index("ix_properties_sha256_hash", table_name="properties")
    op.drop_table("properties")

    bind = op.get_bind()
    _geocode_status.drop(bind, checkfirst=True)
    _extraction_status.drop(bind, checkfirst=True)
    _building_type.drop(bind, checkfirst=True)
