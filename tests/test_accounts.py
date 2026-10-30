import pytest
from fakeredis import FakeAsyncRedis
from fastapi.testclient import TestClient

from hotload import oauth
from hotload.api.app import create_app
from hotload.config import Settings

GOOD = {"email": "Ada@Example.com", "password": "correct horse battery"}


@pytest.fixture
def make_client(tmp_path, monkeypatch):
    monkeypatch.setattr("socket.getaddrinfo", lambda host, port, *a, **k: [(2, 1, 6, "", ("93.184.216.34", port))])
    stack, enq = [], []

    def make(**overrides):
        async def enqueue(job_id, _queue=None):
            enq.append(job_id)

        settings = Settings(**{"data_dir": tmp_path, "rate_limit_per_minute": 100, "secret_key": "test-secret", **overrides})
        app = create_app(settings, redis=FakeAsyncRedis(decode_responses=True), enqueue=enqueue)
        c = TestClient(app)
        c.__enter__()
        stack.append(c)
        c.enqueued = enq
        return c

    yield make
    for c in stack:
        c.__exit__(None, None, None)


@pytest.fixture
def c(make_client):
    return make_client()


def test_register_login_me_logout(c):
    r = c.post("/api/v1/auth/register", json=GOOD)
    assert r.status_code == 201 and r.json()["email"] == "ada@example.com" and r.json()["tier"] == "free"
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie and "hl_session=" in cookie
    assert "password_hash" not in r.text and "argon2" not in r.text
    assert c.get("/api/v1/me").json()["email"] == "ada@example.com"
    assert c.post("/api/v1/auth/logout").status_code == 204
    assert c.get("/api/v1/me").status_code == 401
    ok = c.post("/api/v1/auth/login", json={"email": " ADA@example.com ", "password": GOOD["password"]})
    assert ok.status_code == 200 and c.get("/api/v1/me").status_code == 200


def test_register_validation_and_duplicates(c):
    assert c.post("/api/v1/auth/register", json={**GOOD, "password": "short"}).status_code == 422
    assert c.post("/api/v1/auth/register", json={**GOOD, "email": "not-an-email"}).status_code == 422
    assert c.post("/api/v1/auth/register", json=GOOD).status_code == 201
    assert c.post("/api/v1/auth/register", json={**GOOD, "email": "ada@EXAMPLE.com"}).status_code == 409


def test_login_failures_are_uniform_and_rate_limited(c):
    c.post("/api/v1/auth/register", json=GOOD)
    c.post("/api/v1/auth/logout")
    wrong = c.post("/api/v1/auth/login", json={"email": GOOD["email"], "password": "wrong password!!"})
    unknown = c.post("/api/v1/auth/login", json={"email": "nobody@example.com", "password": "wrong password!!"})
    assert wrong.status_code == unknown.status_code == 401 and wrong.json() == unknown.json()
    codes = [c.post("/api/v1/auth/login", json={"email": GOOD["email"], "password": "nope nope nope"}).status_code
             for _ in range(12)]
    assert 429 in codes
    # the real password is also locked out for this address+email until the window passes
    assert c.post("/api/v1/auth/login", json=GOOD).status_code == 429


def test_tampered_or_foreign_session_is_rejected(c, make_client):
    c.post("/api/v1/auth/register", json=GOOD)
    token = c.cookies.get("hl_session")
    c.cookies.set("hl_session", token[:-3] + "xyz")
    assert c.get("/api/v1/me").status_code == 401
    other = make_client(secret_key="a-different-secret")  # tokens signed with another secret are invalid
    other.cookies.set("hl_session", token)
    assert other.get("/api/v1/me").status_code == 401


def test_closed_registration(make_client):
    assert make_client(registration_open=False).post("/api/v1/auth/register", json=GOOD).status_code == 403


def test_history_records_jobs_and_can_be_cleared(c):
    assert c.get("/api/v1/me/history").status_code == 401
    c.post("/api/v1/auth/register", json=GOOD)
    c.post("/api/v1/jobs", json={"url": "https://example.com/a", "preset": "mp3"})
    c.post("/api/v1/batches", json={"urls": ["https://example.com/b", "https://example.com/c"]})
    h = c.get("/api/v1/me/history").json()
    assert len(h) == 3 and {e["state"] for e in h} == {"queued"} and h[0]["url"].startswith("https://example.com/")
    assert c.delete("/api/v1/me/history").status_code == 204
    assert c.get("/api/v1/me/history").json() == []


