import os
import stat
import time

import pytest
from cryptography.fernet import Fernet
from fakeredis import FakeAsyncRedis
from fastapi.testclient import TestClient

from hotload import cookies, engine, worker
from hotload.api.app import create_app
from hotload.config import Settings
from hotload.cookies import CookieError, CookieVault
from hotload.db import Database
from hotload.jobs import JobStore
from hotload.proxies import ProxyPool

KEY = Fernet.generate_key().decode()
PW = "correct horse battery"
SECRET_VALUE = "SUPERSECRETSESSIONTOKEN123"


def jar(*rows):
    return "# Netscape HTTP Cookie File\n" + "\n".join("\t".join(r) for r in rows) + "\n"


def row(domain=".example.com", name="sid", value=SECRET_VALUE, path="/", secure="TRUE", expiry="2000000000", flag="TRUE"):
    return (domain, flag, path, secure, expiry, name, value)


# --- parsing ------------------------------------------------------------------------------------

def test_only_cookies_for_the_stated_site_are_kept():
    text = jar(row(".example.com", "keep1"), row("example.com", "keep2"), row("www.example.com", "dropsub"),
               row(".other.org", "dropother"), row(".com", "dropbroad"), row(".evilexample.com", "droplookalike"))
    p = cookies.parse_netscape(text, "example.com")
    names = [ln.split("\t")[5] for ln in p.text.splitlines() if ln and not ln.startswith("#")]
    assert names == ["keep1", "keep2"] and p.kept == 2 and p.dropped == 4
    sub = cookies.parse_netscape(text, "www.example.com")  # a subdomain also receives cookies scoped to its parent
    assert {ln.split("\t")[5] for ln in sub.text.splitlines() if ln and not ln.startswith("#")} == {"keep1", "keep2", "dropsub"}


@pytest.mark.parametrize("broad", [".com", "com", ".co.uk", ".org", ".gov.uk"])
def test_cookies_scoped_to_a_whole_suffix_are_never_kept(broad):
    """Such a cookie would be sent to every unrelated site under that suffix."""
    domain = "example." + broad.lstrip(".")
    if broad == ".gov.uk":
        domain = "agency.gov.uk"
    with pytest.raises(CookieError):
        cookies.parse_netscape(jar(row(broad, "x")), domain)
    assert cookies.parse_netscape(jar(row(broad, "x"), row("." + domain, "ok")), domain).kept == 1


def test_httponly_lines_and_comments_and_junk():
    text = "# Netscape HTTP Cookie File\n# comment\n\n#HttpOnly_" + "\t".join(row(name="h")) + "\nnot a cookie\n" + "\t".join(row(name="ok")) + "\n"
    p = cookies.parse_netscape(text, "example.com")
    assert p.kept == 2 and p.dropped == 1 and "#HttpOnly_" in p.text


@pytest.mark.parametrize("bad", [
    row(secure="MAYBE"), row(path="nope"), row(expiry="abc"), row(name=""), row(name="a" * 201), row(value="x" * 4001),
    row(domain="")])
def test_malformed_cookies_are_dropped(bad):
    with pytest.raises(CookieError):
        cookies.parse_netscape(jar(bad), "example.com")


def test_limits():
    with pytest.raises(CookieError, match="too large"):
        cookies.parse_netscape("x" * (cookies.MAX_UPLOAD_BYTES + 1), "example.com")
    many = jar(*[row(name=f"c{i}") for i in range(cookies.MAX_COOKIES + 1)])
    with pytest.raises(CookieError, match="At most"):
        cookies.parse_netscape(many, "example.com")
    with pytest.raises(CookieError, match="No usable"):
        cookies.parse_netscape("# nothing here\n", "example.com")


@pytest.mark.parametrize("ok,expected", [("Example.COM", "example.com"), ("https://www.example.com/path?x", "www.example.com"), (".example.co.uk", "example.co.uk")])
def test_domain_cleaning(ok, expected):
    assert cookies.clean_domain(ok) == expected


@pytest.mark.parametrize("bad", ["", "localhost", "com", "1.2.3.4", "exa mple.com", "-bad.com", "a" * 300 + ".com", "http://"])
def test_bad_domains_rejected(bad):
    with pytest.raises(CookieError):
        cookies.clean_domain(bad)


# --- vault ---------------------------------------------------------------------------------------

