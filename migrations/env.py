"""Alembic environment.

- CLI (`uv run alembic upgrade head`): connects with DATABASE_URL from settings.
- Programmatic (tests): pass an open Connection in config.attributes["connection"] and an
  optional schema in config.attributes["schema"]; migrations then run inside that schema.
"""

from alembic import context
from sqlalchemy import Connection, text

from jobagent.core.settings import Settings
from jobagent.db.models import Base
from jobagent.db.session import make_engine

config = context.config
target_metadata = Base.metadata


def _run(connection: Connection, schema: str | None) -> None:
    if schema:
        # SET LOCAL lasts only for this transaction (safe with Neon's pooled connections).
        connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
    context.configure(connection=connection, target_metadata=target_metadata, version_table_schema=schema)
    context.run_migrations()


def run_migrations_online() -> None:
    connection: Connection | None = config.attributes.get("connection")
    schema: str | None = config.attributes.get("schema")
    if connection is not None:
        with connection.begin():
            _run(connection, schema)
        return
    engine = make_engine(Settings().require("database_url"))
    with engine.connect() as conn, conn.begin():
        _run(conn, schema)
    engine.dispose()


if context.is_offline_mode():
    raise SystemExit("Offline (--sql) mode is not supported; run against a database.")
run_migrations_online()
