"""Job.max_yoe: upper end of an explicit experience range ("2-12+ years" -> 2..12).

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("job", sa.Column("max_yoe", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("job", "max_yoe")
