import logging
import time

import pytest
from fakeredis import FakeAsyncRedis
from fastapi.testclient import TestClient

from hotload import admin, blocklist, engine, worker
from hotload.api.app import create_app
from hotload.blocklist import Blocklist
from hotload.config import Settings
from hotload.db import BlockedItem, Database
from hotload.jobs import JobStore

PW = "correct horse battery"
PUBLIC = [(2, 1, 6, "", ("93.184.216.34", 443))]


@pytest.fixture
def make(tmp_path, monkeypatch):
    monkeypatch.setattr("socket.getaddrinfo", lambda host, port, *a, **k: [(2, 1, 6, "", ("93.184.216.34", port))])
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


def signup(c, email):
    r = c.post("/api/v1/auth/register", json={"email": email, "password": PW})
    assert r.status_code == 201
    return r.json()["id"]


def as_admin(c, email="boss@example.com"):
    signup(c, email)
    assert c.portal.call(c.app.state.db.set_admin, email, True)
    c.post("/api/v1/auth/logout")
    assert c.post("/api/v1/auth/login", json={"email": email, "password": PW}).json()["is_admin"] is True


def block(c, kind, value, reason=""):
    return c.post("/api/v1/admin/blocklist", json={"kind": kind, "value": value, "reason": reason})


# --- who can be an admin ------------------------------------------------------------------------

def test_admin_routes_require_a_signed_in_admin(c):
    for method, path in [("get", "/api/v1/admin/overview"), ("get", "/api/v1/admin/reports"), ("get", "/api/v1/admin/blocklist")]:
        assert getattr(c, method)(path).status_code == 401
    signup(c, "user@example.com")
    assert c.get("/api/v1/admin/overview").status_code == 403
    assert block(c, "host", "example.org").status_code == 403
    assert c.post("/api/v1/admin/users/tier", json={"email": "user@example.com", "tier": "pro"}).status_code == 403


def test_admin_comes_from_the_server_flag_never_from_an_email_address(c):
    uid = signup(c, "admin@example.com")  # a stranger registering an "admin-looking" address gets nothing
    assert c.get("/api/v1/me").json()["is_admin"] is False
    c.portal.call(c.app.state.db.set_admin, "admin@example.com", True)
    assert c.get("/api/v1/admin/overview").status_code == 200 and uid
    c.portal.call(c.app.state.db.set_admin, "admin@example.com", False)
    assert c.get("/api/v1/admin/overview").status_code == 403  # revocation is immediate


def test_api_keys_cannot_reach_admin_routes_even_for_an_admin_owner(c):
    as_admin(c)
    key = c.post("/api/v1/me/keys", json={"name": "k"}).json()["key"]
    c.post("/api/v1/auth/logout")
    h = {"authorization": f"Bearer {key}"}
    assert c.get("/api/v1/admin/overview", headers=h).status_code == 403
    assert c.post("/api/v1/admin/blocklist", json={"kind": "host", "value": "x.org"}, headers=h).status_code == 403


async def test_admin_cli_grants_and_revokes(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path)
    monkeypatch.setattr(admin, "get_settings", lambda: settings)
    db = Database(settings)
    await db.migrate()
    await db.add_user(email="ops@example.com", password_hash="x")
    assert await admin.run("grant", "ops@example.com") == 0
    assert (await db.user_by_email("ops@example.com")).is_admin is True
    assert await admin.run("revoke", "OPS@example.com") == 0
    assert (await db.user_by_email("ops@example.com")).is_admin is False
    assert await admin.run("grant", "nobody@example.com") == 1
    await db.dispose()


# --- public reports -----------------------------------------------------------------------------

def test_report_form_validation_storage_and_rate_limit(c, caplog):
    ok = {"url": "https://example.com/watch?v=1", "kind": "copyright", "contact": "owner@studio.example", "details": "mine"}
    assert c.post("/api/v1/reports", json={**ok, "url": "ftp://example.com/x"}).status_code == 422
    assert c.post("/api/v1/reports", json={**ok, "url": "javascript:alert(1)"}).status_code == 422
    assert c.post("/api/v1/reports", json={**ok, "contact": "nope"}).status_code == 422
    assert c.post("/api/v1/reports", json={**ok, "kind": "spam"}).status_code == 422
    assert c.post("/api/v1/reports", json={**ok, "details": "x" * 2001}).status_code == 422
    with caplog.at_level(logging.WARNING):
        assert c.post("/api/v1/reports", json=ok).status_code == 201
        assert c.post("/api/v1/reports", json={**ok, "kind": "csam"}).status_code == 201
    assert any(r.levelno == logging.ERROR and "report received" in r.message for r in caplog.records)  # CSAM pages ops
    assert c.post("/api/v1/reports", json=ok).status_code == 201
    assert c.post("/api/v1/reports", json=ok).status_code == 429


