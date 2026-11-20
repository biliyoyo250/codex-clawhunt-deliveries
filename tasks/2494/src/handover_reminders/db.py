import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


def database_url() -> str:
    value = os.environ.get("DATABASE_URL", "")
    if not value:
        raise RuntimeError("Set DATABASE_URL; no database credentials are embedded")
    if value.startswith("postgresql://"):
        value = value.replace("postgresql://", "postgresql+psycopg://", 1)
    if not value.startswith("postgresql+psycopg://"):
        raise ValueError("This component requires PostgreSQL with psycopg; SQLite is not an acceptance substitute")
    return value


def make_session_factory(url: str | None = None):
    engine = create_engine(url or database_url(), pool_pre_ping=True)
    if engine.dialect.name != "postgresql":
        raise ValueError("PostgreSQL required")
    return sessionmaker(engine, expire_on_commit=False)

