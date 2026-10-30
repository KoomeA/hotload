"""Users and download history (SQLAlchemy 2, async). SQLite by default, PostgreSQL for the hosted service."""

import asyncio
import secrets
import time

from sqlalchemy import (
    Boolean,
    Float,
    Index,
    Integer,
    String,
    UniqueConstraint,
    delete,
    event,
    select,
    update,
)
from sqlalchemy.exc import IntegrityError
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
    tier_expires: Mapped[float | None] = mapped_column(Float, nullable=True)  # None = never (manual grants)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)  # set only from the server CLI, never by email
    session_epoch: Mapped[int] = mapped_column(Integer, default=0)  # bump to revoke every existing session
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False)  # True once the owner proved the mailbox (link or OAuth)
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


class Payment(Base):
    """One checkout attempt. `id` is our reference, sent to the provider and echoed back in its webhook."""

    __tablename__ = "payments"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(String(32), index=True)
    provider: Mapped[str] = mapped_column(String(16))
    plan: Mapped[str] = mapped_column(String(24))
    amount_minor: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3))
    status: Mapped[str] = mapped_column(String(10), default="pending")  # pending | paid | failed | expired
    provider_ref: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    created: Mapped[float] = mapped_column(Float, default=time.time)
    paid_at: Mapped[float | None] = mapped_column(Float, nullable=True)

    def public(self) -> dict:
        return {"id": self.id, "provider": self.provider, "plan": self.plan, "amount_minor": self.amount_minor,
                "currency": self.currency, "status": self.status, "created": self.created, "paid_at": self.paid_at}


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(String(16), primary_key=True, default=lambda: secrets.token_hex(6))
    user_id: Mapped[str] = mapped_column(String(32), index=True)
    name: Mapped[str] = mapped_column(String(64))
    key_hash: Mapped[str] = mapped_column(String(64), unique=True)  # sha256 of the secret (high entropy)
    webhook_secret: Mapped[str] = mapped_column(String(64))  # signs job-completion webhooks (server must know it)
    created: Mapped[float] = mapped_column(Float, default=time.time)
    last_used: Mapped[float | None] = mapped_column(Float, nullable=True)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)

    def public(self) -> dict:
        return {"id": self.id, "name": self.name, "created": self.created, "last_used": self.last_used,
                "revoked": self.revoked}


