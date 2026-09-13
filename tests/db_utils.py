from __future__ import annotations

import os

TEST_DB = os.getenv(
    "ECHO_TEST_DATABASE_URL", "postgresql+psycopg://echo:echo@localhost:5433/echo_test"
)


def ensure_test_database() -> bool:
    """Create the test database if PostgreSQL is reachable. Returns False if it is not."""
    try:
        import psycopg
    except ImportError:
        return False
    admin = (
        TEST_DB.replace("postgresql+psycopg://", "postgresql://").rsplit("/", 1)[0] + "/postgres"
    )
    name = TEST_DB.rsplit("/", 1)[1]
    try:
        with psycopg.connect(admin, autocommit=True, connect_timeout=2) as conn:
            if not conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone():
                conn.execute(f'CREATE DATABASE "{name}"')
    except psycopg.OperationalError:
        return False
    return True
