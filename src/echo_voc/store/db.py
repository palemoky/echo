from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from echo_voc.errors import ConfigError

DEFAULT_URL = "postgresql+psycopg://echo:echo@localhost:5433/echo"


def database_url() -> str:
    url = os.getenv("DATABASE_URL", DEFAULT_URL)
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url.removeprefix("postgresql://")
    return url


def _require_store():
    try:
        import pgvector  # noqa: F401
        import sqlalchemy  # noqa: F401
    except ImportError as e:  # pragma: no cover
        raise ConfigError(
            "Storage features need extra dependencies: uv sync --extra store "
            "(or pip install 'echo-voc[store]')"
        ) from e


@lru_cache(maxsize=4)
def get_sessionmaker(url: str | None = None):
    _require_store()
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(url or database_url(), pool_pre_ping=True)
    return async_sessionmaker(engine, expire_on_commit=False)


def migrate(url: str | None = None, revision: str = "head") -> None:
    """Create / upgrade the schema with Alembic (synchronous)."""
    _require_store()
    from alembic import command
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
    cfg.set_main_option("sqlalchemy.url", (url or database_url()).replace("%", "%%"))
    try:
        command.upgrade(cfg, revision)
    except Exception as e:
        if "Connection refused" in str(e) or "connection failed" in str(e):
            raise ConfigError(
                f"Cannot connect to PostgreSQL at {url or database_url()}. "
                "Start it with: docker compose up -d"
            ) from e
        raise
