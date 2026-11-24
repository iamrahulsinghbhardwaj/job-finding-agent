"""Checks that need no database."""

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy.dialects.postgresql import JSONB

from jobagent.db.models import Base
from jobagent.db.session import to_sqlalchemy_url
from tests.db.conftest import REPO_ROOT


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("postgresql://u:p@host/db?sslmode=require", "postgresql+psycopg://u:p@host/db?sslmode=require"),
        ("postgres://u:p@host/db", "postgresql+psycopg://u:p@host/db"),
        ("postgresql+psycopg://u:p@host/db", "postgresql+psycopg://u:p@host/db"),
    ],
)
def test_to_sqlalchemy_url(url: str, expected: str) -> None:
    assert to_sqlalchemy_url(url) == expected


@pytest.mark.parametrize("url", ["mysql://u:p@host/db", "not a url"])
def test_to_sqlalchemy_url_rejects(url: str) -> None:
    with pytest.raises(ValueError):  # noqa: PT011
        to_sqlalchemy_url(url)


def test_tables_and_key_constraints() -> None:
    tables = Base.metadata.tables
    assert set(tables) == {"company", "source", "run", "job", "ranking", "dead_letter"}
    assert "uq_job_company_id_source_job_id" in {c.name for c in tables["job"].constraints}  # dedupe key
    assert "uq_ranking_job_id_profile_version" in {c.name for c in tables["ranking"].constraints}
    assert "uq_source_company_id_platform_tenant" in {c.name for c in tables["source"].constraints}


def test_jsonb_columns() -> None:
    tables = Base.metadata.tables
    assert isinstance(tables["source"].c.tenant_config.type, JSONB)
    assert isinstance(tables["source"].c.raw_sample.type, JSONB)
    assert isinstance(tables["run"].c.per_source_counts.type, JSONB)


def test_single_migration_head() -> None:
    script = ScriptDirectory.from_config(Config(str(REPO_ROOT / "alembic.ini")))
    assert script.get_heads() == ["0002"]
