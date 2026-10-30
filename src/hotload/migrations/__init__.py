"""Alembic migrations, run programmatically at startup (`Database.migrate`) or via `alembic` if preferred."""

from pathlib import Path

from alembic import command
from alembic.config import Config

HERE = Path(__file__).resolve().parent


def make_config(db_url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(HERE))
    cfg.set_main_option("sqlalchemy.url", db_url.replace("%", "%%"))
    return cfg


def upgrade(db_url: str, revision: str = "head") -> None:
    """Blocking. Run in a thread: Alembic's async env starts its own event loop."""
    command.upgrade(make_config(db_url), revision)
