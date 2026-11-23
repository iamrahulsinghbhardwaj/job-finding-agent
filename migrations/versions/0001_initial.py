"""Initial schema: company, source, run, job, ranking, dead_letter.

Revision ID: 0001
Revises:
Create Date: 2026-11-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TZ = sa.DateTime(timezone=True)
EMPTY_JSON = sa.text("'{}'::jsonb")


def upgrade() -> None:
    op.create_table(
        "company",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("slug", sa.String(100), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("tier", sa.String(20), nullable=False),
        sa.Column("source_list", sa.String(50), nullable=True),
        sa.Column("india_presence", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("europe_presence", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("us_transfer_path", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("yc_batch", sa.String(10), nullable=True),
        sa.Column("last_round_date", sa.Date(), nullable=True),
        sa.Column("team_size", sa.Integer(), nullable=True),
        sa.Column("review_status", sa.String(20), server_default=sa.text("'confirmed'"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_company"),
        sa.UniqueConstraint("slug", name="uq_company_slug"),
    )
    op.create_table(
        "run",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("started_at", TZ, server_default=sa.func.now(), nullable=False),
        sa.Column("finished_at", TZ, nullable=True),
        sa.Column("per_source_counts", postgresql.JSONB(), server_default=EMPTY_JSON, nullable=False),
        sa.Column("emailed_job_ids", sa.ARRAY(sa.Integer()), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("cost_usd", sa.Numeric(10, 4), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_run"),
    )
    op.create_table(
        "source",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("platform", sa.String(30), nullable=False),
        sa.Column("tenant", sa.String(200), nullable=False),
        sa.Column("tenant_config", postgresql.JSONB(), server_default=EMPTY_JSON, nullable=False),
        sa.Column("status", sa.String(20), server_default=sa.text("'active'"), nullable=False),
        sa.Column("blocked_reason", sa.Text(), nullable=True),
        sa.Column("health_status", sa.String(20), nullable=True),
        sa.Column("last_ok_at", TZ, nullable=True),
        sa.Column("expected_job_count", sa.Integer(), nullable=True),
        sa.Column("raw_sample", postgresql.JSONB(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_source"),
        sa.ForeignKeyConstraint(
            ["company_id"], ["company.id"], name="fk_source_company_id_company", ondelete="CASCADE"
        ),
        sa.UniqueConstraint("company_id", "platform", "tenant", name="uq_source_company_id_platform_tenant"),
    )
    op.create_table(
        "job",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=False),
        sa.Column("source_job_id", sa.String(200), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("location_raw", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("country", sa.String(2), nullable=True),
        sa.Column("city", sa.String(100), nullable=True),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("posted_at", TZ, nullable=True),
        sa.Column("min_yoe", sa.Integer(), nullable=True),
        sa.Column("visa_label", sa.String(50), nullable=True),
        sa.Column("description_hash", sa.String(64), nullable=True),
        sa.Column("first_seen", TZ, server_default=sa.func.now(), nullable=False),
        sa.Column("last_seen", TZ, server_default=sa.func.now(), nullable=False),
        sa.Column("first_seen_run_id", sa.Integer(), nullable=True),
        sa.Column("last_seen_run_id", sa.Integer(), nullable=True),
        sa.Column("missed_runs", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("status", sa.String(20), server_default=sa.text("'active'"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_job"),
        sa.ForeignKeyConstraint(
            ["company_id"], ["company.id"], name="fk_job_company_id_company", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["source_id"], ["source.id"], name="fk_job_source_id_source", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["first_seen_run_id"], ["run.id"], name="fk_job_first_seen_run_id_run", ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["last_seen_run_id"], ["run.id"], name="fk_job_last_seen_run_id_run", ondelete="SET NULL"
        ),
        sa.UniqueConstraint("company_id", "source_job_id", name="uq_job_company_id_source_job_id"),
    )
    op.create_index("ix_job_source_id_status", "job", ["source_id", "status"])
    op.create_table(
        "ranking",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("job_id", sa.Integer(), nullable=False),
        sa.Column("profile_version", sa.Integer(), nullable=False),
        sa.Column("model", sa.String(100), nullable=False),
        sa.Column("score", sa.Integer(), nullable=False),
        sa.Column("fit", sa.String(10), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("ranked_at", TZ, server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_ranking"),
        sa.ForeignKeyConstraint(["job_id"], ["job.id"], name="fk_ranking_job_id_job", ondelete="CASCADE"),
        sa.UniqueConstraint("job_id", "profile_version", name="uq_ranking_job_id_profile_version"),
    )
    op.create_table(
        "dead_letter",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text(), nullable=False),
        sa.Column("created_at", TZ, server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_dead_letter"),
        sa.ForeignKeyConstraint(["run_id"], ["run.id"], name="fk_dead_letter_run_id_run", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["source_id"], ["source.id"], name="fk_dead_letter_source_id_source", ondelete="CASCADE"
        ),
    )


def downgrade() -> None:
    op.drop_table("dead_letter")
    op.drop_table("ranking")
    op.drop_index("ix_job_source_id_status", table_name="job")
    op.drop_table("job")
    op.drop_table("source")
    op.drop_table("run")
    op.drop_table("company")
