import hashlib
import hmac
import json
import time

import httpx
import pytest
from arq import Retry
from fakeredis import FakeAsyncRedis
from fastapi.testclient import TestClient

from hotload import auth, webhooks, worker
from hotload.api.app import create_app
from hotload.config import Settings
from hotload.db import Database
from hotload.jobs import JobStore

USER = {"email": "dev@example.com", "password": "correct horse battery"}
PUBLIC_IP = "93.184.216.34"


def public(host, port, *a, **k):
    return [(2, 1, 6, "", (PUBLIC_IP, port))]


@pytest.fixture
def make(tmp_path, monkeypatch):
    monkeypatch.setattr("socket.getaddrinfo", public)
    stack, queue = [], []

    def build(**overrides):
        async def enqueue(job_id, _queue=None):
            queue.append(job_id)

        cfg = {"data_dir": tmp_path, "rate_limit_per_minute": 1000, "secret_key": "s", **overrides}
        c = TestClient(create_app(Settings(**cfg), redis=FakeAsyncRedis(decode_responses=True), enqueue=enqueue))
        c.__enter__()
        c.queue = queue
        stack.append(c)
        return c

    yield build
    for c in stack:
        c.__exit__(None, None, None)


@pytest.fixture
def c(make):
    return make()


def register(c):
    r = c.post("/api/v1/auth/register", json=USER)
    assert r.status_code == 201
    return r.json()["id"]


