"""Email verification and password reset. A fake mailer records messages; the links are read back out of them."""

import re
import time

import pytest
from fakeredis import FakeAsyncRedis
from fastapi.testclient import TestClient

from hotload.api.app import create_app
from hotload.config import Settings

PW = "correct horse battery staple"


async def _noop(job_id, _queue=None):
    pass


class FakeMailer:
    def __init__(self):
        self.sent: list[tuple[str, str, str]] = []

    async def send(self, to, subject, body):
        self.sent.append((to, subject, body))

    def token(self, index=-1) -> str:
        return re.search(r"#token=(\S+)", self.sent[index][2]).group(1)


@pytest.fixture
def mailer():
    return FakeMailer()


@pytest.fixture
def mail_settings(tmp_path):
    return Settings(data_dir=tmp_path, public_url="https://hotload.test", smtp_host="smtp.test", mail_from="Hotload <no-reply@hotload.test>",
                    secure_cookies=False, cookie_secure=False)


@pytest.fixture
def mclient(mail_settings, mailer, monkeypatch):
    monkeypatch.setattr("socket.getaddrinfo", lambda host, port, *a, **k: [(2, 1, 6, "", ("93.184.216.34", port))])
    app = create_app(mail_settings, redis=FakeAsyncRedis(decode_responses=True), enqueue=_noop, mailer=mailer)
    with TestClient(app, base_url="http://testserver") as c:
        c.app = app
        yield c


def register(c, email="a@example.com"):
    r = c.post("/api/v1/auth/register", json={"email": email, "password": PW})
    assert r.status_code == 201, r.text
    return r.json()


def test_registration_sends_a_verification_link_and_it_verifies(mclient, mailer):
    assert register(mclient)["email_verified"] is False
    assert len(mailer.sent) == 1 and mailer.sent[0][0] == "a@example.com"
    body = mailer.sent[0][2]
    assert body.count("https://hotload.test/account/verify/#token=") == 1  # fragment, and built from public_url
    assert mclient.post("/api/v1/auth/verify", json={"token": mailer.token()}).json() == {"verified": True}
    assert mclient.get("/api/v1/me").json()["email_verified"] is True


def test_links_use_public_url_never_the_host_header(mclient, mailer):
    mclient.post("/api/v1/auth/register", json={"email": "h@example.com", "password": PW}, headers={"host": "evil.example"})
    assert "evil.example" not in mailer.sent[0][2]


def test_verify_token_is_not_a_reset_token_and_garbage_is_rejected(mclient, mailer):
    register(mclient)
    token = mailer.token()
    assert mclient.post("/api/v1/auth/password/reset", json={"token": token, "password": "another long password"}).status_code == 400
    for bad in ("", "x", token + "x", "a" * 2000):
        assert mclient.post("/api/v1/auth/verify", json={"token": bad}).status_code in (400, 422)


def test_expired_verify_token_is_rejected(mclient, mailer, monkeypatch):
    register(mclient)
    token = mailer.token()
    real = time.time
    monkeypatch.setattr("time.time", lambda: real() + 4 * 24 * 3600)
    assert mclient.post("/api/v1/auth/verify", json={"token": token}).status_code == 400


def test_resend_is_rate_limited_and_noop_when_verified(mclient, mailer):
    register(mclient)
    for _ in range(5):  # the limit is 5 an hour
        assert mclient.post("/api/v1/auth/verify/send").status_code == 202
    assert mclient.post("/api/v1/auth/verify/send").status_code == 429
    mclient.post("/api/v1/auth/verify", json={"token": mailer.token()})
    before = len(mailer.sent)
    assert mclient.post("/api/v1/auth/verify/send").json()["verified"] is True and len(mailer.sent) == before


def test_forgot_answers_identically_for_known_and_unknown_addresses(mclient, mailer):
    register(mclient, "known@example.com")
    mailer.sent.clear()
    a = mclient.post("/api/v1/auth/password/forgot", json={"email": "known@example.com"})
    b = mclient.post("/api/v1/auth/password/forgot", json={"email": "nobody@example.com"})
    c = mclient.post("/api/v1/auth/password/forgot", json={"email": "not an email"})
    assert (a.status_code, a.json()) == (b.status_code, b.json()) == (c.status_code, c.json()) == (202, {"ok": True})
    assert [m[0] for m in mailer.sent] == ["known@example.com"]  # only the real account got mail


def test_forgot_is_rate_limited_per_address_without_changing_the_answer(mclient, mailer):
    register(mclient, "r@example.com")
    mailer.sent.clear()
    for _ in range(6):
        assert mclient.post("/api/v1/auth/password/forgot", json={"email": "r@example.com"}).status_code == 202
    assert len(mailer.sent) == 3


def test_reset_changes_password_ends_old_sessions_and_link_is_single_use(mclient, mailer):
    register(mclient, "p@example.com")
    stolen = mclient.cookies.get("hl_session")
    mclient.post("/api/v1/auth/password/forgot", json={"email": "p@example.com"})
    token = mailer.token()
    mclient.cookies.clear()
    r = mclient.post("/api/v1/auth/password/reset", json={"token": token, "password": "a brand new password"})
    assert r.status_code == 200
    assert mclient.get("/api/v1/me").json()["email_verified"] is True  # the reset signed this browser in, and proved the mailbox
    mclient.cookies.clear()
    mclient.cookies.set("hl_session", stolen)
    assert mclient.get("/api/v1/me").status_code == 401  # sessions from before the reset are dead
    mclient.cookies.clear()
    assert mclient.post("/api/v1/auth/login", json={"email": "p@example.com", "password": PW}).status_code == 401
    assert mclient.post("/api/v1/auth/login", json={"email": "p@example.com", "password": "a brand new password"}).status_code == 200
    again = mclient.post("/api/v1/auth/password/reset", json={"token": token, "password": "yet another password!"})
    assert again.status_code == 400  # same link, second use


