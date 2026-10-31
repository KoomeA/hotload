"""Refunds, disputes and chargebacks: the state machine (against the database) and the admin and webhook paths."""

import asyncio
import hashlib
import hmac
import json
import time

import httpx
import pytest
from fakeredis import FakeAsyncRedis
from fastapi.testclient import TestClient

from hotload.api.app import create_app
from hotload.config import Settings
from hotload.db import Database

DAY = 86400
PS_KEY = "sk_test_secret"
PW = "correct horse battery"


# --- the state machine, straight against the database ------------------------------------------------------------

@pytest.fixture
async def db(tmp_path):
    d = Database(Settings(data_dir=tmp_path))
    await d.migrate()
    yield d
    await d.dispose()


async def buy(db, user, plan="premium_30", now=None, provider="paystack"):
    p = await db.create_payment(user_id=user.id, provider=provider, plan=plan, amount_minor=999, currency="USD")
    assert await db.complete_payment(p.id, now) == "granted"
    return p


async def tier(db, user):
    u = await db.user_by_id(user.id)
    return u.tier, u.tier_expires


async def test_refund_takes_the_days_back_and_drops_to_free(db):
    user = await db.add_user(email="a@example.com", password_hash="x")
    p = await buy(db, user)
    r = await db.reverse_payment(p.id, "refund", "admin1", "customer asked")
    assert r == {"ok": True, "status": "refunded", "changed": True, "entitlement": "revoked"}
    assert await tier(db, user) == ("free", None)
    assert (await db.get_payment(p.id)).status == "refunded"


async def test_refunding_one_of_two_stacked_purchases_keeps_the_other(db):
    user = await db.add_user(email="b@example.com", password_hash="x")
    now = time.time()
    first, _second = await buy(db, user, now=now), await buy(db, user, now=now)
    before = (await tier(db, user))[1]
    assert abs(before - (now + 60 * DAY)) < 1
    r = await db.reverse_payment(first.id, "refund", "admin1")
    assert r["entitlement"] == "shortened"
    t, exp = await tier(db, user)
    assert t == "premium" and abs(exp - (now + 30 * DAY)) < 1


async def test_repeating_an_action_is_a_no_op_so_retried_webhooks_do_not_shorten_twice(db):
    user = await db.add_user(email="c@example.com", password_hash="x")
    now = time.time()
    first, _second = await buy(db, user, now=now), await buy(db, user, now=now)
    await db.reverse_payment(first.id, "refund", "provider:paystack")
    expiry = (await tier(db, user))[1]
    for _ in range(3):
        again = await db.reverse_payment(first.id, "refund", "provider:paystack")
        assert again["ok"] and not again["changed"]
    assert (await tier(db, user))[1] == expiry
    assert len(await db.payment_events(first.id)) == 1


async def test_concurrent_refunds_take_the_days_back_once(db):
    user = await db.add_user(email="d@example.com", password_hash="x")
    now = time.time()
    first, _second = await buy(db, user, now=now), await buy(db, user, now=now)
    results = await asyncio.gather(*(db.reverse_payment(first.id, "refund", "x") for _ in range(6)))
    assert sum(r["changed"] for r in results) == 1
    assert abs((await tier(db, user))[1] - (now + 30 * DAY)) < 1


async def test_a_refunded_payment_can_never_be_granted_again(db):
    user = await db.add_user(email="e@example.com", password_hash="x")
    p = await buy(db, user)
    await db.reverse_payment(p.id, "refund", "x")
    assert await db.complete_payment(p.id) == "noop"  # e.g. the provider still says "success" on a later poll
    assert await tier(db, user) == ("free", None)


async def test_user_who_moved_to_a_higher_plan_keeps_it_when_the_cheaper_payment_is_refunded(db):
    user = await db.add_user(email="f@example.com", password_hash="x")
    cheap = await buy(db, user, "premium_30")
    await buy(db, user, "pro_30")
    assert (await tier(db, user))[0] == "pro"
    r = await db.reverse_payment(cheap.id, "refund", "x")
    assert r["ok"] and r["entitlement"] == "skipped_higher_plan"
    assert (await tier(db, user))[0] == "pro"


