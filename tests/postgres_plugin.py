"""Run the whole suite on PostgreSQL instead of SQLite. Not loaded by default.

    export HOTLOAD_TEST_POSTGRES_URL=postgresql+asyncpg://postgres@127.0.0.1:5433/suite   # an EMPTY database you do not mind losing
    HOTLOAD_DATABASE_URL=$HOTLOAD_TEST_POSTGRES_URL PYTHONPATH=tests pytest -p postgres_plugin \\
        --deselect tests/test_migrations.py --deselect tests/test_preflight.py

Every test starts from an empty schema (this drops everything in the database's `public` schema, so never point it at real
data). test_migrations.py is SQLite-specific and test_preflight.py checks the default (SQLite) configuration.
"""

import asyncio
import os

import pytest


def _reset(url: str) -> None:
    import asyncpg

    async def go():
        conn = await asyncpg.connect(url.replace("postgresql+asyncpg://", "postgresql://"))
        try:
            await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        finally:
            await conn.close()

    asyncio.run(go())


@pytest.fixture(autouse=True)
def _fresh_postgres_schema():
    url = os.environ.get("HOTLOAD_TEST_POSTGRES_URL")
    if not url:
        pytest.exit("postgres_plugin needs HOTLOAD_TEST_POSTGRES_URL", returncode=2)
    _reset(url)
    yield