def new_key(c, name="ci"):
    r = c.post("/api/v1/me/keys", json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()


def bearer(key):
    return {"authorization": f"Bearer {key}"}


# --- key lifecycle --------------------------------------------------------------------------------

def test_key_is_shown_once_and_only_its_hash_is_stored(c):
    register(c)
    k = new_key(c)
    assert k["key"].startswith(f"hl_{k['id']}_") and k["webhook_secret"]
    listed = c.get("/api/v1/me/keys").json()
    assert [x["id"] for x in listed] == [k["id"]]
    assert "key" not in listed[0] and "webhook_secret" not in listed[0] and "key_hash" not in listed[0]
    secret = k["key"].split("_", 2)[2]
    row = c.portal.call(c.app.state.db.get_key, k["id"])
    assert row.key_hash == auth.hash_secret(secret) and secret not in row.key_hash and secret not in str(row.public())


def test_key_management_needs_a_browser_session(c):
    assert c.post("/api/v1/me/keys", json={"name": "x"}).status_code == 401
    register(c)
    k = new_key(c)
    c.post("/api/v1/auth/logout")
    h = bearer(k["key"])
    assert c.post("/api/v1/me/keys", json={"name": "minted"}, headers=h).status_code == 403  # a leaked key cannot mint keys
    assert c.get("/api/v1/me/keys", headers=h).status_code == 403
    assert c.delete(f"/api/v1/me/keys/{k['id']}", headers=h).status_code == 403


def test_api_key_authenticates_requests_and_revocation_works(c):
    register(c)
    k = new_key(c)
    c.post("/api/v1/auth/logout")
    ok = c.post("/api/v1/jobs", json={"url": "https://example.com/v"}, headers=bearer(k["key"]))
    assert ok.status_code == 202
    assert c.get("/api/v1/me/usage", headers=bearer(k["key"])).status_code == 200
    # sign in again to revoke
    c.post("/api/v1/auth/login", json=USER)
    assert c.delete(f"/api/v1/me/keys/{k['id']}").status_code == 204
    assert c.delete(f"/api/v1/me/keys/{k['id']}").status_code == 404
    c.post("/api/v1/auth/logout")
    assert c.post("/api/v1/jobs", json={"url": "https://example.com/v"}, headers=bearer(k["key"])).status_code == 401
    assert c.get("/api/v1/me/keys").status_code == 401


@pytest.mark.parametrize("token", ["hl_nope_nope", "hl_", "hl__", "hl_abc", "Bearer", "hl_aaaaaaaaaaaa_" + "x" * 43])
def test_malformed_or_unknown_keys_are_rejected(c, token):
    r = c.post("/api/v1/jobs", json={"url": "https://example.com/v"}, headers={"authorization": f"Bearer {token}"})
    assert r.status_code in (401, 202)  # "Bearer" alone / non-hl tokens are not treated as keys (anonymous)
    if token.startswith("hl_"):
        assert r.status_code == 401


def test_wrong_secret_for_a_real_key_id_is_rejected(c):
    register(c)
    k = new_key(c)
    forged = f"hl_{k['id']}_{'A' * 43}"
    assert c.post("/api/v1/jobs", json={"url": "https://example.com/v"}, headers=bearer(forged)).status_code == 401


def test_a_bad_key_never_falls_back_to_the_logged_in_cookie(c):
    register(c)  # cookie is present
    r = c.post("/api/v1/jobs", json={"url": "https://example.com/v"}, headers=bearer("hl_deadbeef0000_wrong"))
    assert r.status_code == 401


def test_pro_plan_gates_keys_when_quotas_are_enforced(make):
    c = make(quotas_enabled=True)
    uid = register(c)
    assert c.post("/api/v1/me/keys", json={"name": "x"}).status_code == 403
    c.portal.call(c.app.state.db.set_tier, uid, "pro", time.time() + 86400)
    k = new_key(c)
    c.post("/api/v1/auth/logout")
    assert c.post("/api/v1/jobs", json={"url": "https://example.com/v"}, headers=bearer(k["key"])).status_code == 202
    # plan lapses: the key stops working until renewed
    c.portal.call(c.app.state.db.set_tier, uid, "pro", time.time() - 5)
    r = c.post("/api/v1/jobs", json={"url": "https://example.com/v"}, headers=bearer(k["key"]))
    assert r.status_code == 403 and "Pro" in r.json()["detail"]


def test_key_limit_per_account(c):
    register(c)
    for i in range(10):
        new_key(c, f"k{i}")
    assert c.post("/api/v1/me/keys", json={"name": "eleventh"}).status_code == 409


def test_keys_have_their_own_rate_limit_bucket(make):
    c = make(api_rate_limit_per_minute=3, rate_limit_per_minute=1000)
    register(c)
    k = new_key(c)
    c.post("/api/v1/auth/logout")
    codes = [c.post("/api/v1/jobs", json={"url": "https://example.com/v"}, headers=bearer(k["key"])).status_code for _ in range(5)]
    assert codes == [202, 202, 202, 429, 429]
    assert c.post("/api/v1/jobs", json={"url": "https://example.com/v"}).status_code == 202  # anonymous IP bucket untouched


def test_usage_metering_per_account_and_per_key(c):
    register(c)
    k = new_key(c)
    h = bearer(k["key"])
    c.post("/api/v1/jobs", json={"url": "https://example.com/a"}, headers=h)
    c.post("/api/v1/batches", json={"urls": ["https://example.com/b", "https://example.com/c"]}, headers=h)
    c.post("/api/v1/jobs", json={"url": "https://example.com/d"})  # cookie session, not the key
    by_key = c.get("/api/v1/me/usage", headers=h).json()
    assert by_key["key"][0]["jobs"] == 3 and by_key["account"][0]["jobs"] == 4
    by_cookie = c.get("/api/v1/me/usage").json()
    assert by_cookie["key"] is None and by_cookie["account"][0]["jobs"] == 4


# --- webhook_url on jobs ---------------------------------------------------------------------------

def test_webhook_url_requires_a_key_https_and_a_public_host(make, monkeypatch):
    c = make()
    register(c)
    k = new_key(c)
    h = bearer(k["key"])
    body = {"url": "https://example.com/v", "webhook_url": "https://hooks.example.com/in"}
    assert c.post("/api/v1/jobs", json=body).status_code == 400                                       # cookie session: no key
    assert c.post("/api/v1/jobs", json={**body, "webhook_url": "http://hooks.example.com/in"}, headers=h).status_code == 400
    assert c.post("/api/v1/jobs", json={**body, "webhook_url": "https://169.254.169.254/x"}, headers=h).status_code == 400

    def realistic(host, port, *a, **k):  # like real DNS: "localhost" is loopback, everything else public
        return [(2, 1, 6, "", ("127.0.0.1" if host == "localhost" else PUBLIC_IP, port))]

    monkeypatch.setattr("socket.getaddrinfo", realistic)
    assert c.post("/api/v1/jobs", json={**body, "webhook_url": "https://localhost/x"}, headers=h).status_code == 400
    ok = c.post("/api/v1/jobs", json=body, headers=h)
    assert ok.status_code == 202
    assert "webhook_url" not in ok.json() and "key_id" not in ok.json()
    job = c.portal.call(c.app.state.store.get, ok.json()["id"])
    assert job.webhook_url == "https://hooks.example.com/in" and job.key_id == k["id"]


# --- delivery ----------------------------------------------------------------------------------------

def test_signature_is_verifiable_by_the_receiver():
    body, headers = webhooks.build({"id": "j1", "state": "done"}, "whsec", now=1700000000)
    ts = headers["x-hotload-timestamp"]
    expected = "sha256=" + hmac.new(b"whsec", f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    assert headers["x-hotload-signature"] == expected and ts == "1700000000"
    assert json.loads(body) == {"event": "job.finished", "job": {"id": "j1", "state": "done"}, "sent_at": 1700000000}
    assert webhooks.sign("other", 1700000000, body) != expected  # a different secret gives a different signature


def test_pin_resolves_once_and_connects_to_the_validated_ip():
    calls = []

    def rebinding(host, port):
        calls.append(host)  # first answer is public; a second lookup would return an internal address
        return [(2, 1, 6, "", (PUBLIC_IP if len(calls) == 1 else "10.0.0.5", port))]

    target, host = webhooks._pin("https://hooks.example.com:8443/in?x=1", Settings(), rebinding)
    assert target == f"https://{PUBLIC_IP}:8443/in?x=1" and host == "hooks.example.com" and len(calls) == 1


def test_pin_refuses_private_answers_even_if_one_is_public():
    def mixed(host, port):
        return [(2, 1, 6, "", (PUBLIC_IP, port)), (2, 1, 6, "", ("192.168.1.9", port))]

    with pytest.raises(Exception, match="non-public"):
        webhooks._pin("https://hooks.example.com/in", Settings(), mixed)


class Receiver:
    def __init__(self, *statuses):
        self.statuses = list(statuses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        status = self.statuses.pop(0) if self.statuses else 200
        return httpx.Response(status, headers={"location": "http://10.0.0.5/steal"} if status in (301, 302) else {})


async def send(receiver, url="https://hooks.example.com/in"):
    async with httpx.AsyncClient(transport=httpx.MockTransport(receiver)) as client:
        body, headers = webhooks.build({"id": "j"}, "sec")
        return await webhooks.deliver(url, body, headers, Settings(), client, resolve=public)


async def test_delivery_goes_to_the_pinned_ip_with_host_sni_and_signature():
    r = Receiver(200)
    assert await send(r) == 200
    req = r.requests[0]
    assert req.url.host == PUBLIC_IP and req.headers["host"] == "hooks.example.com"
    assert req.extensions["sni_hostname"] == "hooks.example.com"
    assert req.headers["x-hotload-signature"].startswith("sha256=") and req.method == "POST"


@pytest.mark.parametrize("status,retryable", [(500, True), (503, True), (429, True), (408, True), (404, False), (400, False), (302, False)])
async def test_delivery_retry_classification_and_redirects_not_followed(status, retryable):
    r = Receiver(status)
    with pytest.raises(webhooks.WebhookError) as e:
        await send(r)
    assert e.value.retryable is retryable
    assert len(r.requests) == 1  # a redirect to an internal address was NOT followed


async def test_delivery_refuses_unsafe_destinations_without_connecting():
    r = Receiver(200)
    with pytest.raises(webhooks.WebhookError) as e:
        await send(r, "https://10.0.0.5/in")
    assert e.value.retryable is False and r.requests == []


async def test_network_errors_are_retryable():
    def boom(request):
        raise httpx.ConnectError("refused")

    async with httpx.AsyncClient(transport=httpx.MockTransport(boom)) as client:
        body, headers = webhooks.build({}, "s")
        with pytest.raises(webhooks.WebhookError) as e:
            await webhooks.deliver("https://hooks.example.com/in", body, headers, Settings(), client, resolve=public)
    assert e.value.retryable is True


# --- worker ------------------------------------------------------------------------------------------

@pytest.fixture
async def wctx(tmp_path, monkeypatch):
    monkeypatch.setattr("socket.getaddrinfo", public)
    settings = Settings(data_dir=tmp_path, webhook_max_attempts=3)
    monkeypatch.setattr(worker, "get_settings", lambda: settings)
    db = Database(settings)
    await db.migrate()
    user = await db.add_user(email="w@example.com", password_hash="x")
    full, key_id, h, secret = auth.new_api_key()
    await db.create_key(user.id, "k", h, secret, key_id)
    redis = FakeAsyncRedis(decode_responses=True)
    store = JobStore(redis, 600)
    job = await store.create(url="https://example.com/v", preset="mp3", user_id=user.id, key_id=key_id,
                             webhook_url="https://hooks.example.com/in")
    job.state, job.filename = "done", "v.mp3"
    await store.save(job)
    receiver = Receiver()
    ctx = {"store": store, "db": db, "http": httpx.AsyncClient(transport=httpx.MockTransport(receiver)), "job_try": 1}
    yield ctx, job, receiver, secret, user, key_id
    await ctx["http"].aclose()
    await db.dispose()


async def test_worker_delivers_signed_webhook(wctx):
    ctx, job, receiver, secret, *_ = wctx
    await worker.deliver_webhook(ctx, job.id)
    [req] = receiver.requests
    ts = req.headers["x-hotload-timestamp"]
    assert req.headers["x-hotload-signature"] == "sha256=" + hmac.new(secret.encode(), f"{ts}.".encode() + req.content, hashlib.sha256).hexdigest()
    payload = json.loads(req.content)
    assert payload["job"]["id"] == job.id and payload["job"]["state"] == "done" and "download_url" in payload["job"]
    assert "webhook_url" not in payload["job"] and "key_id" not in payload["job"]


async def test_worker_retries_with_backoff_then_gives_up(wctx):
    ctx, job, receiver, *_ = wctx
    receiver.statuses = [500, 500, 500]
    with pytest.raises(Retry) as r1:
        await worker.deliver_webhook({**ctx, "job_try": 1}, job.id)
    with pytest.raises(Retry) as r2:
        await worker.deliver_webhook({**ctx, "job_try": 2}, job.id)
    assert r1.value.defer_score < r2.value.defer_score  # backoff grows
    await worker.deliver_webhook({**ctx, "job_try": 3}, job.id)  # max attempts reached: stops quietly
    assert len(receiver.requests) == 3


async def test_worker_does_not_retry_permanent_failures(wctx):
    ctx, job, receiver, *_ = wctx
    receiver.statuses = [410]
    await worker.deliver_webhook(ctx, job.id)
    assert len(receiver.requests) == 1


async def test_revoked_key_stops_receiving_webhooks(wctx):
    ctx, job, receiver, _, user, key_id = wctx
    await ctx["db"].revoke_key(user.id, key_id)
    await worker.deliver_webhook(ctx, job.id)
    assert receiver.requests == []


async def test_finished_job_enqueues_a_webhook_only_when_configured(tmp_path, monkeypatch):
    from hotload import engine
    from hotload.proxies import ProxyPool

    settings = Settings(data_dir=tmp_path)
    monkeypatch.setattr(worker, "get_settings", lambda: settings)
    monkeypatch.setattr(worker, "_pool", ProxyPool())

    def fake_download(url, s, workdir, preset, proxy=None, **kw):
        workdir.mkdir(parents=True, exist_ok=True)
        (workdir / "v.mp3").write_bytes(b"x")
        return engine.DownloadResult(workdir / "v.mp3", "T", "Generic")

    monkeypatch.setattr(engine, "download", fake_download)

    class Queue:
        def __init__(self):
            self.jobs = []

        async def enqueue_job(self, name, *args):
            self.jobs.append((name, args))

    store = JobStore(FakeAsyncRedis(decode_responses=True), 600)
    q = Queue()
    plain = await store.create(url="https://example.com/a", preset="mp3")
    hooked = await store.create(url="https://example.com/b", preset="mp3", key_id="k1", webhook_url="https://hooks.example.com/in")
    for j in (plain, hooked):
        await worker.run_download({"store": store, "redis": q}, j.id)
    assert q.jobs == [("deliver_webhook", (hooked.id,))]