def test_reset_rejects_short_passwords_without_spending_the_link(mclient, mailer):
    register(mclient, "s@example.com")
    mclient.post("/api/v1/auth/password/forgot", json={"email": "s@example.com"})
    token = mailer.token()
    assert mclient.post("/api/v1/auth/password/reset", json={"token": token, "password": "short"}).status_code == 422
    assert mclient.post("/api/v1/auth/password/reset", json={"token": token, "password": "long enough now"}).status_code == 200


def test_without_a_mail_provider_the_endpoints_say_so(client):
    assert client.get("/api/v1/auth/providers").json()["email"] is False
    assert client.post("/api/v1/auth/password/forgot", json={"email": "a@example.com"}).status_code == 503
    client.post("/api/v1/auth/register", json={"email": "a@example.com", "password": PW})
    assert client.post("/api/v1/auth/verify/send").status_code == 503


def test_unverified_accounts_cannot_buy_or_mint_keys_when_required(tmp_path, mailer, monkeypatch):
    s = Settings(data_dir=tmp_path, public_url="https://hotload.test", smtp_host="x", mail_from="a@b.co",
                 require_verified_email=True, cookie_secure=False)
    app = create_app(s, redis=FakeAsyncRedis(decode_responses=True), enqueue=_noop, mailer=mailer)
    with TestClient(app) as c:
        register(c)
        body = {"plan": "pro_30", "provider": "paystack", "currency": "USD"}
        assert c.post("/api/v1/billing/checkout", json=body).status_code == 403
        assert c.post("/api/v1/me/keys", json={"name": "k"}).status_code == 403
        c.post("/api/v1/auth/verify", json={"token": mailer.token()})
        assert c.post("/api/v1/billing/checkout", json=body).status_code != 403


def test_verified_password_account_is_linked_not_taken_over_by_oauth(tmp_path):
    """A mailbox-verified password account keeps its password when the same address signs in with a provider."""
    import asyncio

    from hotload.db import Database

    async def go():
        db = Database(Settings(data_dir=tmp_path))
        await db.migrate()
        u = await db.add_user(email="v@example.com", password_hash="h", email_verified=True)
        await db.link_oauth(u.id, "github", "42")
        got = await db.user_by_id(u.id)
        assert got.password_hash == "h" and got.oauth_sub == "42" and got.email_verified
        await db.dispose()

    asyncio.run(go())


class _FakeSMTP:
    instances: list = []

    def __init__(self, host, port, timeout=None, context=None):
        self.sent, self.logged_in, self.tls = [], None, False
        _FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self, context=None):
        self.tls = True

    def login(self, user, password):
        self.logged_in = (user, password)

    def send_message(self, msg):
        self.sent.append(msg)


async def test_smtp_mailer_builds_a_plain_message_with_starttls_and_login(mail_settings, monkeypatch):
    from hotload import mail

    _FakeSMTP.instances.clear()
    monkeypatch.setattr("smtplib.SMTP", _FakeSMTP)
    s = mail_settings.model_copy(update={"smtp_user": "u", "smtp_password": "p"})
    await mail.SmtpMailer(s).send("to@example.com", "Hi", "Body")
    smtp = _FakeSMTP.instances[0]
    assert smtp.tls and smtp.logged_in == ("u", "p")
    msg = smtp.sent[0]
    assert msg["To"] == "to@example.com" and msg["From"] == "Hotload <no-reply@hotload.test>" and msg["Subject"] == "Hi"


async def test_smtp_mailer_refuses_header_injection_and_never_logs_the_message(mail_settings, monkeypatch, logs):
    from hotload import mail

    _FakeSMTP.instances.clear()
    monkeypatch.setattr("smtplib.SMTP", _FakeSMTP)
    await mail.SmtpMailer(mail_settings).send("a@example.com\r\nBcc: x@example.com", "Hi", "secret-token-body")
    assert not _FakeSMTP.instances or not _FakeSMTP.instances[0].sent  # nothing went out
    assert "mail send failed" in logs.text  # positive control: the failure was logged...
    assert "secret-token-body" not in logs.text and "example.com" not in logs.text  # ...without the text or recipient


def test_mail_is_enabled_only_with_host_sender_and_public_url(tmp_path):
    base = dict(data_dir=tmp_path)
    assert not Settings(**base).mail_enabled
    assert not Settings(**base, smtp_host="h", mail_from="a@b.co").mail_enabled  # no public_url: links could not be built safely
    assert Settings(**base, smtp_host="h", mail_from="a@b.co", public_url="https://x.test").mail_enabled


async def test_smtp_security_modes(mail_settings, monkeypatch):
    from hotload import mail

    calls = []

    class Plain(_FakeSMTP):
        def starttls(self, context=None):
            calls.append("starttls")

    class Implicit(_FakeSMTP):
        pass

    monkeypatch.setattr("smtplib.SMTP", Plain)
    monkeypatch.setattr("smtplib.SMTP_SSL", Implicit)
    _FakeSMTP.instances.clear()
    for mode, cls in (("ssl", Implicit), ("none", Plain), ("starttls", Plain)):
        _FakeSMTP.instances.clear()
        calls.clear()
        await mail.SmtpMailer(mail_settings.model_copy(update={"smtp_security": mode})).send("a@example.com", "s", "b")
        assert isinstance(_FakeSMTP.instances[0], cls) and _FakeSMTP.instances[0].sent, mode
        assert (calls == ["starttls"]) == (mode == "starttls"), mode
