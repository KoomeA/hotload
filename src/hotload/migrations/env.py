import asyncio

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

config = context.config


def run_migrations(connection) -> None:
    context.configure(connection=connection, target_metadata=None, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_async() -> None:
    engine = create_async_engine(config.get_main_option("sqlalchemy.url"))
    async with engine.connect() as connection:
        await connection.run_sync(run_migrations)
    await engine.dispose()


asyncio.run(run_async())
