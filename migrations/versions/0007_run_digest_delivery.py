"""Run digest delivery state: status, our Message-ID, Gmail's id, attempt and sent times.

emailed_job_ids is recorded before sending; only runs with digest_status = 'sent' count as
emailed, so a failed or interrupted send never hides its jobs and never duplicates them.

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMNS = (
    ("digest_status", sa.String(length=10)),
    ("digest_rfc_message_id", sa.String(length=200)),
    ("digest_gmail_id", sa.String(length=100)),
    ("digest_attempted_at", sa.DateTime(timezone=True)),
    ("digest_sent_at", sa.DateTime(timezone=True)),
)


def upgrade() -> None:
    for name, type_ in _COLUMNS:
        op.add_column("run", sa.Column(name, type_, nullable=True))


def downgrade() -> None:
    for name, _ in reversed(_COLUMNS):
        op.drop_column("run", name)