async def test_manual_grants_and_lapsed_plans_are_not_touched(db):
    user = await db.add_user(email="g@example.com", password_hash="x")
    old = await buy(db, user, now=time.time() - 100 * DAY)  # long expired
    await db.set_tier(user.id, "premium", None)             # then an operator granted a perpetual plan
    r = await db.reverse_payment(old.id, "refund", "x")
    assert r["entitlement"] == "unchanged" and await tier(db, user) == ("premium", None)


async def test_dispute_suspends_at_once_and_winning_it_restores_access(db):
    user = await db.add_user(email="h@example.com", password_hash="x")
    p = await buy(db, user)
    assert (await db.reverse_payment(p.id, "dispute", "provider:paystack"))["entitlement"] == "revoked"
    assert await tier(db, user) == ("free", None)
    won = await db.reverse_payment(p.id, "dispute_won", "admin1")
    assert won["status"] == "paid" and won["entitlement"] == "granted"
    t, exp = await tier(db, user)
    assert t == "premium" and exp > time.time() + 29 * DAY


async def test_losing_a_dispute_is_a_chargeback_with_no_second_deduction(db):
    user = await db.add_user(email="i@example.com", password_hash="x")
    now = time.time()
    first, _second = await buy(db, user, now=now), await buy(db, user, now=now)
    await db.reverse_payment(first.id, "dispute", "x")
    after_dispute = (await tier(db, user))[1]
    r = await db.reverse_payment(first.id, "chargeback", "x")
    assert r["status"] == "chargedback" and r["entitlement"] == "none"
    assert (await tier(db, user))[1] == after_dispute  # the days were already taken when the dispute opened


async def test_chargeback_straight_from_paid_revokes(db):
    user = await db.add_user(email="j@example.com", password_hash="x")
    p = await buy(db, user)
    assert (await db.reverse_payment(p.id, "chargeback", "admin1"))["entitlement"] == "revoked"
    assert await tier(db, user) == ("free", None)


async def test_a_won_dispute_does_not_undercut_a_higher_plan(db):
    user = await db.add_user(email="k@example.com", password_hash="x")
    cheap = await buy(db, user, "premium_30")
    await db.reverse_payment(cheap.id, "dispute", "x")
    await buy(db, user, "pro_30")
    won = await db.reverse_payment(cheap.id, "dispute_won", "x")
    assert won["status"] == "held" and (await tier(db, user))[0] == "pro"  # an operator decides; nothing is extended


async def test_refunding_a_held_payment_changes_nobody_s_plan(db):
    user = await db.add_user(email="l@example.com", password_hash="x")
    await buy(db, user, "pro_30")
    held = await db.create_payment(user_id=user.id, provider="paystack", plan="premium_30", amount_minor=999, currency="USD")
    assert await db.complete_payment(held.id) == "held"
    before = await tier(db, user)
    r = await db.reverse_payment(held.id, "refund", "admin1")
    assert r["status"] == "refunded" and r["entitlement"] == "none" and await tier(db, user) == before


async def test_impossible_transitions_are_refused(db):
    user = await db.add_user(email="m@example.com", password_hash="x")
    pending = await db.create_payment(user_id=user.id, provider="paystack", plan="premium_30", amount_minor=999, currency="USD")
    assert not (await db.reverse_payment(pending.id, "refund", "x"))["ok"]       # never paid
    paid = await buy(db, user)
    await db.reverse_payment(paid.id, "refund", "x")
    assert not (await db.reverse_payment(paid.id, "dispute", "x"))["ok"]         # already refunded
    assert not (await db.reverse_payment(paid.id, "chargeback", "x"))["ok"]
    assert not (await db.reverse_payment("nope", "refund", "x"))["ok"]


async def test_account_deleted_before_the_refund_is_recorded_without_crashing(db):
    user = await db.add_user(email="n@example.com", password_hash="x")
    p = await buy(db, user)
    await db.delete_user(user.id)
    assert (await db.reverse_payment(p.id, "refund", "x"))["ok"]