class BlockedItem(Base):
    """Admin blocklist: a whole host (and its subdomains) or one exact URL."""

    __tablename__ = "blocklist"
    __table_args__ = (UniqueConstraint("kind", "value"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(8))  # host | url
    value: Mapped[str] = mapped_column(String(2048))
    reason: Mapped[str] = mapped_column(String(200), default="")
    created: Mapped[float] = mapped_column(Float, default=time.time)
    created_by: Mapped[str | None] = mapped_column(String(32), nullable=True)

    def public(self) -> dict:
        return {"id": self.id, "kind": self.kind, "value": self.value, "reason": self.reason, "created": self.created}


class Report(Base):
    """Abuse/DMCA report from the public form; admins action or reject it."""

    __tablename__ = "reports"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    url: Mapped[str] = mapped_column(String(2048))
    kind: Mapped[str] = mapped_column(String(12))  # copyright | abuse | csam | other
    contact: Mapped[str] = mapped_column(String(320))
    details: Mapped[str] = mapped_column(String(2000), default="")
    status: Mapped[str] = mapped_column(String(10), default="open")  # open | actioned | rejected
    created: Mapped[float] = mapped_column(Float, default=time.time)
    resolved: Mapped[float | None] = mapped_column(Float, nullable=True)

    def public(self) -> dict:
        return {"id": self.id, "url": self.url, "kind": self.kind, "contact": self.contact, "details": self.details,
                "status": self.status, "created": self.created, "resolved": self.resolved}


class UserCookie(Base):
    """A user's own site cookies for one domain, Fernet-encrypted. Never returned by the API, never logged."""

    __tablename__ = "user_cookies"
    __table_args__ = (UniqueConstraint("user_id", "domain"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(32), index=True)
    domain: Mapped[str] = mapped_column(String(255))
    blob: Mapped[str] = mapped_column(String(200000))
    count: Mapped[int] = mapped_column(Integer, default=0)
    created: Mapped[float] = mapped_column(Float, default=time.time)


class Database:
    def __init__(self, settings: Settings):
        self.engine: AsyncEngine = create_async_engine(settings.db_url)
        if settings.db_url.startswith("sqlite"):
            # SQLite ignores SELECT ... FOR UPDATE. Without this, two transactions can both read a user's plan, both
            # compute a new expiry from it, and the second write silently erases the first (a customer loses days).
            # BEGIN IMMEDIATE takes the write lock when the transaction starts, so read-modify-write cycles serialise.
            @event.listens_for(self.engine.sync_engine, "connect")
            def _no_implicit_begin(dbapi_connection, _record):
                dbapi_connection.isolation_level = None

            @event.listens_for(self.engine.sync_engine, "begin")
            def _begin_immediate(conn):
                conn.exec_driver_sql("BEGIN IMMEDIATE")

        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    async def create_all(self) -> None:
        """Create tables directly from the models (tests/dev). Production uses `migrate()`."""
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def migrate(self) -> None:
        """Bring the schema to the latest Alembic revision (safe on new and on pre-Alembic databases)."""
        from .migrations import upgrade

        await asyncio.to_thread(upgrade, str(self.engine.url.render_as_string(hide_password=False)))

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

    async def link_oauth(self, user_id: str, provider: str, sub: str) -> None:
        async with self.session() as s:
            user = await s.get(User, user_id)
            user.oauth_provider, user.oauth_sub, user.email_verified = provider, sub, True
            await s.commit()

    async def bump_epoch(self, user_id: str) -> None:
        async with self.session() as s:
            await s.execute(update(User).where(User.id == user_id).values(session_epoch=User.session_epoch + 1))
            await s.commit()

    async def mark_email_verified(self, user_id: str, email: str) -> bool:
        """Verify only if the account still has the address the link was sent to (the address cannot change today,
        but the check keeps a stale link from verifying a different one if it ever can)."""
        async with self.session() as s:
            res = await s.execute(update(User).where(User.id == user_id, User.email == email).values(email_verified=True))
            await s.commit()
            return bool(res.rowcount)

    async def reset_password(self, user_id: str, old_hash: str | None, new_hash: str) -> bool:
        """Set a new password only if the hash is still the one the reset link was issued against (single use), end every
        session, and count the address as verified: the person just proved they can read its mail."""
        async with self.session() as s, s.begin():
            cond = [User.id == user_id] + ([User.password_hash == old_hash] if old_hash else [User.password_hash.is_(None)])
            res = await s.execute(update(User).where(*cond).values(
                password_hash=new_hash, email_verified=True, session_epoch=User.session_epoch + 1))
            return bool(res.rowcount)

    async def take_over_for_oauth(self, user_id: str, provider: str, sub: str) -> None:
        """First verified provider login for an account that was created with a password.

        Registration does not verify email, so whoever created that row may not own the address. The provider has just
        proven that this person does. Everything the earlier holder could still use is therefore cut off: the password,
        every live session (epoch bump), API keys and saved cookies.
        """
        async with self.session() as s, s.begin():
            await s.execute(update(User).where(User.id == user_id).values(
                oauth_provider=provider, oauth_sub=sub, password_hash=None, email_verified=True, session_epoch=User.session_epoch + 1))
            await s.execute(delete(ApiKey).where(ApiKey.user_id == user_id))
            await s.execute(delete(UserCookie).where(UserCookie.user_id == user_id))

    async def delete_user(self, user_id: str) -> None:
        async with self.session() as s:
            await s.execute(delete(HistoryEntry).where(HistoryEntry.user_id == user_id))
            await s.execute(delete(UserCookie).where(UserCookie.user_id == user_id))
            await s.execute(delete(ApiKey).where(ApiKey.user_id == user_id))
            await s.execute(delete(User).where(User.id == user_id))
            await s.commit()

    # --- payments
    async def create_payment(self, **kwargs) -> Payment:
        async with self.session() as s:
            payment = Payment(**kwargs)
            s.add(payment)
            await s.commit()
            return payment

    async def get_payment(self, payment_id: str) -> Payment | None:
        async with self.session() as s:
            return await s.get(Payment, payment_id)

    async def payment_by_provider_ref(self, provider: str, ref: str) -> Payment | None:
        async with self.session() as s:
            q = select(Payment).where(Payment.provider == provider, Payment.provider_ref == ref)
            return (await s.execute(q)).scalar_one_or_none()

    async def list_payments(self, user_id: str, limit: int = 20) -> list[dict]:
        async with self.session() as s:
            q = select(Payment).where(Payment.user_id == user_id).order_by(Payment.created.desc()).limit(limit)
            return [p.public() for p in (await s.execute(q)).scalars()]

    async def set_provider_ref(self, payment_id: str, ref: str | None) -> None:
        async with self.session() as s:
            await s.execute(update(Payment).where(Payment.id == payment_id).values(provider_ref=ref))
            await s.commit()

    async def close_payment(self, payment_id: str, status: str) -> bool:
        """pending -> failed/expired. Never touches a payment that already settled."""
        async with self.session() as s:
            res = await s.execute(update(Payment).where(Payment.id == payment_id, Payment.status == "pending")
                                  .values(status=status))
            await s.commit()
            return res.rowcount == 1

    async def complete_payment(self, payment_id: str, now: float | None = None) -> str:
        """Settle a payment that the provider confirmed. Returns "granted", "held" or "noop".

        One transaction: the payment's status is moved with a compare-and-set, and only the winner touches the user, so
        replayed webhooks and concurrent confirmations grant exactly once. A payment that closed as expired/failed but
        was then paid late is still honoured (the customer paid); `held` payments are not.

        "held" = the money arrived but granting would be wrong or impossible (the account is gone, or the user already has
        a *higher* plan active: extending it with a cheaper plan's days would sell Pro time at Premium prices). Nothing is
        granted and an operator must refund or credit it; the case is logged at ERROR and counted in the admin overview.
        """
        from .billing.catalog import PLANS, TIER_RANK, entitlement_after

        now = now or time.time()
        async with self.session() as s, s.begin():
            payment = await s.get(Payment, payment_id)
            if payment is None or payment.status in ("paid", "held"):
                return "noop"
            was = payment.status
            user = (await s.execute(select(User).where(User.id == payment.user_id).with_for_update())).scalar_one_or_none()
            plan = PLANS[payment.plan]
            active = (user is not None and user.tier not in ("free", "anonymous")
                      and (user.tier_expires is None or user.tier_expires > now))
            hold = user is None or (active and TIER_RANK[user.tier] > TIER_RANK[plan.tier])
            new_status = "held" if hold else "paid"
            res = await s.execute(update(Payment).where(Payment.id == payment_id, Payment.status == was)
                                  .values(status=new_status, paid_at=now))
            if res.rowcount != 1:
                return "noop"  # another worker settled it first
            if hold:
                return "held"
            user.tier, user.tier_expires = entitlement_after(user.tier, user.tier_expires, plan, now)
            return "granted"

    async def set_tier(self, user_id: str, tier: str, expires: float | None = None) -> None:
        async with self.session() as s:
            user = await s.get(User, user_id)
            user.tier, user.tier_expires = tier, expires
            await s.commit()

    # --- saved cookies
    async def put_cookie(self, user_id: str, domain: str, blob: str, count: int) -> None:
        async with self.session() as s:
            row = (await s.execute(select(UserCookie).where(UserCookie.user_id == user_id, UserCookie.domain == domain))).scalar_one_or_none()
            if row:
                row.blob, row.count, row.created = blob, count, time.time()
            else:
                s.add(UserCookie(user_id=user_id, domain=domain, blob=blob, count=count))
            await s.commit()

    async def list_cookies(self, user_id: str) -> list[UserCookie]:
        async with self.session() as s:
            return list((await s.execute(select(UserCookie).where(UserCookie.user_id == user_id).order_by(UserCookie.domain))).scalars())

    async def delete_cookie(self, user_id: str, domain: str) -> bool:
        async with self.session() as s:
            res = await s.execute(delete(UserCookie).where(UserCookie.user_id == user_id, UserCookie.domain == domain))
            await s.commit()
            return res.rowcount == 1

    # --- admin: blocklist, reports, users
    async def set_admin(self, email: str, value: bool) -> bool:
        async with self.session() as s:
            res = await s.execute(update(User).where(User.email == email.strip().lower()).values(is_admin=value))
            await s.commit()
            return res.rowcount == 1

    async def blocked_items(self) -> list[BlockedItem]:
        async with self.session() as s:
            return list((await s.execute(select(BlockedItem).order_by(BlockedItem.id))).scalars())

    async def add_block(self, kind: str, value: str, reason: str, admin_id: str | None) -> BlockedItem | None:
        async with self.session() as s:
            item = BlockedItem(kind=kind, value=value, reason=reason[:200], created_by=admin_id)
            s.add(item)
            try:
                await s.commit()
            except IntegrityError:
                await s.rollback()
                return None
            return item

    async def remove_block(self, block_id: int) -> bool:
        async with self.session() as s:
            res = await s.execute(delete(BlockedItem).where(BlockedItem.id == block_id))
            await s.commit()
            return res.rowcount == 1

    async def add_report(self, **kwargs) -> Report:
        async with self.session() as s:
            report = Report(**kwargs)
            s.add(report)
            await s.commit()
            return report

    async def list_reports(self, status: str | None = None, limit: int = 100) -> list[dict]:
        async with self.session() as s:
            q = select(Report).order_by(Report.created.desc()).limit(limit)
            if status:
                q = q.where(Report.status == status)
            return [r.public() for r in (await s.execute(q)).scalars()]

    async def resolve_report(self, report_id: str, status: str) -> dict | None:
        async with self.session() as s:
            report = await s.get(Report, report_id)
            if report is None:
                return None
            report.status, report.resolved = status, time.time()
            await s.commit()
            return report.public()

    async def counts(self) -> dict:
        from sqlalchemy import func

        async with self.session() as s:
            users = (await s.execute(select(func.count()).select_from(User))).scalar_one()
            paid = (await s.execute(select(func.count()).select_from(Payment).where(Payment.status == "paid"))).scalar_one()
            open_reports = (await s.execute(select(func.count()).select_from(Report).where(Report.status == "open"))).scalar_one()
            held = (await s.execute(select(func.count()).select_from(Payment).where(Payment.status == "held"))).scalar_one()
            return {"users": users, "paid_payments": paid, "open_reports": open_reports, "held_payments": held}

    # --- API keys
    async def create_key(self, user_id: str, name: str, key_hash: str, webhook_secret: str, key_id: str) -> ApiKey:
        async with self.session() as s:
            key = ApiKey(id=key_id, user_id=user_id, name=name, key_hash=key_hash, webhook_secret=webhook_secret)
            s.add(key)
            await s.commit()
            return key

    async def get_key(self, key_id: str) -> ApiKey | None:
        async with self.session() as s:
            return await s.get(ApiKey, key_id)

    async def list_keys(self, user_id: str) -> list[ApiKey]:
        async with self.session() as s:
            q = select(ApiKey).where(ApiKey.user_id == user_id, ApiKey.revoked.is_(False)).order_by(ApiKey.created)
            return list((await s.execute(q)).scalars())

    async def revoke_key(self, user_id: str, key_id: str) -> bool:
        async with self.session() as s:
            res = await s.execute(update(ApiKey).where(ApiKey.id == key_id, ApiKey.user_id == user_id,
                                                       ApiKey.revoked.is_(False)).values(revoked=True))
            await s.commit()
            return res.rowcount == 1

    async def touch_key(self, key_id: str, now: float) -> None:
        async with self.session() as s:
            await s.execute(update(ApiKey).where(ApiKey.id == key_id).values(last_used=now))
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