@pytest.fixture
async def vault(tmp_path):
    db = Database(Settings(data_dir=tmp_path))
    await db.migrate()
    user = await db.add_user(email="v@example.com", password_hash="x")
    yield CookieVault(Settings(data_dir=tmp_path, cookie_key=KEY), db), db, user
    await db.dispose()


async def test_stored_encrypted_and_decrypted_into_a_private_file(vault, tmp_path):
    v, db, user = vault
    await v.save(user.id, "example.com", jar(row()))
    [stored] = await db.list_cookies(user.id)
    assert SECRET_VALUE not in stored.blob and "sid" not in stored.blob  # ciphertext only in the database
    async with v.file_for(user.id, "https://media.example.com/watch?v=1") as path:
        assert path is not None and SECRET_VALUE in path.read_text()
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600 and stat.S_IMODE(os.stat(path.parent).st_mode) == 0o700
        kept = path
    assert not kept.exists() and not kept.parent.exists()  # removed as soon as the download ends


async def test_file_is_removed_even_if_the_download_fails(vault):
    v, db, user = vault
    await v.save(user.id, "example.com", jar(row()))
    seen = {}
    with pytest.raises(RuntimeError):
        async with v.file_for(user.id, "https://example.com/v") as path:
            seen["p"] = path
            raise RuntimeError("download blew up")
    assert not seen["p"].exists()


async def test_no_cookies_for_other_sites_or_other_users(vault):
    v, db, user = vault
    other = await db.add_user(email="o@example.com", password_hash="x")
    await v.save(user.id, "example.com", jar(row()))
    async with v.file_for(user.id, "https://example.org/v") as p:
        assert p is None  # different site
    async with v.file_for(other.id, "https://example.com/v") as p:
        assert p is None  # someone else's cookies are never used
    async with v.file_for(user.id, "https://example.com.evil.org/v") as p:
        assert p is None  # lookalike host


async def test_longest_domain_wins_and_save_replaces(vault):
    v, db, user = vault
    await v.save(user.id, "example.com", jar(row(name="broad")))
    await v.save(user.id, "media.example.com", jar(row(".media.example.com", "narrow")))
    assert (await v.match(user.id, "https://media.example.com/x")).domain == "media.example.com"
    await v.save(user.id, "example.com", jar(row(name="replaced")))
    assert len(await db.list_cookies(user.id)) == 2


async def test_domain_limit(vault):
    v, db, user = vault
    for i in range(cookies.MAX_DOMAINS):
        await v.save(user.id, f"site{i}.com", jar(row(f".site{i}.com")))
    with pytest.raises(CookieError, match="At most"):
        await v.save(user.id, "one-too-many.com", jar(row(".one-too-many.com")))
    await v.save(user.id, "site0.com", jar(row(".site0.com", "again")))  # updating an existing site is fine


async def test_key_rotation_and_wrong_key(vault, tmp_path):
    v, db, user = vault
    await v.save(user.id, "example.com", jar(row()))
    new = Fernet.generate_key().decode()
    rotated = CookieVault(Settings(data_dir=tmp_path, cookie_key=f"{new},{KEY}"), db)  # new key first, old still decrypts
    async with rotated.file_for(user.id, "https://example.com/v") as p:
        assert SECRET_VALUE in p.read_text()
    wrong = CookieVault(Settings(data_dir=tmp_path, cookie_key=Fernet.generate_key().decode()), db)
    with pytest.raises(CookieError, match="key changed"):
        async with wrong.file_for(user.id, "https://example.com/v"):
            pass
    assert not CookieVault(Settings(data_dir=tmp_path), db).enabled


# --- API ----------------------------------------------------------------------------------------

@pytest.fixture
def make(tmp_path, monkeypatch):
    monkeypatch.setattr("socket.getaddrinfo", lambda host, port, *a, **k: [(2, 1, 6, "", ("93.184.216.34", port))])
    stack, queue = [], []

    def build(**o):
        async def enqueue(job_id, _q=None):
            queue.append(job_id)

        cfg = {"data_dir": tmp_path, "rate_limit_per_minute": 1000, "secret_key": "s", "cookie_key": KEY, **o}
        c = TestClient(create_app(Settings(**cfg), redis=FakeAsyncRedis(decode_responses=True), enqueue=enqueue))
        c.__enter__()
        c.queue = queue
        stack.append(c)
        return c

    yield build
    for c in stack:
        c.__exit__(None, None, None)