def test_anonymous_jobs_are_not_recorded(c):
    c.post("/api/v1/jobs", json={"url": "https://example.com/a"})
    c.post("/api/v1/auth/register", json=GOOD)
    assert c.get("/api/v1/me/history").json() == []


def test_delete_account_requires_password_and_erases_data(c):
    c.post("/api/v1/auth/register", json=GOOD)
    c.post("/api/v1/jobs", json={"url": "https://example.com/a"})
    assert c.request("DELETE", "/api/v1/me", json={"password": "wrong wrong wrong"}).status_code == 403
    assert c.request("DELETE", "/api/v1/me", json={"password": GOOD["password"]}).status_code == 204
    assert c.get("/api/v1/me").status_code == 401
    assert c.post("/api/v1/auth/login", json=GOOD).status_code == 401
    c.post("/api/v1/auth/register", json=GOOD)  # same email can sign up again, with no old history
    assert c.get("/api/v1/me/history").json() == []


def test_cross_site_writes_are_blocked_but_same_origin_and_non_browser_pass(c):
    body = {"url": "https://example.com/a"}
    assert c.post("/api/v1/jobs", json=body, headers={"Origin": "https://evil.example"}).status_code == 403
    assert c.post("/api/v1/jobs", json=body, headers={"Origin": "null"}).status_code == 403
    assert c.post("/api/v1/jobs", json=body, headers={"Origin": "http://testserver"}).status_code == 202
    assert c.post("/api/v1/jobs", json=body).status_code == 202  # curl/SDK: no Origin header
    assert c.get("/api/v1/status", headers={"Origin": "https://evil.example"}).status_code == 200  # reads are fine


def test_allowed_origins_setting(make_client):
    c = make_client(allowed_origins=["chrome-extension://abc"])
    ok = c.post("/api/v1/jobs", json={"url": "https://example.com/a"}, headers={"Origin": "chrome-extension://abc"})
    assert ok.status_code == 202


# --- quotas and plans -------------------------------------------------------

def test_quotas_off_by_default_are_unlimited(c):
    for _ in range(15):
        assert c.post("/api/v1/jobs", json={"url": "https://example.com/a"}).status_code == 202


def test_anonymous_quota_and_duration_cap(make_client):
    c = make_client(quotas_enabled=True)
    for _ in range(10):
        assert c.post("/api/v1/jobs", json={"url": "https://example.com/a"}).status_code == 202
    r = c.post("/api/v1/jobs", json={"url": "https://example.com/a"})
    assert r.status_code == 429 and "Daily limit" in r.json()["detail"]
    store = c.app.state.store
    job = c.portal.call(store.get, c.enqueued[0])
    assert job.max_duration == 20 * 60


def test_batch_cost_counts_and_plan_batch_size(make_client):
    c = make_client(quotas_enabled=True)
    urls = [f"https://example.com/{i}" for i in range(4)]
    assert c.post("/api/v1/batches", json={"urls": urls}).status_code == 400  # anonymous plan: 3 per batch
    assert c.post("/api/v1/batches", json={"urls": urls[:3]}).status_code == 202
    assert c.post("/api/v1/batches", json={"urls": urls[:3]}).status_code == 202
    assert c.post("/api/v1/batches", json={"urls": urls[:3]}).status_code == 202  # 9 used
    assert c.post("/api/v1/batches", json={"urls": urls[:2]}).status_code == 429  # would exceed 10


def test_video_trim_needs_premium_audio_trim_is_free(make_client):
    c = make_client(quotas_enabled=True)
    video = {"url": "https://example.com/a", "preset": "720p", "start": 1, "end": 5}
    assert c.post("/api/v1/jobs", json=video).status_code == 403
    assert c.post("/api/v1/jobs", json={**video, "preset": "mp3"}).status_code == 202
    c.post("/api/v1/auth/register", json=GOOD)
    uid = c.get("/api/v1/me").json()["id"]
    assert c.post("/api/v1/jobs", json=video).status_code == 403
    c.portal.call(c.app.state.db.set_tier, uid, "premium")
    assert c.post("/api/v1/jobs", json=video).status_code == 202
    me = c.get("/api/v1/me").json()
    assert me["tier"] == "premium" and me["limits"]["max_minutes"] == 180 and me["used_12h"] == 1  # the anonymous job used the IP bucket


