import asyncio

from alembic import context
from sqlalchemy import text
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


asyncio.run(run_async())
