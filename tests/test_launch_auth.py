"""Regression tests for the launch-readiness security review (account, session and request-handling findings)."""

import httpx
import pytest
from fakeredis import FakeAsyncRedis
from fastapi.testclient import TestClient

from hotload import oauth
from hotload.api.app import create_app
from hotload.config import Settings

PW = "correct horse battery"


@pytest.fixture
def make(tmp_path, monkeypatch):
    monkeypatch.setattr("socket.getaddrinfo", lambda host, port, *a, **k: [(2, 1, 6, "", ("93.184.216.34", port))])
    stack, queued = [], []

    def build(**o):
        async def enqueue(job_id, _q=None):
            queued.append(job_id)

        cfg = {"data_dir": tmp_path, "rate_limit_per_minute": 10000, "secret_key": "s", **o}
        c = TestClient(create_app(Settings(**cfg), redis=FakeAsyncRedis(decode_responses=True), enqueue=enqueue))
        c.__enter__()
        c.queued = queued
        stack.append(c)
        return c

    yield build
    for c in stack:
        c.__exit__(None, None, None)


def signup(c, email="u@example.com"):
    r = c.post("/api/v1/auth/register", json={"email": email, "password": PW})
    assert r.status_code == 201
    return r.json()["id"]


def redis_keys(c, pattern):
    return c.portal.call(c.app.state.redis.keys, pattern)


# --- pre-hijack of an unverified registration via OAuth -----------------------------------------------------

def test_oauth_takeover_cuts_off_everything_the_pre_registrant_held(make, monkeypatch):
    c = make(github_client_id="g", github_client_secret="s", cookie_key=None)
    # the attacker registers the victim's address with a password they know, and makes themselves comfortable
    signup(c, "victim@gmail.com")
    attacker_cookie = c.cookies.get("hl_session")
    key = c.post("/api/v1/me/keys", json={"name": "backdoor"}).json()["key"]
    c.post("/api/v1/jobs", json={"url": "https://example.com/a"})
    c.post("/api/v1/auth/logout")

    async def verified(oauth_client, provider, request):
        return oauth.Profile(sub="victim-gh-1", email="victim@gmail.com", email_verified=True)

    monkeypatch.setattr(oauth, "fetch_profile", verified)
    c.get("/api/v1/auth/oauth/github/callback", follow_redirects=False)  # the real owner signs in with the provider
    me = c.get("/api/v1/me").json()
    assert me["email"] == "victim@gmail.com" and me["has_password"] is False

    c.cookies.clear()
    c.cookies.set("hl_session", attacker_cookie)
    assert c.get("/api/v1/me").status_code == 401, "the attacker's existing session must be dead"
    c.cookies.clear()
    assert c.post("/api/v1/auth/login", json={"email": "victim@gmail.com", "password": PW}).status_code == 401
    keyed = c.post("/api/v1/jobs", json={"url": "https://example.com/b"}, headers={"authorization": f"Bearer {key}"})
    assert keyed.status_code == 401, "the attacker's API key must be gone"


# --- revocable sessions -----------------------------------------------------------------------------------------

def test_logout_all_revokes_every_session_including_copies(make):
    c = make()
    signup(c)
    copied = c.cookies.get("hl_session")  # e.g. stolen from a shared computer
    assert c.post("/api/v1/auth/logout").status_code == 204
    c.cookies.set("hl_session", copied)
    assert c.get("/api/v1/me").status_code == 200  # plain logout only clears this browser (stateless sessions)
    assert c.post("/api/v1/auth/logout-all").status_code == 204
    c.cookies.set("hl_session", copied)
    assert c.get("/api/v1/me").status_code == 401  # now the copy is dead too
    assert c.post("/api/v1/auth/login", json={"email": "u@example.com", "password": PW}).status_code == 200  # but login works


def test_logout_all_requires_a_session(make):
    assert make().post("/api/v1/auth/logout-all").status_code == 401


# --- cookies never ride along with an API key ---------------------------------------------------------------------

def test_a_request_with_an_api_key_never_uses_the_cookie_session(make):
    c = make()
    signup(c)
    key = c.post("/api/v1/me/keys", json={"name": "k"}).json()["key"]
    h = {"authorization": f"Bearer {key}"}
    # cookie-only routes ignore the cookie as soon as a key header is present, so a hostile page that could add
    # an Authorization header still could not combine it with the victim's cookie
    assert c.get("/api/v1/me/history").status_code == 200
    assert c.get("/api/v1/me/history", headers=h).status_code == 401
    assert c.get("/api/v1/billing/payments", headers=h).status_code == 401
    assert c.request("DELETE", "/api/v1/me", json={"password": PW}, headers=h).status_code == 401
    assert c.get("/api/v1/me").status_code == 200  # the cookie still works when no key header is sent


# --- client IP: right-most trusted hop ---------------------------------------------------------------------------

def attempt(c, ip_header=None):
    h = {"x-forwarded-for": ip_header} if ip_header is not None else {}
    return c.post("/api/v1/auth/login", json={"email": "x@example.com", "password": "wrong password"}, headers=h)


