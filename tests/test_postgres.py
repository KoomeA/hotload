"""Checks that only mean something on PostgreSQL. Skipped unless HOTLOAD_TEST_POSTGRES_URL points at a server:

    HOTLOAD_TEST_POSTGRES_URL=postgresql+asyncpg://postgres@127.0.0.1:5433 pytest tests/test_postgres.py

The URL is the server (no database name): each test creates and drops its own throwaway database.
"""

import asyncio
import os
import secrets

import pytest

from hotload import migrations
from hotload.config import Settings
from hotload.db import Database

BASE = os.environ.get("HOTLOAD_TEST_POSTGRES_URL", "").rstrip("/")
pytestmark = pytest.mark.skipif(not BASE, reason="set HOTLOAD_TEST_POSTGRES_URL to run the PostgreSQL checks")


@pytest.fixture
async def url():
    import asyncpg

    name = f"hl_test_{secrets.token_hex(4)}"
    admin = await asyncpg.connect(BASE.replace("postgresql+asyncpg://", "postgresql://") + "/postgres")
    await admin.execute(f'CREATE DATABASE "{name}"')
    yield f"{BASE}/{name}"
    await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    await admin.close()


async def test_fresh_database_reaches_head_with_every_table(url):
    from sqlalchemy import text

    db = Database(Settings(database_url=url))
    await db.migrate()
    async with db.engine.connect() as c:
        tables = {r[0] for r in await c.execute(text("select tablename from pg_tables where schemaname='public'"))}
        width = (await c.execute(text("select character_maximum_length from information_schema.columns "
                                      "where table_name='payments' and column_name='status'"))).scalar_one()
    assert {"users", "payments", "payment_events", "scheduled_recordings", "user_cookies", "alembic_version"} <= tables
    assert width >= 12          # "chargedback" has 11 characters and PostgreSQL enforces column widths (SQLite does not)
    await db.dispose()


async def test_an_older_database_with_data_upgrades_and_keeps_it(url):
    from sqlalchemy import text

    await asyncio.to_thread(migrations.upgrade, url, "0005")
    db = Database(Settings(database_url=url))
    async with db.engine.begin() as c:
        await c.execute(text("insert into users (id,email,password_hash,tier,is_admin,session_epoch,created) "
                             "values ('u1','old@example.com','h','premium',false,0,1.0)"))
        await c.execute(text("insert into payments (id,user_id,provider,plan,amount_minor,currency,status,created) "
                             "values ('p1','u1','paystack','premium_30',999,'USD','paid',3.0)"))
    await db.migrate()
    assert (await db.user_by_email("old@example.com")).email_verified is False
    assert (await db.reverse_payment("p1", "chargeback", "admin"))["status"] == "chargedback"
    await db.dispose()


async def test_processes_that_start_together_do_not_race_to_create_the_schema(url):
    """The API and every worker migrate at startup; on a first deploy they start at the same moment. Without the advisory
    lock in migrations/env.py all but one of them crash with a duplicate-key error. (Separate processes, as in production:
    Alembic keeps global state, so threads in one process would not be a fair test.)"""
    import sys

    code = ("import asyncio, sys; from hotload.config import Settings; from hotload.db import Database\n"
            "async def main():\n"
            "    db = Database(Settings(database_url=sys.argv[1])); await db.migrate(); await db.dispose()\n"
            "asyncio.run(main())")
    procs = [await asyncio.create_subprocess_exec(sys.executable, "-c", code, url, stdout=asyncio.subprocess.PIPE,
                                                  stderr=asyncio.subprocess.PIPE) for _ in range(5)]
    outputs = await asyncio.gather(*(p.communicate() for p in procs))
    failed = [err.decode()[-300:] for p, (_, err) in zip(procs, outputs, strict=True) if p.returncode != 0]
    assert failed == []