def signup(c, email="u@example.com"):
    r = c.post("/api/v1/auth/register", json={"email": email, "password": PW})
    assert r.status_code == 201
    return r.json()["id"]


def put(c, domain="example.com", text=None):
    return c.put("/api/v1/me/cookies", json={"domain": domain, "cookies_txt": text or jar(row())})


def test_upload_list_delete_never_reveals_values(make):
    c = make()
    assert put(c).status_code == 401
    signup(c)
    r = put(c, text=jar(row(), row(".other.org", "x")))
    assert r.status_code == 200 and r.json() == {"domain": "example.com", "cookies": 1, "ignored": 1}
    listed = c.get("/api/v1/me/cookies")
    assert listed.json()[0]["domain"] == "example.com" and listed.json()[0]["cookies"] == 1
    assert SECRET_VALUE not in listed.text and SECRET_VALUE not in r.text and "blob" not in listed.text
    assert c.delete("/api/v1/me/cookies/example.com").status_code == 204
    assert c.delete("/api/v1/me/cookies/example.com").status_code == 404
    assert c.get("/api/v1/me/cookies").json() == []


def test_validation_errors_are_readable(make):
    c = make()
    signup(c)
    assert put(c, "localhost").status_code == 422
    assert put(c, text="# empty").status_code == 422
    assert c.put("/api/v1/me/cookies", json={"domain": "example.com", "cookies_txt": "x" * 200000}).status_code == 422


def test_disabled_without_a_key_and_session_only(make):
    off = make(cookie_key=None)
    signup(off, "a@example.com")
    assert put(off).status_code == 503
    c = make()
    signup(c, "b@example.com")
    key = c.post("/api/v1/me/keys", json={"name": "k"}).json()["key"]
    h = {"authorization": f"Bearer {key}"}
    assert c.put("/api/v1/me/cookies", json={"domain": "example.com", "cookies_txt": jar(row())}, headers=h).status_code == 403
    assert c.get("/api/v1/me/cookies", headers=h).status_code == 403  # a leaked key cannot read or plant cookies


def test_free_plan_blocked_only_when_plans_are_enforced(make):
    c = make(quotas_enabled=True)
    uid = signup(c)
    assert put(c).status_code == 403
    c.portal.call(c.app.state.db.set_tier, uid, "premium", time.time() + 86400)
    assert put(c).status_code == 200


def test_cookies_are_deleted_with_the_account(make):
    c = make()
    signup(c)
    put(c)
    assert c.request("DELETE", "/api/v1/me", json={"password": PW}).status_code == 204
    assert c.portal.call(c.app.state.db.list_cookies, "anything") == []
    signup(c)  # same email re-registers as a new user with nothing saved
    assert c.get("/api/v1/me/cookies").json() == []


def test_opting_in_requires_login_cookies_for_that_site_and_a_plan(make, monkeypatch):
    c = make()
    body = {"url": "https://media.example.com/v", "use_cookies": True}
    assert c.post("/api/v1/jobs", json=body).status_code == 401
    signup(c)
    assert c.post("/api/v1/jobs", json=body).status_code == 400  # none saved yet
    put(c)
    ok = c.post("/api/v1/jobs", json=body)
    assert ok.status_code == 202
    assert c.post("/api/v1/jobs", json={**body, "url": "https://example.org/v"}).status_code == 400  # other site
    assert "use_cookies" not in str(ok.json().get("download_url"))
    b = c.post("/api/v1/batches", json={"urls": ["https://example.com/a", "https://example.org/b"], "use_cookies": True})
    assert b.status_code == 400 and c.queue.count(ok.json()["id"]) == 1  # all-or-nothing: nothing was queued for it
    seen = {}

    def fake_extract(url, s, proxy=None, cookiefile=None):
        seen["file"] = cookiefile
        seen["text"] = cookiefile.read_text() if cookiefile else None
        return {"title": "t", "site": "X", "id": "1", "age_limit": 0}

    monkeypatch.setattr(engine, "extract", fake_extract)
    assert c.post("/api/v1/extract", json={"url": "https://media.example.com/v", "use_cookies": True}).status_code == 200
    assert SECRET_VALUE in seen["text"] and not seen["file"].exists()  # used for extraction, gone afterwards
    c.post("/api/v1/extract", json={"url": "https://media.example.com/v"})
    assert seen["file"] is None  # not opted in: cookies are never attached implicitly


