"""Database engine creation. Connection strings come from settings, never from code."""

from sqlalchemy import Engine, create_engine

_DRIVER = "postgresql+psycopg"


def to_sqlalchemy_url(url: str) -> str:
    """Neon/Heroku style `postgres://` or `postgresql://` -> SQLAlchemy URL using psycopg 3."""
    scheme, sep, rest = url.partition("://")
    if not sep:
        raise ValueError("not a database URL")
    if scheme in ("postgres", "postgresql"):
        return f"{_DRIVER}://{rest}"
    if scheme.startswith("postgresql+"):
        return url
    raise ValueError(f"unsupported database scheme: {scheme!r}")


def make_engine(url: str) -> Engine:
    return create_engine(to_sqlalchemy_url(url), pool_pre_ping=True)
