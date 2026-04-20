"""add cap_rate and valuation columns; drop existing rows

Revision ID: 0003_add_cap_rate_and_valuation
Revises: 0002_drop_uploaded_by
Create Date: 2026-04-19

Adds two new extracted fields to `properties`:
  - cap_rate      DOUBLE PRECISION   (percentage, e.g. 6.5 = 6.5%)
  - valuation     BIGINT             (asking price in whole US dollars)

Pre-existing rows are TRUNCATEd rather than backfilled — the user will
re-upload their OMs so Claude can populate the new fields. Raw SQL,
consistent with earlier migrations.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0003_add_cap_rate_and_valuation"
down_revision: Union[str, None] = "0002_drop_uploaded_by"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE properties "
        "ADD COLUMN cap_rate DOUBLE PRECISION, "
        "ADD COLUMN valuation BIGINT;"
    )
    # Drop existing rows so re-uploads are not blocked by the sha256 unique
    # index. RESTART IDENTITY keeps the id sequence tidy.
    op.execute("TRUNCATE TABLE properties RESTART IDENTITY;")


def downgrade() -> None:
    op.execute("ALTER TABLE properties DROP COLUMN IF EXISTS valuation;")
    op.execute("ALTER TABLE properties DROP COLUMN IF EXISTS cap_rate;")