def seen_ips(c):
    return {k.split(":")[3] for k in redis_keys(c, "hotload:rl:loginip:*")}  # hotload:rl:loginip:<ip>:<window>


def test_forwarded_for_is_ignored_unless_a_proxy_is_trusted(make):
    c = make()
    attempt(c, "6.6.6.6")
    assert seen_ips(c) == {"testclient"}  # the socket peer, whatever the client claims


def test_spoofed_leftmost_forwarded_for_entry_does_not_choose_your_bucket(make):
    c = make(trust_proxy_headers=True, trusted_proxy_hops=1)
    attempt(c, "6.6.6.6, 203.0.113.9")  # the attacker prepended 6.6.6.6; our proxy appended the real peer
    attempt(c, "7.7.7.7, 203.0.113.9")
    assert seen_ips(c) == {"203.0.113.9"}


def test_hop_count_picks_the_right_entry_and_junk_falls_back(make):
    c = make(trust_proxy_headers=True, trusted_proxy_hops=2)
    attempt(c, "198.51.100.1, 203.0.113.9, 10.0.0.2")
    assert seen_ips(c) == {"203.0.113.9"}
    for bad in ("key:abc123, key:abc123", "not-an-ip", "", "1.2.3.4"):  # too few hops, or not an address
        attempt(c, bad)
    assert seen_ips(c) == {"203.0.113.9", "testclient"}


def test_forwarded_for_cannot_name_another_bucket(make):
    c = make(trust_proxy_headers=True)
    attempt(c, "key:abcdef123456")
    assert not [k for k in redis_keys(c, "hotload:rl:*") if "key:abcdef123456" in k]


# --- login throttling layers ------------------------------------------------------------------------------------

def test_one_address_cannot_stuff_credentials_across_many_accounts(make):
    c = make()
    codes = [c.post("/api/v1/auth/login", json={"email": f"user{i}@example.com", "password": "wrong password"}).status_code
             for i in range(45)]
    assert codes[:40] == [401] * 40 and codes[40:] == [429] * 5


def test_one_account_cannot_be_guessed_from_many_addresses(make):
    c = make(trust_proxy_headers=True)
    codes = [attempt(c, f"198.51.100.{i % 250}, 203.0.113.{i // 250 + 1}").status_code for i in range(0)]
    codes = [c.post("/api/v1/auth/login", json={"email": "target@example.com", "password": "wrong password"},
                    headers={"x-forwarded-for": f"x, 192.0.2.{i % 250}"}).status_code for i in range(65)]
    assert codes[:60] == [401] * 60 and 429 in codes[60:]


def test_deleting_an_account_is_not_a_password_oracle(make):
    c = make()
    signup(c)
    codes = [c.request("DELETE", "/api/v1/me", json={"password": f"wrong password {i}"}).status_code for i in range(12)]
    assert codes[:10] == [403] * 10 and codes[10:] == [429, 429]
    assert c.get("/api/v1/me").status_code == 200  # the account survived


def test_login_normalises_email_like_registration(make):
    c = make()
    signup(c, "  Ada.Lovelace@Example.COM ")
    c.post("/api/v1/auth/logout")
    assert c.post("/api/v1/auth/login", json={"email": "ADA.LOVELACE@example.com", "password": PW}).status_code == 200


# --- cookie flags, secrets, metrics -----------------------------------------------------------------------------

def session_cookie_header(c):
    return c.post("/api/v1/auth/register", json={"email": "f@example.com", "password": PW}).headers["set-cookie"].lower()


@pytest.mark.parametrize("cfg,secure", [
    ({"public_url": "https://hotload.example"}, True),     # derived from https: nobody has to remember a flag
    ({"public_url": "http://localhost:8000"}, False),
    ({}, False),
    ({"public_url": "https://hotload.example", "cookie_secure": False}, False),  # explicit override still wins
    ({"cookie_secure": True}, True),
])
def test_session_cookie_secure_flag(make, cfg, secure):
    assert ("secure" in session_cookie_header(make(**cfg)).replace("samesite", "")) is secure


def test_hosted_mode_refuses_to_start_without_a_secret_key(tmp_path):
    with pytest.raises(RuntimeError, match="HOTLOAD_SECRET_KEY"):
        create_app(Settings(data_dir=tmp_path, quotas_enabled=True), redis=FakeAsyncRedis(decode_responses=True))
    create_app(Settings(data_dir=tmp_path, quotas_enabled=True, secret_key="x"), redis=FakeAsyncRedis(decode_responses=True))
    create_app(Settings(data_dir=tmp_path), redis=FakeAsyncRedis(decode_responses=True))  # self-host default stays easy


def test_metrics_are_closed_in_hosted_mode_until_a_token_is_set(make):
    assert make().get("/metrics").status_code == 200  # self-host: open by default
    hosted = make(quotas_enabled=True)
    assert hosted.get("/metrics").status_code == 404
    tok = make(quotas_enabled=True, metrics_token="s3cret")
    assert tok.get("/metrics").status_code == 401
    assert tok.get("/metrics", headers={"authorization": "Bearer wrong"}).status_code == 401
    assert tok.get("/metrics", headers={"authorization": "Bearer s3cret"}).status_code == 200


