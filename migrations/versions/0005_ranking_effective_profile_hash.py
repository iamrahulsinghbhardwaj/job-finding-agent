"""Ranking.effective_profile_hash: sha256 of the profile version + the overrides content in use.

NOT NULL: every ranking records exactly which profile it was scored against. The ranking table
is still empty (no ranker yet); with existing rows this migration would refuse to run.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("ranking", sa.Column("effective_profile_hash", sa.String(length=64), nullable=False))


def downgrade() -> None:
    op.drop_column("ranking", "effective_profile_hash")