async def test_audit_trail_records_who_what_and_the_plan_effect(db):
    user = await db.add_user(email="o@example.com", password_hash="x")
    p = await buy(db, user)
    await db.reverse_payment(p.id, "dispute", "provider:paystack", "opened")
    await db.reverse_payment(p.id, "chargeback", "admin9", "lost")
    events = await db.payment_events(p.id)
    assert [(e["kind"], e["actor"], e["from"], e["to"], e["entitlement"]) for e in events] == [
        ("dispute", "provider:paystack", "paid", "disputed", "revoked"), ("chargeback", "admin9", "disputed", "chargedback", "none")]
    assert events[1]["note"] == "lost"


async def test_counts_and_attention_list_include_disputes(db):
    user = await db.add_user(email="p@example.com", password_hash="x")
    p = await buy(db, user)
    await db.reverse_payment(p.id, "dispute", "x")
    assert (await db.counts())["disputed_payments"] == 1
    listed = await db.payments_needing_attention()
    assert [x["id"] for x in listed] == [p.id] and listed[0]["email"] == "p@example.com"


# --- admin API and provider webhooks ------------------------------------------------------------------------------

class Paystack:
    def __init__(self):
        self.calls: list[httpx.Request] = []
        self.refund_answer = (200, {"status": True, "data": {"id": 777, "status": "pending"}})

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if request.url.path == "/refund":
            code, body = self.refund_answer
            return httpx.Response(code, json=body)
        if request.url.path.startswith("/transaction/verify/"):
            return httpx.Response(200, json={"status": True, "data": {"status": "success", "amount": 999, "currency": "USD"}})
        return httpx.Response(404, json={"message": "nope"})


@pytest.fixture
def ps():
    return Paystack()


@pytest.fixture
def shop(tmp_path, monkeypatch, ps):
    monkeypatch.setattr("socket.getaddrinfo", lambda host, port, *a, **k: [(2, 1, 6, "", ("93.184.216.34", port))])
    cfg = Settings(data_dir=tmp_path, rate_limit_per_minute=1000, secret_key="s", paystack_secret_key=PS_KEY,
                   btcpay_url="https://btcpay.test", btcpay_store_id="S", btcpay_api_key="k", btcpay_webhook_secret="w")

    async def enqueue(_, _queue=None):
        pass

    app = create_app(cfg, redis=FakeAsyncRedis(decode_responses=True), enqueue=enqueue,
                     http_client=httpx.AsyncClient(transport=httpx.MockTransport(ps.handler)))
    with TestClient(app) as c:
        yield c


def customer(c, email="buyer@example.com", plan="premium_30", provider="paystack"):
    """A customer with a paid plan. Returns (user id, payment id). Leaves the client signed out."""
    uid = c.post("/api/v1/auth/register", json={"email": email, "password": PW}).json()["id"]
    db = c.app.state.db
    p = c.portal.call(lambda: db.create_payment(user_id=uid, provider=provider, plan=plan, amount_minor=999, currency="USD"))
    assert c.portal.call(db.complete_payment, p.id) == "granted"
    c.post("/api/v1/auth/logout")
    return uid, p.id


def as_admin(c, email="boss@example.com"):
    c.post("/api/v1/auth/register", json={"email": email, "password": PW})
    assert c.portal.call(c.app.state.db.set_admin, email, True)
    assert c.post("/api/v1/auth/login", json={"email": email, "password": PW}).json()["is_admin"] is True


def user_tier(c, uid):
    return c.portal.call(c.app.state.db.user_by_id, uid).tier


def status(c, pid):
    return c.portal.call(c.app.state.db.get_payment, pid).status


def hook(c, event, ref, key=PS_KEY, nested=True):
    data = {"transaction": {"reference": ref}} if nested else {"transaction_reference": ref}
    body = json.dumps({"event": event, "data": data}).encode()
    sig = hmac.new(key.encode(), body, hashlib.sha512).hexdigest()
    return c.post("/api/v1/billing/webhooks/paystack", content=body, headers={"x-paystack-signature": sig, "content-type": "application/json"})


