"""Fixtures for tests that need a real Postgres.

They run only when TEST_DATABASE_URL is set (environment or repo-root .env), e.g. a Neon dev
branch; otherwise they are skipped. Each test session creates its own schema, runs the real
Alembic migration into it, and drops it at the end. Each test runs in a transaction that is
rolled back, so nothing is left behind.
"""

import os
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from dotenv import dotenv_values
from sqlalchemy import Connection, Engine, text
from sqlalchemy.orm import Session

from jobagent.db.session import make_engine

REPO_ROOT = Path(__file__).resolve().parents[2]
_DOTENV = dotenv_values(REPO_ROOT / ".env") if (REPO_ROOT / ".env").exists() else {}
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL") or _DOTENV.get("TEST_DATABASE_URL") or ""
DATABASE_URL = os.environ.get("DATABASE_URL") or _DOTENV.get("DATABASE_URL") or ""
STALE_SCHEMA_SECONDS = 3600

needs_db = pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL not set")


@dataclass(frozen=True)
class DbHandle:
    engine: Engine
    schema: str


@pytest.fixture(scope="session")
def test_db() -> Iterator[DbHandle]:
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL not set")
    if DATABASE_URL and TEST_DATABASE_URL == DATABASE_URL:
        raise RuntimeError("TEST_DATABASE_URL equals DATABASE_URL; refusing to test on the real database")

    engine = make_engine(TEST_DATABASE_URL)
    schema = f"test_{int(time.time())}_{uuid.uuid4().hex[:8]}"
    with engine.begin() as conn:
        _drop_abandoned_schemas(conn)
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    try:
        config = Config(str(REPO_ROOT / "alembic.ini"))
        with engine.connect() as conn:
            config.attributes["connection"] = conn
            config.attributes["schema"] = schema
            command.upgrade(config, "head")
        yield DbHandle(engine, schema)
    finally:
        with engine.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        engine.dispose()


@pytest.fixture
def session(test_db: DbHandle) -> Iterator[Session]:
    """A Session inside a transaction that is always rolled back."""
    with test_db.engine.connect() as conn:
        transaction = conn.begin()
        conn.execute(text(f'SET LOCAL search_path TO "{test_db.schema}"'))
        db = Session(bind=conn, join_transaction_mode="create_savepoint")
        try:
            yield db
        finally:
            db.close()
            transaction.rollback()


def _drop_abandoned_schemas(conn: Connection) -> None:
    """Remove test schemas left behind by crashed runs (older than an hour)."""
    names = conn.execute(text("SELECT nspname FROM pg_namespace WHERE nspname LIKE 'test\\_%'")).scalars()
    cutoff = time.time() - STALE_SCHEMA_SECONDS
    for name in list(names):
        parts = name.split("_")
        if len(parts) == 3 and parts[1].isdigit() and int(parts[1]) < cutoff:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{name}" CASCADE'))