def test_cookie_use_is_not_exposed_in_job_json_or_history_urls(make):
    c = make()
    signup(c)
    put(c)
    j = c.post("/api/v1/jobs", json={"url": "https://example.com/v", "use_cookies": True}).json()
    assert SECRET_VALUE not in str(j) and "cookie" not in {k for k in j if k != "use_cookies"}


# --- worker ---------------------------------------------------------------------------------------

async def _worker_setup(tmp_path, monkeypatch, *, with_key=True):
    settings = Settings(data_dir=tmp_path, cookie_key=KEY if with_key else None)
    monkeypatch.setattr(worker, "get_settings", lambda: settings)
    monkeypatch.setattr(worker, "_pool", ProxyPool())
    db = Database(settings)
    await db.migrate()
    user = await db.add_user(email="w@example.com", password_hash="x")
    vault = CookieVault(settings, db)
    store = JobStore(FakeAsyncRedis(decode_responses=True), 600)
    return settings, db, user, vault, store


async def test_worker_gives_yt_dlp_the_cookie_file_then_removes_it(tmp_path, monkeypatch):
    settings, db, user, vault, store = await _worker_setup(tmp_path, monkeypatch)
    await vault.save(user.id, "example.com", jar(row()))
    seen = {}

    def fake_download(url, s, workdir, preset, proxy=None, cookiefile=None, **kw):
        seen["file"], seen["text"] = cookiefile, cookiefile.read_text()
        assert not str(cookiefile).startswith(str(settings.output_dir))  # never inside the served/zipped output tree
        workdir.mkdir(parents=True, exist_ok=True)
        (workdir / "v.mp3").write_bytes(b"x")
        return engine.DownloadResult(workdir / "v.mp3", "T", "G")

    monkeypatch.setattr(engine, "download", fake_download)
    job = await store.create(url="https://media.example.com/v", preset="mp3", user_id=user.id, use_cookies=True)
    await worker.run_download({"store": store, "db": db, "vault": vault}, job.id)
    done = await store.get(job.id)
    assert done.state == "done" and SECRET_VALUE in seen["text"] and not seen["file"].exists()
    assert SECRET_VALUE not in str(done.public())
    await db.dispose()


async def test_worker_never_attaches_cookies_to_jobs_that_did_not_opt_in(tmp_path, monkeypatch):
    settings, db, user, vault, store = await _worker_setup(tmp_path, monkeypatch)
    await vault.save(user.id, "example.com", jar(row()))
    got = []

    def fake_download(url, s, workdir, preset, proxy=None, cookiefile=None, **kw):
        got.append(cookiefile)
        workdir.mkdir(parents=True, exist_ok=True)
        (workdir / "v.mp3").write_bytes(b"x")
        return engine.DownloadResult(workdir / "v.mp3", "T", "G")

    monkeypatch.setattr(engine, "download", fake_download)
    job = await store.create(url="https://example.com/v", preset="mp3", user_id=user.id)  # use_cookies defaults to False
    await worker.run_download({"store": store, "db": db, "vault": vault}, job.id)
    assert got == [None]
    await db.dispose()


async def test_worker_fails_cleanly_when_cookies_vanished_or_feature_is_off(tmp_path, monkeypatch):
    settings, db, user, vault, store = await _worker_setup(tmp_path, monkeypatch)
    monkeypatch.setattr(engine, "download", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not download")))
    gone = await store.create(url="https://example.com/v", preset="mp3", user_id=user.id, use_cookies=True)
    await worker.run_download({"store": store, "db": db, "vault": vault}, gone.id)
    assert "removed" in (await store.get(gone.id)).message and (await store.get(gone.id)).state == "error"
    off = CookieVault(Settings(data_dir=tmp_path), db)
    j2 = await store.create(url="https://example.com/v", preset="mp3", user_id=user.id, use_cookies=True)
    await worker.run_download({"store": store, "db": db, "vault": off}, j2.id)
    assert "not available" in (await store.get(j2.id)).message
    await db.dispose()


def test_cookiefile_reaches_yt_dlp_options(tmp_path):
    p = tmp_path / "c.txt"
    opts = engine.build_download_opts(Settings(), tmp_path, engine.PRESETS["mp3"], cookiefile=p)
    assert opts["cookiefile"] == str(p)
    assert "cookiefile" not in engine.build_download_opts(Settings(), tmp_path, engine.PRESETS["mp3"])