def test_quota_is_per_user_not_shared_with_anonymous_ip(make_client):
    c = make_client(quotas_enabled=True)
    for _ in range(10):
        c.post("/api/v1/jobs", json={"url": "https://example.com/a"})
    assert c.post("/api/v1/jobs", json={"url": "https://example.com/a"}).status_code == 429
    c.post("/api/v1/auth/register", json=GOOD)
    assert c.post("/api/v1/jobs", json={"url": "https://example.com/a"}).status_code == 202  # fresh bucket


# --- OAuth ------------------------------------------------------------------

@pytest.fixture
def oc(make_client):
    return make_client(github_client_id="gid", github_client_secret="gsecret")


def test_providers_and_unconfigured_oauth(c, oc):
    assert c.get("/api/v1/auth/providers").json() == {"password": True, "oauth": []}
    assert oc.get("/api/v1/auth/providers").json()["oauth"] == ["github"]
    assert c.get("/api/v1/auth/oauth/github/login", follow_redirects=False).status_code == 404
    assert oc.get("/api/v1/auth/oauth/google/login", follow_redirects=False).status_code == 404


def test_oauth_login_redirects_to_provider_with_state(oc):
    r = oc.get("/api/v1/auth/oauth/github/login", follow_redirects=False)
    loc = r.headers["location"]
    assert r.status_code == 302 and loc.startswith("https://github.com/login/oauth/authorize?")
    assert "client_id=gid" in loc and "state=" in loc and "callback" in loc


def _profile(monkeypatch, **kw):
    async def fake(oauth_client, provider, request):
        return oauth.Profile(**kw)

    monkeypatch.setattr(oauth, "fetch_profile", fake)


def test_oauth_callback_creates_user_and_signs_in(oc, monkeypatch):
    _profile(monkeypatch, sub="42", email="new@example.com", email_verified=True)
    r = oc.get("/api/v1/auth/oauth/github/callback", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    me = oc.get("/api/v1/me").json()
    assert me["email"] == "new@example.com" and me["provider"] == "github" and me["has_password"] is False
    oc.post("/api/v1/auth/logout")
    oc.get("/api/v1/auth/oauth/github/callback", follow_redirects=False)  # same identity logs into the same account
    assert oc.get("/api/v1/me").json()["email"] == "new@example.com"


def test_oauth_takes_over_a_password_account_only_when_provider_verified_email(oc, monkeypatch):
    oc.post("/api/v1/auth/register", json=GOOD)
    uid = oc.get("/api/v1/me").json()["id"]
    oc.post("/api/v1/auth/logout")
    _profile(monkeypatch, sub="7", email="ada@example.com", email_verified=False)
    r = oc.get("/api/v1/auth/oauth/github/callback", follow_redirects=False)
    assert "error=unverified_email" in r.headers["location"] and oc.get("/api/v1/me").status_code == 401  # no takeover
    _profile(monkeypatch, sub="7", email="ada@example.com", email_verified=True)
    oc.get("/api/v1/auth/oauth/github/callback", follow_redirects=False)
    me = oc.get("/api/v1/me").json()
    # same account (plan and history survive), but the password is gone: whoever registered it first can no longer use it
    assert me["id"] == uid and me["provider"] == "github" and me["has_password"] is False
    oc.post("/api/v1/auth/logout")
    assert oc.post("/api/v1/auth/login", json=GOOD).status_code == 401


def test_oauth_failure_and_closed_registration(make_client, monkeypatch):
    c = make_client(github_client_id="g", github_client_secret="s", registration_open=False)

    async def boom(*a, **k):
        raise RuntimeError("state mismatch")

    monkeypatch.setattr(oauth, "fetch_profile", boom)
    assert "error=oauth" in c.get("/api/v1/auth/oauth/github/callback", follow_redirects=False).headers["location"]
    _profile(monkeypatch, sub="1", email="x@example.com", email_verified=True)
    assert "error=closed" in c.get("/api/v1/auth/oauth/github/callback", follow_redirects=False).headers["location"]
