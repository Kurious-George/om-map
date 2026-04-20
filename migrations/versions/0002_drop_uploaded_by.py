"""drop uploaded_by column and its index

Revision ID: 0002_drop_uploaded_by
Revises: 0001_initial
Create Date: 2026-04-19

The app no longer tracks who uploaded a given PDF (the user-selector UI
was removed). Drop the column and its index. Raw SQL, consistent with
0001, keeps the migration style uniform.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0002_drop_uploaded_by"
down_revision: Union[str, None] = "0001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_properties_uploaded_by;")
    op.execute("ALTER TABLE properties DROP COLUMN IF EXISTS uploaded_by;")


def downgrade() -> None:
    # Existing rows cannot recover the original value; backfill with a
    # placeholder so the NOT NULL constraint can be restored.
    op.execute(
        "ALTER TABLE properties "
        "ADD COLUMN uploaded_by VARCHAR(128) NOT NULL DEFAULT 'unknown';"
    )
    op.execute("ALTER TABLE properties ALTER COLUMN uploaded_by DROP DEFAULT;")
    op.execute(
        "CREATE INDEX ix_properties_uploaded_by ON properties (uploaded_by);"
    )
