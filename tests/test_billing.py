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
from hotload.billing import catalog
from hotload.config import Settings
from hotload.db import Database

PS_KEY = "sk_test_secret"
BTC_SECRET = "btcpay-hook-secret"
USER = {"email": "buyer@example.com", "password": "correct horse battery"}
DAY = 86400


class Provider:
    """Programmable fake of the Paystack/BTCPay HTTP APIs that records every call the app makes."""

    def __init__(self):
        self.calls: list[httpx.Request] = []
        self.paystack_verify = {"status": "success", "amount": 999, "currency": "USD"}
        self.invoice = {"status": "Settled", "amount": "9.99", "currency": "USD"}
        self.fail_init = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        path = request.url.path
        if path == "/transaction/initialize":
            if self.fail_init:
                return httpx.Response(400, json={"status": False, "message": "Invalid key"})
            ref = json.loads(request.content)["reference"]
            return httpx.Response(200, json={"status": True, "data": {
                "authorization_url": f"https://checkout.paystack.test/{ref}", "access_code": "AC_1", "reference": ref}})
        if path.startswith("/transaction/verify/"):
            return httpx.Response(200, json={"status": True, "data": self.paystack_verify})
        if path.endswith("/invoices") and request.method == "POST":
            return httpx.Response(200, json={"id": "INV1", "checkoutLink": "https://btcpay.test/i/INV1", "status": "New"})
        if "/invoices/" in path:
            order = self.order_id
            return httpx.Response(200, json={"id": "INV1", "metadata": {"orderId": order}, **self.invoice})
        return httpx.Response(404, json={"message": "nope"})

    order_id = None

    def sent(self, path_part):
        return [c for c in self.calls if path_part in c.url.path]


@pytest.fixture
def prov():
    return Provider()


@pytest.fixture
def make(tmp_path, monkeypatch, prov):
    monkeypatch.setattr("socket.getaddrinfo", lambda host, port, *a, **k: [(2, 1, 6, "", ("93.184.216.34", port))])
    stack = []

    def build(**overrides):
        cfg = {"data_dir": tmp_path, "rate_limit_per_minute": 1000, "secret_key": "s", "paystack_secret_key": PS_KEY,
               "btcpay_url": "https://btcpay.test", "btcpay_store_id": "STORE", "btcpay_api_key": "apikey",
               "btcpay_webhook_secret": BTC_SECRET, **overrides}
        http = httpx.AsyncClient(transport=httpx.MockTransport(prov.handler))

        async def enqueue(_):
            pass

        app = create_app(Settings(**cfg), redis=FakeAsyncRedis(decode_responses=True), enqueue=enqueue, http_client=http)
        c = TestClient(app)
        c.__enter__()
        stack.append(c)
        return c

    yield build
    for c in stack:
        c.__exit__(None, None, None)


@pytest.fixture
def c(make):
    return make()


def signed_in(c):
    r = c.post("/api/v1/auth/register", json=USER)
    assert r.status_code == 201
    return r.json()["id"]


def ps_sig(body: bytes, key=PS_KEY) -> dict:
    return {"x-paystack-signature": hmac.new(key.encode(), body, hashlib.sha512).hexdigest(), "content-type": "application/json"}