# --- job visibility ------------------------------------------------------------------------------------------------

def test_signed_in_users_jobs_are_visible_only_to_their_owner(make):
    c = make()
    uid = signup(c, "owner@example.com")
    key = c.post("/api/v1/me/keys", json={"name": "k"}).json()["key"]
    job = c.post("/api/v1/jobs", json={"url": "https://example.com/mine"}).json()["id"]
    batch = c.post("/api/v1/batches", json={"urls": ["https://example.com/b1"]}).json()["id"]
    assert c.get(f"/api/v1/jobs/{job}").status_code == 200 and c.get(f"/api/v1/batches/{batch}").status_code == 200
    assert c.get(f"/api/v1/jobs/{job}", headers={"authorization": f"Bearer {key}"}).status_code in (200, 401)  # key path below

    c.post("/api/v1/auth/logout")
    for path in (f"/api/v1/jobs/{job}", f"/api/v1/jobs/{job}/events", f"/api/v1/batches/{batch}"):
        assert c.get(path).status_code == 404, path  # an id learned from a log or URL reveals nothing (and no token)
    assert c.get("/api/v1/jobs/nope").status_code == 404  # indistinguishable from a missing job

    signup(c, "other@example.com")
    assert c.get(f"/api/v1/jobs/{job}").status_code == 404 and c.get(f"/api/v1/batches/{batch}").status_code == 404
    c.post("/api/v1/auth/logout")
    owner = c.post("/api/v1/auth/login", json={"email": "owner@example.com", "password": PW})
    assert owner.status_code == 200 and c.get(f"/api/v1/jobs/{job}").status_code == 200 and uid
    c.post("/api/v1/auth/logout")
    assert c.get(f"/api/v1/jobs/{job}", headers={"authorization": f"Bearer {key}"}).status_code == 200  # the owner's key works


def test_anonymous_jobs_and_token_gated_downloads_are_unchanged(make):
    c = make()
    anon = c.post("/api/v1/jobs", json={"url": "https://example.com/a"}).json()["id"]
    assert c.get(f"/api/v1/jobs/{anon}").status_code == 200  # capability-style, as before
    signup(c)
    mine = c.post("/api/v1/jobs", json={"url": "https://example.com/b"}).json()["id"]
    token = c.portal.call(c.app.state.store.get, mine).token
    c.post("/api/v1/auth/logout")
    # the token itself is still a capability (webhook consumers and bot replies use it without a session)
    assert c.get(f"/api/v1/jobs/{mine}/file?token={token}").status_code == 404  # not finished yet, but accepted
    assert c.get(f"/api/v1/jobs/{mine}/file?token=wrong").status_code == 403


def test_non_ascii_tokens_are_rejected_not_crashed(make):
    c = make()
    job = c.post("/api/v1/jobs", json={"url": "https://example.com/a"}).json()["id"]
    assert c.get(f"/api/v1/jobs/{job}/file", params={"token": "é中"}).status_code == 403
    b = c.post("/api/v1/batches", json={"urls": ["https://example.com/a"]}).json()["id"]
    assert c.get(f"/api/v1/batches/{b}/zip", params={"token": "é"}).status_code == 403


def test_opening_event_streams_is_rate_limited(make):
    c = make()
    job = c.post("/api/v1/jobs", json={"url": "https://example.com/a"}).json()["id"]

    async def finish():
        j = await c.app.state.store.get(job)
        j.state = "done"
        await c.app.state.store.save(j)

    c.portal.call(finish)  # a finished job ends the stream immediately
    codes = [c.get(f"/api/v1/jobs/{job}/events").status_code for _ in range(33)]
    assert codes[:30] == [200] * 30 and codes[30:] == [429] * 3


def test_account_deletion_removes_api_keys(make):
    c = make()
    signup(c)
    key_id = c.post("/api/v1/me/keys", json={"name": "k"}).json()["id"]
    assert c.request("DELETE", "/api/v1/me", json={"password": PW}).status_code == 204
    assert c.portal.call(c.app.state.db.get_key, key_id) is None


def test_comparisons_never_raise_on_non_ascii_input():
    """compare_digest on str raises TypeError for non-ASCII, which used to turn a bad header into an HTTP 500."""
    from hotload import auth
    from hotload.billing.btcpay import BTCPay
    from hotload.billing.paystack import Paystack

    odd = "\u00e9\u4e2d"
    assert auth.safe_equal(odd, "x") is False and auth.safe_equal(odd, odd) is True and auth.safe_equal(None, "") is True
    assert Paystack(Settings(paystack_secret_key="k"), None).check_signature({"x-paystack-signature": odd}, b"{}") is False
    cfg = Settings(btcpay_url="https://b.example", btcpay_store_id="s", btcpay_api_key="k", btcpay_webhook_secret="w")
    assert BTCPay(cfg, None).check_signature({"btcpay-sig": odd}, b"{}") is False


def test_http_client_import_is_available():
    assert httpx.__version__