def test_reporting_does_not_fetch_the_reported_url(c, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("must not touch the network")

    monkeypatch.setattr(engine, "extract", boom)
    monkeypatch.setattr("socket.getaddrinfo", boom)
    r = c.post("/api/v1/reports", json={"url": "http://169.254.169.254/x", "kind": "abuse", "contact": "a@b.example"})
    assert r.status_code == 201


# --- enforcement --------------------------------------------------------------------------------

def test_host_rule_blocks_subdomains_across_every_entry_point(c, monkeypatch):
    monkeypatch.setattr(engine, "extract", lambda url, s, proxy=None, **k: {"title": "t", "site": "X", "id": "1", "age_limit": 0})
    monkeypatch.setattr(engine, "extract_playlist", lambda url, s, proxy=None, limit=50: {"playlist": True, "title": "p", "count": 2, "entries": [
        {"url": "https://bad.example.org/a", "title": "a"}, {"url": "https://fine.example.com/b", "title": "b"}]})
    as_admin(c)
    assert block(c, "host", "https://Example.org/anything", "takedown #1").status_code == 201  # normalised to the host
    assert block(c, "host", "example.org").status_code == 409
    for url in ("https://example.org/v", "https://cdn.sub.example.org/v"):
        assert c.post("/api/v1/extract", json={"url": url}).status_code == 451
        assert c.post("/api/v1/jobs", json={"url": url}).status_code == 451
        assert c.post("/api/v1/batches", json={"urls": ["https://fine.example.com/ok", url]}).status_code == 451
    assert c.post("/api/v1/extract", json={"url": "https://notexample.org/v"}).status_code == 200  # suffix lookalike is fine
    pl = c.post("/api/v1/playlist", json={"url": "https://fine.example.com/list"}).json()
    assert [e["title"] for e in pl["entries"]] == ["b"]
    assert c.queue == []


def test_url_rule_is_exact_after_normalisation(c):
    as_admin(c)
    assert block(c, "url", "https://Example.com/Watch/?v=1#frag").status_code == 201
    for blocked in ("https://example.com/Watch?v=1", "https://EXAMPLE.com/Watch/?v=1#other"):
        assert c.post("/api/v1/jobs", json={"url": blocked}).status_code == 451
    assert c.post("/api/v1/jobs", json={"url": "https://example.com/Watch?v=2"}).status_code == 202


def test_media_rule_blocks_by_id_however_the_link_is_written(c, monkeypatch):
    def fake(url, s, proxy=None, **k):
        vid = "jNQXAC9IVRw" if "jNQ" in url else "other"
        return {"title": "t", "site": "Youtube", "id": vid, "age_limit": 0}

    monkeypatch.setattr(engine, "extract", fake)
    as_admin(c)
    assert block(c, "media", "badformat").status_code == 422
    assert block(c, "media", "Youtube:jNQXAC9IVRw", "dmca 123").status_code == 201
    for link in ("https://www.youtube.com/watch?v=jNQXAC9IVRw", "https://youtu.be/jNQXAC9IVRw?t=5", "https://m.youtube.com/watch?v=jNQXAC9IVRw&x=1"):
        assert c.post("/api/v1/extract", json={"url": link}).status_code == 451
    assert c.post("/api/v1/extract", json={"url": "https://www.youtube.com/watch?v=zzz"}).status_code == 200


def test_removing_a_rule_restores_access_immediately(c):
    as_admin(c)
    rule = block(c, "host", "example.org").json()
    assert c.post("/api/v1/jobs", json={"url": "https://example.org/v"}).status_code == 451
    assert c.delete(f"/api/v1/admin/blocklist/{rule['id']}").status_code == 204
    assert c.delete(f"/api/v1/admin/blocklist/{rule['id']}").status_code == 404
    assert c.post("/api/v1/jobs", json={"url": "https://example.org/v"}).status_code == 202
    assert c.get("/api/v1/admin/blocklist").json() == []


async def test_worker_rechecks_rules_for_jobs_queued_before_the_takedown(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path)
    monkeypatch.setattr(worker, "get_settings", lambda: settings)
    db = Database(settings)
    await db.migrate()
    await db.add_block("host", "example.org", "dmca", None)

    def must_not_download(*a, **k):
        raise AssertionError("a blocked job must never reach the downloader")

    monkeypatch.setattr(engine, "download", must_not_download)
    store = JobStore(FakeAsyncRedis(decode_responses=True), 600)
    job = await store.create(url="https://media.example.org/v", preset="mp3")
    await worker.run_download({"store": store, "db": db}, job.id)
    done = await store.get(job.id)
    assert done.state == "error" and done.message == "This content is not available"
    await db.dispose()


def test_engine_filter_refuses_blocked_media():
    f = engine._duration_filter(600, None, None, False, frozenset({"Youtube:abc"}))
    assert f({"extractor_key": "Youtube", "id": "abc", "duration": 5}, incomplete=False) == "This content is not available"
    assert f({"extractor_key": "Youtube", "id": "zzz", "duration": 5}, incomplete=False) is None
    assert f({"extractor_key": "Vimeo", "id": "abc", "duration": 5}, incomplete=False) is None


# --- resolving reports, grants, overview ------------------------------------------------------------

def test_resolve_report_with_takedown_in_one_step(c):
    r = c.post("/api/v1/reports", json={"url": "https://example.org/v/9", "kind": "copyright", "contact": "o@x.example"}).json()
    as_admin(c)
    assert [x["id"] for x in c.get("/api/v1/admin/reports?status=open").json()] == [r["id"]]
    assert c.post(f"/api/v1/admin/reports/{r['id']}/resolve", json={"status": "bogus"}).status_code == 422
    assert c.post("/api/v1/admin/reports/nope/resolve", json={"status": "rejected"}).status_code == 404
    out = c.post(f"/api/v1/admin/reports/{r['id']}/resolve",
                 json={"status": "actioned", "block": {"kind": "url", "value": "https://example.org/v/9", "reason": "dmca"}})
    assert out.status_code == 200 and out.json()["status"] == "actioned" and out.json()["resolved"]
    assert c.get("/api/v1/admin/reports?status=open").json() == []
    assert c.post("/api/v1/jobs", json={"url": "https://example.org/v/9"}).status_code == 451
    assert c.get("/api/v1/admin/overview").json()["open_reports"] == 0


def test_admin_can_grant_and_clear_a_plan(c):
    c2 = c
    signup(c2, "customer@example.com")
    c2.post("/api/v1/auth/logout")
    as_admin(c2)
    r = c2.post("/api/v1/admin/users/tier", json={"email": "Customer@example.com", "tier": "premium", "days": 30})
    assert r.status_code == 200 and r.json()["tier"] == "premium" and abs(r.json()["tier_expires"] - (time.time() + 30 * 86400)) < 10
    assert c2.post("/api/v1/admin/users/tier", json={"email": "customer@example.com", "tier": "free"}).json()["tier"] == "free"
    assert c2.post("/api/v1/admin/users/tier", json={"email": "ghost@example.com", "tier": "pro"}).status_code == 404
    assert c2.post("/api/v1/admin/users/tier", json={"email": "customer@example.com", "tier": "gold"}).status_code == 422


def test_overview_counts(c):
    c.post("/api/v1/reports", json={"url": "https://example.org/a", "kind": "abuse", "contact": "a@b.example"})
    as_admin(c)
    block(c, "host", "example.org")
    o = c.get("/api/v1/admin/overview").json()
    assert o == {"users": 1, "paid_payments": 0, "open_reports": 1, "held_payments": 0, "blocked": 1}


# --- blocklist unit -------------------------------------------------------------------------------------

def test_blocklist_normalisation_and_matching():
    assert blocklist.normalise_value("host", "https://WWW.Example.org/path") == "www.example.org"
    assert blocklist.normalise_value("host", ".Example.org.") == "example.org"
    assert blocklist.normalise_value("url", "HTTPS://Example.com/A/#x") == "https://example.com/A"
    items = [BlockedItem(kind="host", value="example.org"), BlockedItem(kind="media", value="Site:1")]
    rules = Blocklist(items)
    assert rules.blocks_url("https://a.b.example.org/x") and not rules.blocks_url("https://example.org.evil.com/x")
    assert rules.blocks_media("Site", "1") and not rules.blocks_media("Site", "2") and not rules.blocks_media(None, "1")


def test_blocked_hosts_answer_451_even_when_dns_fails(c, monkeypatch):
    """Takedowns are checked before any DNS lookup, so they hold even for names that no longer resolve."""
    import socket

    def nxdomain(*a, **k):
        raise socket.gaierror("Name or service not known")

    as_admin(c)
    assert block(c, "host", "gone.example.org").status_code == 201
    monkeypatch.setattr("socket.getaddrinfo", nxdomain)
    assert c.post("/api/v1/jobs", json={"url": "https://cdn.gone.example.org/x"}).status_code == 451
    assert c.post("/api/v1/extract", json={"url": "https://gone.example.org/x"}).status_code == 451
    assert c.post("/api/v1/batches", json={"urls": ["https://gone.example.org/x"]}).status_code == 451
    assert c.post("/api/v1/jobs", json={"url": "https://other.example.org/x"}).status_code == 400  # unresolvable, not blocked
