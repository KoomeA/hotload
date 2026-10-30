"""Users and download history (SQLAlchemy 2, async). SQLite by default, PostgreSQL for the hosted service."""

import secrets
import time

from sqlalchemy import Float, Index, String, UniqueConstraint, delete, select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from .config import Settings

HISTORY_LIMIT = 200


class Base(DeclarativeBase):
    pass


def new_id() -> str:
    return secrets.token_urlsafe(12)


class User(Base):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("oauth_provider", "oauth_sub"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    tier: Mapped[str] = mapped_column(String(16), default="free")
    oauth_provider: Mapped[str | None] = mapped_column(String(16), nullable=True)
    oauth_sub: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created: Mapped[float] = mapped_column(Float, default=time.time)


class HistoryEntry(Base):
    __tablename__ = "history"
    __table_args__ = (Index("ix_history_user_created", "user_id", "created"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True)  # the job id
    user_id: Mapped[str] = mapped_column(String(32), index=True)
    url: Mapped[str] = mapped_column(String(2048))
    title: Mapped[str | None] = mapped_column(String(300), nullable=True)
    site: Mapped[str | None] = mapped_column(String(64), nullable=True)
    preset: Mapped[str] = mapped_column(String(16))
    state: Mapped[str] = mapped_column(String(10), default="queued")
    created: Mapped[float] = mapped_column(Float, default=time.time)

    def public(self) -> dict:
        return {"id": self.id, "url": self.url, "title": self.title, "site": self.site,
                "preset": self.preset, "state": self.state, "created": self.created}


class Database:
    def __init__(self, settings: Settings):
        self.engine: AsyncEngine = create_async_engine(settings.db_url)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    async def create_all(self) -> None:
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def dispose(self) -> None:
        await self.engine.dispose()

    def session(self) -> AsyncSession:
        return self.sessions()

    # --- users
    async def user_by_email(self, email: str) -> User | None:
        async with self.session() as s:
            return (await s.execute(select(User).where(User.email == email))).scalar_one_or_none()

    async def user_by_id(self, user_id: str) -> User | None:
        async with self.session() as s:
            return await s.get(User, user_id)

    async def user_by_oauth(self, provider: str, sub: str) -> User | None:
        async with self.session() as s:
            q = select(User).where(User.oauth_provider == provider, User.oauth_sub == sub)
            return (await s.execute(q)).scalar_one_or_none()

    async def add_user(self, **kwargs) -> User:
        async with self.session() as s:
            user = User(**kwargs)
            s.add(user)
            await s.commit()
            return user

    async def set_tier(self, user_id: str, tier: str) -> None:
        async with self.session() as s:
            user = await s.get(User, user_id)
            user.tier = tier
            await s.commit()

    async def link_oauth(self, user_id: str, provider: str, sub: str) -> None:
        async with self.session() as s:
            user = await s.get(User, user_id)
            user.oauth_provider, user.oauth_sub = provider, sub
            await s.commit()

    async def delete_user(self, user_id: str) -> None:
        async with self.session() as s:
            await s.execute(delete(HistoryEntry).where(HistoryEntry.user_id == user_id))
            await s.execute(delete(User).where(User.id == user_id))
            await s.commit()

    # --- history
    async def add_history(self, user_id: str, job) -> None:
        async with self.session() as s:
            s.add(HistoryEntry(id=job.id, user_id=user_id, url=job.url, preset=job.preset, state=job.state,
                               title=job.title, site=job.site, created=job.created))
            await s.commit()
            old = (await s.execute(select(HistoryEntry.id).where(HistoryEntry.user_id == user_id)
                                   .order_by(HistoryEntry.created.desc()).offset(HISTORY_LIMIT))).scalars().all()
            if old:
                await s.execute(delete(HistoryEntry).where(HistoryEntry.id.in_(old)))
                await s.commit()

    async def finish_history(self, job) -> None:
        async with self.session() as s:
            row = await s.get(HistoryEntry, job.id)
            if row:
                row.state, row.site = job.state, job.site
                row.title = (job.title or row.title or "")[:300] or None
                await s.commit()

    async def list_history(self, user_id: str, limit: int = 50) -> list[dict]:
        async with self.session() as s:
            q = (select(HistoryEntry).where(HistoryEntry.user_id == user_id)
                 .order_by(HistoryEntry.created.desc()).limit(limit))
            return [h.public() for h in (await s.execute(q)).scalars()]

    async def clear_history(self, user_id: str) -> None:
        async with self.session() as s:
            await s.execute(delete(HistoryEntry).where(HistoryEntry.user_id == user_id))
            await s.commit()
