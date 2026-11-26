"""job.description_text (filter survivors only); Ranking unique on (job_id, effective_profile_hash).

The ranker keys rankings on the effective profile (profile version + overrides content), so a
change of overrides can re-rank a job without clashing with its earlier row.

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("job", sa.Column("description_text", sa.Text(), nullable=True))
    op.drop_constraint("uq_ranking_job_id_profile_version", "ranking", type_="unique")
    op.create_unique_constraint(
        "uq_ranking_job_id_effective_profile_hash", "ranking", ["job_id", "effective_profile_hash"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_ranking_job_id_effective_profile_hash", "ranking", type_="unique")
    op.create_unique_constraint("uq_ranking_job_id_profile_version", "ranking", ["job_id", "profile_version"])
    op.drop_column("job", "description_text")