def test_payment_admin_routes_need_an_admin(shop):
    uid, pid = customer(shop)
    shop.post("/api/v1/auth/login", json={"email": "buyer@example.com", "password": PW})
    for method, path in [("get", "/api/v1/admin/payments"), ("get", f"/api/v1/admin/payments/{pid}"),
                         ("post", f"/api/v1/admin/payments/{pid}/refund"), ("post", f"/api/v1/admin/payments/{pid}/chargeback")]:
        r = getattr(shop, method)(path, **({"json": {}} if method == "post" else {}))
        assert r.status_code == 403, path
    assert status(shop, pid) == "paid" and user_tier(shop, uid) == "premium"  # a customer cannot refund themselves
    shop.post("/api/v1/auth/logout")
    assert shop.post(f"/api/v1/admin/payments/{pid}/refund", json={}).status_code == 401


def test_refund_through_paystack_calls_the_provider_then_revokes(shop, ps):
    uid, pid = customer(shop)
    as_admin(shop)
    r = shop.post(f"/api/v1/admin/payments/{pid}/refund", json={"through_provider": True, "note": "asked nicely"})
    assert r.status_code == 200 and r.json()["entitlement"] == "revoked"
    sent = [c for c in ps.calls if c.url.path == "/refund"]
    assert len(sent) == 1 and json.loads(sent[0].content) == {"transaction": pid}
    assert status(shop, pid) == "refunded" and user_tier(shop, uid) == "free"
    detail = shop.get(f"/api/v1/admin/payments/{pid}").json()
    assert detail["status"] == "refunded" and detail["email"] == "buyer@example.com"
    assert detail["events"][0]["kind"] == "refund" and "provider refund 777" in detail["events"][0]["note"]
    again = shop.post(f"/api/v1/admin/payments/{pid}/refund", json={"through_provider": True})
    assert again.status_code == 200 and not again.json()["changed"]
    assert len([c for c in ps.calls if c.url.path == "/refund"]) == 1  # the provider was not asked twice


def test_provider_refusing_the_refund_changes_nothing(shop, ps):
    uid, pid = customer(shop)
    as_admin(shop)
    ps.refund_answer = (400, {"status": False, "message": "Transaction has been fully reversed"})
    r = shop.post(f"/api/v1/admin/payments/{pid}/refund", json={"through_provider": True})
    assert r.status_code == 502 and "fully reversed" in r.json()["detail"]
    assert status(shop, pid) == "paid" and user_tier(shop, uid) == "premium"


def test_btcpay_has_no_refund_api_so_it_is_recorded_not_requested(shop):
    uid, pid = customer(shop, provider="btcpay")
    as_admin(shop)
    r = shop.post(f"/api/v1/admin/payments/{pid}/refund", json={"through_provider": True})
    assert r.status_code == 400 and "dashboard" in r.json()["detail"]
    assert status(shop, pid) == "paid"
    ok = shop.post(f"/api/v1/admin/payments/{pid}/refund", json={"note": "sent on-chain, txid abc"})
    assert ok.status_code == 200 and status(shop, pid) == "refunded" and user_tier(shop, uid) == "free"


def test_refunding_an_unpaid_payment_is_refused(shop):
    uid = shop.post("/api/v1/auth/register", json={"email": "x@example.com", "password": PW}).json()["id"]
    p = shop.portal.call(lambda: shop.app.state.db.create_payment(user_id=uid, provider="paystack", plan="premium_30",
                                                                  amount_minor=999, currency="USD"))
    shop.post("/api/v1/auth/logout")
    as_admin(shop)
    assert shop.post(f"/api/v1/admin/payments/{p.id}/refund", json={}).status_code == 409
    assert shop.post("/api/v1/admin/payments/nope/refund", json={}).status_code == 404