def btc_sig(body: bytes, secret=BTC_SECRET) -> dict:
    return {"btcpay-sig": "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest(), "content-type": "application/json"}


def ps_event(ref, event="charge.success"):
    return json.dumps({"event": event, "data": {"reference": ref, "status": "success", "amount": 1, "currency": "USD"}}).encode()


def me(c):
    return c.get("/api/v1/me").json()


def checkout(c, provider="paystack", plan="premium_30", currency="USD"):
    return c.post("/api/v1/billing/checkout", json={"plan": plan, "provider": provider, "currency": currency})


# --- catalog ------------------------------------------------------------------

def test_money_conversion_is_exact():
    assert catalog.to_major(999, "USD") == "9.99"
    assert catalog.to_major(8399, "USD") == "83.99"
    assert catalog.to_major(500, "JPY") == "500"
    assert catalog.to_minor("9.99", "USD") == 999 and catalog.to_minor(83.99, "USD") == 8399
    assert catalog.to_minor("0.10", "USD") == 10


def test_prices_merge_and_ignore_junk():
    s = Settings(prices={"kes": {"premium_30": 129900, "nonsense": 5, "pro_30": -1}})
    assert catalog.price(s, "premium_30", "KES") == 129900
    assert catalog.price(s, "pro_30", "KES") is None and catalog.price(s, "nonsense", "KES") is None
    assert catalog.price(s, "premium_30", "USD") == 999 and catalog.price(Settings(), "premium_30", "EUR") is None


NOW = 1000.0


@pytest.mark.parametrize("tier,exp,plan,expected", [
    ("free", None, "premium_30", ("premium", NOW + 30 * DAY)),
    ("premium", NOW + 10 * DAY, "premium_30", ("premium", NOW + 40 * DAY)),    # renew early: extends, loses nothing
    ("premium", NOW - 5, "premium_30", ("premium", NOW + 30 * DAY)),           # lapsed: a fresh period from now
    ("premium", NOW + 10 * DAY, "pro_30", ("pro", NOW + 30 * DAY)),            # upgrade: switches, no proration
    ("pro", NOW + 10 * DAY, "premium_30", ("pro", NOW + 40 * DAY)),            # race: the days extend the higher plan
    ("pro", None, "premium_365", ("pro", None)),                                # manual perpetual grant stays perpetual
    ("premium", NOW + 10 * DAY, "premium_365", ("premium", NOW + 375 * DAY)),
])
def test_entitlement_after(tier, exp, plan, expected):
    assert catalog.entitlement_after(tier, exp, catalog.PLANS[plan], NOW) == expected


# --- plans & checkout ---------------------------------------------------------

def test_plans_list_only_configured_providers(make):
    assert make(paystack_secret_key=None, btcpay_url=None).get("/api/v1/billing/plans").json() == {"providers": [], "plans": []}
    c = make(btcpay_url=None, prices={"KES": {"premium_30": 129900}})
    data = c.get("/api/v1/billing/plans").json()
    assert data["providers"] == ["paystack"]
    options = {(o["provider"], o["currency"]): o["amount_minor"] for o in next(p for p in data["plans"] if p["plan"] == "premium_30")["options"]}
    assert options == {("paystack", "USD"): 999, ("paystack", "KES"): 129900}


def test_checkout_validation(c):
    assert checkout(c).status_code == 401
    signed_in(c)
    assert checkout(c, plan="gold").status_code == 400
    assert checkout(c, provider="stripe").status_code == 400
    assert checkout(c, currency="EUR").status_code == 400  # no price configured: never guess exchange rates
    assert c.get("/api/v1/billing/payments").json() == []


def test_paystack_checkout_request_is_correct(c, prov):
    uid = signed_in(c)
    r = checkout(c)
    assert r.status_code == 201
    body = r.json()
    req = prov.sent("/transaction/initialize")[0]
    sent = json.loads(req.content)
    assert req.headers["authorization"] == f"Bearer {PS_KEY}"
    assert sent["amount"] == 999 and sent["currency"] == "USD" and sent["email"] == "buyer@example.com"
    assert sent["reference"] == body["payment_id"] and sent["callback_url"].endswith("/account/?payment=")
    assert sent["metadata"]["user_id"] == uid and body["url"] == f"https://checkout.paystack.test/{body['payment_id']}"
    [p] = c.get("/api/v1/billing/payments").json()
    assert p["status"] == "pending" and p["plan"] == "premium_30" and p["provider"] == "paystack"


def test_btcpay_checkout_request_is_correct(c, prov):
    signed_in(c)
    r = checkout(c, "btcpay", "pro_365")
    assert r.status_code == 201 and r.json()["url"] == "https://btcpay.test/i/INV1"
    req = prov.sent("/invoices")[0]
    sent = json.loads(req.content)
    assert req.url.path == "/api/v1/stores/STORE/invoices" and req.headers["authorization"] == "token apikey"
    assert sent["amount"] == "239.99" and sent["currency"] == "USD"
    assert sent["metadata"]["orderId"] == r.json()["payment_id"] and sent["checkout"]["redirectURL"]


def test_provider_failure_returns_502_and_marks_payment_failed(c, prov):
    signed_in(c)
    prov.fail_init = True
    r = checkout(c)
    assert r.status_code == 502 and "Invalid key" not in r.text  # provider detail is not leaked to the user
    assert c.get("/api/v1/billing/payments").json()[0]["status"] == "failed"


def test_cannot_buy_lower_plan_while_higher_is_active(c):
    uid = signed_in(c)
    c.portal.call(c.app.state.db.set_tier, uid, "pro", time.time() + 5 * DAY)
    assert checkout(c, plan="premium_30").status_code == 409
    assert checkout(c, plan="pro_30").status_code == 201  # renewing the same plan is fine


def test_checkout_is_rate_limited(c):
    signed_in(c)
    codes = [checkout(c).status_code for _ in range(8)]
    assert codes[:6] == [201] * 6 and codes[6:] == [429, 429]


# --- Paystack webhook ---------------------------------------------------------

def start(c, provider="paystack", plan="premium_30"):
    return checkout(c, provider, plan).json()["payment_id"]


def post_ps(c, body, headers=None):
    return c.post("/api/v1/billing/webhooks/paystack", content=body, headers=headers or ps_sig(body))


def test_paystack_webhook_rejects_bad_signatures(c, prov):
    signed_in(c)
    pid = start(c)
    body = ps_event(pid)
    assert post_ps(c, body, {"content-type": "application/json"}).status_code == 401           # missing
    assert post_ps(c, body, ps_sig(body, key="wrong")).status_code == 401                       # wrong key
    assert post_ps(c, body + b" ", ps_sig(body)).status_code == 401                             # tampered body
    assert post_ps(c, body, {"x-paystack-signature": hmac.new(PS_KEY.encode(), body, hashlib.sha256).hexdigest()}).status_code == 401
    assert prov.sent("/transaction/verify/") == [] and me(c)["tier"] == "free"                   # nothing was even verified


def test_paystack_success_grants_plan_once(c, prov):
    signed_in(c)
    pid = start(c)
    body = ps_event(pid)
    before = time.time()
    assert post_ps(c, body).status_code == 200
    u = me(c)
    assert u["tier"] == "premium" and before + 30 * DAY - 5 < u["tier_expires"] < time.time() + 30 * DAY + 5
    assert c.get(f"/api/v1/billing/payments/{pid}").json()["status"] == "paid"
    expiry = u["tier_expires"]
    for _ in range(3):  # Paystack retries; replays must not extend the plan
        assert post_ps(c, body).status_code == 200
    assert me(c)["tier_expires"] == expiry


def test_webhook_body_is_not_trusted_the_provider_is_asked(c, prov):
    """A correctly signed event that claims success is still checked with Paystack before granting."""
    signed_in(c)
    pid = start(c)
    prov.paystack_verify = {"status": "abandoned", "amount": 999, "currency": "USD"}
    assert post_ps(c, ps_event(pid)).status_code == 200
    assert me(c)["tier"] == "free" and c.get(f"/api/v1/billing/payments/{pid}").json()["status"] == "pending"
    assert len(prov.sent("/transaction/verify/")) >= 1


@pytest.mark.parametrize("verify", [
    {"status": "success", "amount": 100, "currency": "USD"},      # underpaid
    {"status": "success", "amount": 999, "currency": "KES"},      # different currency, same number
    {"status": "success", "amount": 0, "currency": ""},
])
def test_mismatched_amount_or_currency_never_grants(c, prov, verify):
    signed_in(c)
    pid = start(c)
    prov.paystack_verify = verify
    post_ps(c, ps_event(pid))
    assert me(c)["tier"] == "free" and c.get(f"/api/v1/billing/payments/{pid}").json()["status"] == "pending"


def test_overpayment_still_grants_exact_plan(c, prov):
    signed_in(c)
    pid = start(c)
    prov.paystack_verify = {"status": "success", "amount": 5000, "currency": "USD"}
    post_ps(c, ps_event(pid))
    assert me(c)["tier"] == "premium"


def test_failed_payment_is_closed_and_a_later_success_cannot_resurrect_it(c, prov):
    signed_in(c)
    pid = start(c)
    prov.paystack_verify = {"status": "failed", "amount": 999, "currency": "USD"}
    post_ps(c, ps_event(pid))
    assert c.get(f"/api/v1/billing/payments/{pid}").json()["status"] == "failed"
    prov.paystack_verify = {"status": "success", "amount": 999, "currency": "USD"}
    post_ps(c, ps_event(pid))
    assert me(c)["tier"] == "free"


def test_irrelevant_unknown_and_malformed_events_are_acknowledged_quietly(c, prov):
    signed_in(c)
    start(c)
    for body in (ps_event("pay_x", "transfer.success"), ps_event("does-not-exist"), b"not json", b"{}"):
        assert post_ps(c, body).status_code == 200
    assert prov.sent("/transaction/verify/") == [] and me(c)["tier"] == "free"


def test_one_users_payment_cannot_be_completed_via_another_providers_webhook(c, prov):
    signed_in(c)
    pid = start(c, "btcpay")
    body = ps_event(pid)  # a valid Paystack-signed event naming a BTCPay payment
    assert post_ps(c, body).status_code == 200
    assert me(c)["tier"] == "free" and prov.sent("/transaction/verify/") == []


def test_polling_the_payment_completes_it_without_any_webhook(c, prov):
    signed_in(c)
    pid = start(c)
    assert c.get(f"/api/v1/billing/payments/{pid}").json()["status"] == "paid"  # return page after redirect
    assert me(c)["tier"] == "premium"


def test_payments_are_private_to_their_owner(make):
    a, b = make(), None
    signed_in(a)
    pid = start(a)
    a.post("/api/v1/auth/logout")
    a.post("/api/v1/auth/register", json={"email": "other@example.com", "password": "another long password"})
    assert a.get(f"/api/v1/billing/payments/{pid}").status_code == 404
    assert a.get("/api/v1/billing/payments").json() == []
    a.post("/api/v1/auth/logout")
    assert a.get(f"/api/v1/billing/payments/{pid}").status_code == 401 and b is None


# --- BTCPay webhook -----------------------------------------------------------

def post_btc(c, invoice="INV1", typ="InvoiceSettled", headers=None, body=None):
    body = body or json.dumps({"type": typ, "invoiceId": invoice}).encode()
    return c.post("/api/v1/billing/webhooks/btcpay", content=body, headers=headers or btc_sig(body))


def test_btcpay_settled_invoice_grants_plan(c, prov):
    signed_in(c)
    pid = start(c, "btcpay", "pro_30")
    prov.order_id = pid
    prov.invoice = {"status": "Settled", "amount": "29.99", "currency": "USD"}
    assert post_btc(c).status_code == 200
    assert me(c)["tier"] == "pro"
    assert post_btc(c).status_code == 200  # replay is harmless


def test_btcpay_bad_signature_and_foreign_invoice(c, prov):
    signed_in(c)
    pid = start(c, "btcpay")
    body = json.dumps({"type": "InvoiceSettled", "invoiceId": "INV1"}).encode()
    assert post_btc(c, body=body, headers=btc_sig(body, "wrong")).status_code == 401
    assert post_btc(c, body=body, headers={"content-type": "application/json"}).status_code == 401
    prov.order_id = "someone-elses-order"  # invoice exists but belongs to a different payment
    assert post_btc(c).status_code == 200 and me(c)["tier"] == "free"
    assert c.get(f"/api/v1/billing/payments/{pid}").json()["status"] == "pending"


@pytest.mark.parametrize("invoice,expected", [
    ({"status": "Processing", "amount": "9.99", "currency": "USD"}, "pending"),
    ({"status": "Expired", "amount": "9.99", "currency": "USD"}, "expired"),
    ({"status": "Invalid", "amount": "9.99", "currency": "USD"}, "failed"),
    ({"status": "Settled", "amount": "1.00", "currency": "USD"}, "pending"),   # underpaid: held for review, no grant
])
def test_btcpay_other_statuses(c, prov, invoice, expected):
    signed_in(c)
    pid = start(c, "btcpay")
    prov.order_id, prov.invoice = pid, invoice
    post_btc(c)
    assert c.get(f"/api/v1/billing/payments/{pid}").json()["status"] == expected
    assert me(c)["tier"] == "free"


# --- entitlement expiry & concurrency -------------------------------------------

def test_expired_plan_falls_back_to_free_limits(make):
    c = make(quotas_enabled=True)
    uid = signed_in(c)
    c.portal.call(c.app.state.db.set_tier, uid, "premium", time.time() - 10)
    u = me(c)
    assert u["tier"] == "free" and u["tier_expires"] is None and u["limits"]["max_minutes"] == 20
    assert c.post("/api/v1/jobs", json={"url": "https://example.com/v", "preset": "720p", "start": 1, "end": 5}).status_code == 403
    c.portal.call(c.app.state.db.set_tier, uid, "premium", time.time() + DAY)
    assert me(c)["tier"] == "premium" and me(c)["limits"]["max_minutes"] == 180


def test_stale_pending_payments_expire(c, prov):
    uid = signed_in(c)
    pid = start(c)
    prov.paystack_verify = {"status": "abandoned", "amount": 999, "currency": "USD"}

    async def age():
        from sqlalchemy import update

        from hotload.db import Payment
        async with c.app.state.db.session() as s:
            await s.execute(update(Payment).where(Payment.id == pid).values(created=time.time() - 2 * DAY))
            await s.commit()

    c.portal.call(age)
    assert c.get(f"/api/v1/billing/payments/{pid}").json()["status"] == "expired" and uid


async def test_concurrent_completions_grant_exactly_once(tmp_path):
    db = Database(Settings(data_dir=tmp_path))
    await db.migrate()
    user = await db.add_user(email="race@example.com", password_hash="x")
    payment = await db.create_payment(user_id=user.id, provider="paystack", plan="premium_30", amount_minor=999, currency="USD")
    now = time.time()
    results = await asyncio.gather(*(db.complete_payment(payment.id, now) for _ in range(8)))
    assert results.count(True) == 1
    fresh = await db.user_by_id(user.id)
    assert fresh.tier == "premium" and abs(fresh.tier_expires - (now + 30 * DAY)) < 1
    await db.dispose()
