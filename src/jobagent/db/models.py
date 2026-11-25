"""SQLAlchemy 2.0 models for HLD section 9 (Phase 1 tables; Signal and Outreach come in Phase 2).

Enum-like columns are plain strings; values are validated by the Pydantic models before they
reach the database. Timestamps are timezone-aware.
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    ARRAY,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Deterministic constraint names, so migrations and ON CONFLICT can refer to them.
NAMING = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

Json = dict[str, Any]
_EMPTY_JSON = text("'{}'::jsonb")


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING)
    type_annotation_map = {Json: JSONB, datetime: DateTime(timezone=True)}  # noqa: RUF012 (SQLAlchemy setting)


class Company(Base):
    __tablename__ = "company"

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(100), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    tier: Mapped[str] = mapped_column(String(20))  # T1 / T1_STARTUP / OTHER, set by hand
    source_list: Mapped[str | None] = mapped_column(String(50))  # registry / yc / vc:<fund>
    india_presence: Mapped[bool] = mapped_column(server_default=text("false"))
    europe_presence: Mapped[bool] = mapped_column(server_default=text("false"))
    us_transfer_path: Mapped[bool] = mapped_column(server_default=text("false"))
    yc_batch: Mapped[str | None] = mapped_column(String(10))
    last_round_date: Mapped[date | None] = mapped_column(Date)
    team_size: Mapped[int | None]
    review_status: Mapped[str] = mapped_column(String(20), server_default=text("'confirmed'"))


class Source(Base):
    __tablename__ = "source"
    __table_args__ = (UniqueConstraint("company_id", "platform", "tenant"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("company.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(20))  # platform / custom / email_alert / yc
    platform: Mapped[str] = mapped_column(String(30))
    tenant: Mapped[str] = mapped_column(String(200))
    tenant_config: Mapped[Json] = mapped_column(server_default=_EMPTY_JSON)
    status: Mapped[str] = mapped_column(String(20), server_default=text("'active'"))  # active / blocked
    blocked_reason: Mapped[str | None] = mapped_column(Text)
    health_status: Mapped[str | None] = mapped_column(String(20))
    last_ok_at: Mapped[datetime | None]
    expected_job_count: Mapped[int | None]
    raw_sample: Mapped[Json | None]  # one recent listing, for debugging schema changes
    # per-query state of paged sources, e.g. {"backfilled": ["|IND|software-development"]}
    crawl_state: Mapped[Json] = mapped_column(server_default=_EMPTY_JSON)


class Run(Base):
    __tablename__ = "run"

    id: Mapped[int] = mapped_column(primary_key=True)
    started_at: Mapped[datetime] = mapped_column(server_default=func.now())
    finished_at: Mapped[datetime | None]
    per_source_counts: Mapped[Json] = mapped_column(server_default=_EMPTY_JSON)
    emailed_job_ids: Mapped[list[int]] = mapped_column(ARRAY(Integer), server_default=text("'{}'"))
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))


class Job(Base):
    __tablename__ = "job"
    __table_args__ = (
        UniqueConstraint("company_id", "source_job_id"),
        Index("ix_job_source_id_status", "source_id", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("company.id", ondelete="CASCADE"))
    source_id: Mapped[int] = mapped_column(ForeignKey("source.id", ondelete="CASCADE"))
    source_job_id: Mapped[str] = mapped_column(String(200))
    title: Mapped[str] = mapped_column(Text)
    location_raw: Mapped[str] = mapped_column(Text, server_default=text("''"))
    country: Mapped[str | None] = mapped_column(String(2))  # ISO-2, filled by normalize
    city: Mapped[str | None] = mapped_column(String(100))
    url: Mapped[str] = mapped_column(Text)
    posted_at: Mapped[datetime | None]
    min_yoe: Mapped[int | None]
    max_yoe: Mapped[int | None]  # only for an explicit range ("2-12+ years")
    visa_label: Mapped[str | None] = mapped_column(String(50))
    description_hash: Mapped[str | None] = mapped_column(String(64))
    first_seen: Mapped[datetime] = mapped_column(server_default=func.now())
    last_seen: Mapped[datetime] = mapped_column(server_default=func.now())
    first_seen_run_id: Mapped[int | None] = mapped_column(ForeignKey("run.id", ondelete="SET NULL"))
    last_seen_run_id: Mapped[int | None] = mapped_column(ForeignKey("run.id", ondelete="SET NULL"))
    missed_runs: Mapped[int] = mapped_column(server_default=text("0"))
    status: Mapped[str] = mapped_column(String(20), server_default=text("'active'"))  # active / stale


class Ranking(Base):
    __tablename__ = "ranking"
    __table_args__ = (UniqueConstraint("job_id", "profile_version"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("job.id", ondelete="CASCADE"))
    profile_version: Mapped[int]
    model: Mapped[str] = mapped_column(String(100))
    score: Mapped[int]
    fit: Mapped[str] = mapped_column(String(10))  # High / Medium / Low
    reason: Mapped[str] = mapped_column(Text)
    ranked_at: Mapped[datetime] = mapped_column(server_default=func.now())


class DeadLetter(Base):
    __tablename__ = "dead_letter"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("run.id", ondelete="CASCADE"))
    source_id: Mapped[int] = mapped_column(ForeignKey("source.id", ondelete="CASCADE"))
    error: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
