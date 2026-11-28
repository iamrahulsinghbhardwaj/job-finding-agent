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
    # hide_parameters: SQLAlchemy errors would otherwise quote the bound values (job titles,
    # descriptions, reasons) into exception messages, and Actions logs are public.
    return create_engine(to_sqlalchemy_url(url), pool_pre_ping=True, hide_parameters=True)
