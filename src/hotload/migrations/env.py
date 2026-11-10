import asyncio
import contextlib
import os

from alembic import context
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

config = context.config
# Arbitrary constant: every Hotload process asks for this same PostgreSQL advisory lock before migrating.
MIGRATION_LOCK = 7_341_522_001


def run_migrations(connection) -> None:
    context.configure(connection=connection, target_metadata=None, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_async() -> None:
    engine = create_async_engine(config.get_main_option("sqlalchemy.url"))
    async with engine.connect() as connection:
        locked = connection.dialect.name == "postgresql"
        if locked:
            # The API and every worker migrate at startup, and on a first deploy they start together. Without this they race
            # to create the same tables and all but one crash. Whoever gets the lock migrates; the rest wait, then find
            # the schema already current and do nothing. (A session-level lock: it ends with the connection even on a crash.)
            await connection.execute(text("SELECT pg_advisory_lock(:k)"), {"k": MIGRATION_LOCK})
            await connection.commit()
        try:
            await connection.run_sync(run_migrations)
        finally:
            if locked:
                await connection.rollback()
                await connection.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": MIGRATION_LOCK})
                await connection.commit()
    await engine.dispose()


@contextlib.contextmanager
def sqlite_file_lock(url: str):
    """The same protection as the PostgreSQL advisory lock, for a SQLite file (the self-hosted default): an exclusive lock on a
    sidecar file, held while migrating. Without it the API and the worker, started together on a new volume, both try to create
    the schema and the loser crashes ("table alembic_version already exists")."""
    parsed = make_url(url)
    if not parsed.drivername.startswith("sqlite") or not parsed.database or parsed.database == ":memory:":
        yield
        return
    try:
        import fcntl
    except ImportError:  # Windows: no flock; single-process development there is unaffected
        yield
        return
    os.makedirs(os.path.dirname(os.path.abspath(parsed.database)), exist_ok=True)
    with open(parsed.database + ".migrate.lock", "w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)  # blocks until the other process has finished migrating
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


with sqlite_file_lock(config.get_main_option("sqlalchemy.url")):
    asyncio.run(run_async())