def test_signed_refund_webhook_records_the_refund_and_replays_are_harmless(shop):
    uid, pid = customer(shop)
    assert hook(shop, "refund.processed", pid, nested=False).status_code == 200
    assert status(shop, pid) == "refunded" and user_tier(shop, uid) == "free"
    assert hook(shop, "refund.processed", pid, nested=False).status_code == 200
    events = shop.portal.call(shop.app.state.db.payment_events, pid)
    assert len(events) == 1 and events[0]["actor"] == "provider:paystack"


def test_refund_webhook_with_a_bad_signature_or_foreign_reference_does_nothing(shop):
    uid, pid = customer(shop)
    assert hook(shop, "refund.processed", pid, key="wrong").status_code == 401
    assert hook(shop, "refund.processed", "unknownref").status_code == 200
    assert status(shop, pid) == "paid" and user_tier(shop, uid) == "premium"
    btc_uid, btc_pid = customer(shop, email="btc@example.com", provider="btcpay")
    assert hook(shop, "refund.processed", btc_pid).status_code == 200      # a paystack event naming a btcpay payment
    assert status(shop, btc_pid) == "paid" and user_tier(shop, btc_uid) == "premium"


def test_dispute_opened_suspends_and_the_operator_decides_the_outcome(shop, logs):
    uid, pid = customer(shop)
    assert hook(shop, "charge.dispute.create", pid).status_code == 200
    assert status(shop, pid) == "disputed" and user_tier(shop, uid) == "free"
    assert "payment DISPUTE" in logs.text  # positive control for the alert
    assert hook(shop, "charge.dispute.resolve", pid).status_code == 200
    assert status(shop, pid) == "disputed"  # a provider's resolution is NOT acted on automatically...
    assert "dispute RESOLVED" in logs.text  # ...but it is shouted about
    as_admin(shop)
    listed = shop.get("/api/v1/admin/payments").json()
    assert [p["id"] for p in listed] == [pid]
    assert shop.get("/api/v1/admin/overview").json()["disputed_payments"] == 1
    won = shop.post(f"/api/v1/admin/payments/{pid}/dispute-won", json={"note": "evidence accepted"})
    assert won.json()["entitlement"] == "granted" and user_tier(shop, uid) == "premium" and status(shop, pid) == "paid"
    kinds = [e["kind"] for e in shop.get(f"/api/v1/admin/payments/{pid}").json()["events"]]
    assert kinds == ["dispute", "dispute_resolved", "dispute_won"]


def test_admin_can_record_a_chargeback_reported_outside_the_api(shop):
    uid, pid = customer(shop, provider="btcpay")
    as_admin(shop)
    assert shop.post(f"/api/v1/admin/payments/{pid}/chargeback", json={"note": "processor email 2026-11-02"}).status_code == 200
    assert status(shop, pid) == "chargedback" and user_tier(shop, uid) == "free"
    assert shop.post(f"/api/v1/admin/payments/{pid}/dispute-won", json={}).status_code == 409  # terminal


def test_a_late_paid_answer_cannot_resurrect_a_refunded_payment(shop):
    """After a refund the provider may still report the transaction as successful; the plan must not come back."""
    uid, pid = customer(shop)
    assert hook(shop, "refund.processed", pid).status_code == 200
    shop.post("/api/v1/auth/login", json={"email": "buyer@example.com", "password": PW})
    shop.get(f"/api/v1/billing/payments/{pid}")                           # polling asks Paystack, which says "success"
    shop.post("/api/v1/billing/webhooks/paystack", content=b"{}", headers={"x-paystack-signature": "bad"})
    assert status(shop, pid) == "refunded" and user_tier(shop, uid) == "free"


def test_migration_gives_a_pre_refunds_database_the_audit_table(tmp_path):
    import sqlite3

    async def go():
        d = Database(Settings(data_dir=tmp_path))
        await d.migrate()
        await d.dispose()

    asyncio.run(go())
    tables = {r[0] for r in sqlite3.connect(tmp_path / "hotload.db").execute("select name from sqlite_master where type='table'")}
    assert "payment_events" in tables
