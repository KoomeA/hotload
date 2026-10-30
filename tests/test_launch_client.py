"""Regression tests for the client-facing findings: thumbnails, token-bearing URLs in logs."""

import io
import logging
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fakeredis import FakeAsyncRedis
from fastapi.testclient import TestClient

from hotload import engine, logging_setup, thumbs
from hotload.api.app import create_app
from hotload.config import Settings

PUBLIC = "93.184.216.34"
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64


class Remote:
    """What the 'image host' answers, and a record of every request it received."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.status, self.ctype, self.body, self.location = 200, "image/png", PNG, None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        headers = {"content-type": self.ctype}
        if self.location:
            headers["location"] = self.location
        return httpx.Response(self.status, content=self.body, headers=headers)


@pytest.fixture
def remote():
    return Remote()


@pytest.fixture
def api(tmp_path, monkeypatch, remote):
    monkeypatch.setattr("socket.getaddrinfo", lambda host, port, *a, **k: [(2, 1, 6, "", (PUBLIC, port))])

    async def enqueue(job_id, _q=None):
        pass

    http = httpx.AsyncClient(transport=httpx.MockTransport(remote))
    c = TestClient(create_app(Settings(data_dir=tmp_path, rate_limit_per_minute=10000, secret_key="thumb-secret"),
                              redis=FakeAsyncRedis(decode_responses=True), enqueue=enqueue, http_client=http))
    c.__enter__()
    yield c
    c.__exit__(None, None, None)


def extract_thumb(api, monkeypatch, remote_url="https://img.example/t.png"):
    monkeypatch.setattr(engine, "extract", lambda url, s, proxy=None, **k: {"title": "t", "site": "X", "id": "1", "age_limit": 0,
                                                                              "thumbnail": remote_url})
    return api.post("/api/v1/extract", json={"url": "https://example.com/v"}).json()["thumbnail"]


# --- thumbnails -------------------------------------------------------------------------------------------------------

def test_the_page_never_receives_the_remote_thumbnail_address(api, monkeypatch):
    thumb = extract_thumb(api, monkeypatch, "https://attacker.example/beacon.gif?victim=1")
    assert thumb.startswith("/api/v1/thumb?u=") and "attacker.example" not in thumb  # same-origin; remote host not exposed
    assert extract_thumb(api, monkeypatch, "javascript:alert(1)") is None  # not a web address: no thumbnail at all
    assert extract_thumb(api, monkeypatch, None) is None


def test_signed_thumbnail_is_fetched_by_the_server_pinned_to_the_checked_address(api, monkeypatch, remote):
    thumb = extract_thumb(api, monkeypatch)
    r = api.get(thumb)
    assert r.status_code == 200 and r.content == PNG and r.headers["content-type"] == "image/png"
    assert r.headers["x-content-type-options"] == "nosniff" and "sandbox" in r.headers["content-security-policy"]
    assert r.headers["cache-control"].startswith("private")
    [req] = remote.requests
    assert req.url.host == PUBLIC and req.headers["host"] == "img.example"  # connected to the validated IP, not re-resolved
    assert "cookie" not in req.headers and "authorization" not in req.headers  # nothing of the user's goes to the image host


def test_this_is_not_an_open_image_proxy(api, monkeypatch, remote):
    thumb = extract_thumb(api, monkeypatch)
    q = parse_qs(urlsplit(thumb).query)
    forged = thumbs.sign("some-other-secret", "https://evil.example/x.png")
    bad = [
        f"/api/v1/thumb?u={q['u'][0]}&s={'0' * 32}",                         # wrong signature
        forged,                                                                # signed with a key we do not have
        f"/api/v1/thumb?u={q['u'][0]}x&s={q['s'][0]}",                         # tampered address
        "/api/v1/thumb?u=%%%&s=abc",                                           # garbage
    ]
    for path in bad:
        assert api.get(path).status_code in (403, 422), path
    assert remote.requests == [], "nothing was fetched for any unsigned or tampered request"


def test_thumbnail_to_an_internal_address_is_refused_even_if_signed(api, monkeypatch, remote):
    for hostile in ("http://169.254.169.254/latest/meta-data", "http://127.0.0.1:6379/", "http://10.0.0.5/x.png", "http://[::1]/x.png"):
        thumb = extract_thumb(api, monkeypatch, hostile)
        r = api.get(thumb)
        assert r.status_code == 400 and "refused" in r.json()["detail"], hostile
    assert remote.requests == []


@pytest.mark.parametrize("ctype", ["image/svg+xml", "text/html", "application/javascript", "application/octet-stream", ""])
def test_only_raster_images_are_served(api, monkeypatch, remote, ctype):
    remote.ctype = ctype
    r = api.get(extract_thumb(api, monkeypatch))
    assert r.status_code == 415  # SVG and HTML can carry script: never relayed from our origin


def test_oversized_and_failing_thumbnails_are_cut_off(api, monkeypatch, remote):
    thumb = extract_thumb(api, monkeypatch)
    remote.body = b"x" * (thumbs.MAX_BYTES + 5000)
    assert api.get(thumb).status_code == 413
    remote.body, remote.status = PNG, 404
    assert api.get(thumb).status_code == 502


def test_thumbnail_redirects_are_not_followed(api, monkeypatch, remote):
    remote.status, remote.location = 302, "http://169.254.169.254/latest/meta-data"
    assert api.get(extract_thumb(api, monkeypatch)).status_code == 502
    assert len(remote.requests) == 1  # only the first hop was ever requested


def test_takedowns_cover_thumbnails_and_the_endpoint_is_rate_limited(api, monkeypatch, remote):
    thumb = extract_thumb(api, monkeypatch, "https://takedown.example/t.png")
    api.portal.call(api.app.state.db.add_block, "host", "takedown.example", "dmca", None)
    api.app.state.rules.invalidate()
    assert api.get(thumb).status_code == 451
    ok = extract_thumb(api, monkeypatch)
    codes = [api.get(ok).status_code for _ in range(122)]
    assert codes[:119] == [200] * 119 and codes[119:] == [429] * 3  # 120 per minute, one already used by the 451 above


# --- tokens must not be written to access logs ------------------------------------------------------------------------

def emit_access(logger, path):
    """Shaped like uvicorn's access log: (client, method, path, http version, status)."""
    logger.info('%s - "%s %s HTTP/%s" %d', "10.0.0.1:5555", "GET", path, "1.1", 200)


def test_download_tokens_are_redacted_from_access_logs(logs):
    logging_setup.setup()
    access = logging.getLogger("uvicorn.access")
    emit_access(access, "/api/v1/jobs/abc123/file?token=SECRETCAPABILITY")
    emit_access(access, "/api/v1/batches/b1/zip?token=ANOTHERSECRET&x=1")
    emit_access(access, "/api/v1/thumb?u=aHR0cHM6Ly9l&s=0123456789abcdef")
    emit_access(access, "/api/v1/jobs/abc123")  # nothing sensitive: untouched
    assert logs.records, "positive control: the access lines were captured"
    text = logs.text
    assert "SECRETCAPABILITY" not in text and "ANOTHERSECRET" not in text and "0123456789abcdef" not in text
    assert "token=REDACTED" in text and "/api/v1/jobs/abc123" in text and "x=1" in text  # the rest of the line survives


def test_redaction_filter_unit():
    f = logging_setup.RedactTokens()
    rec = logging.LogRecord("x", logging.INFO, "", 0, "GET /f?token=abc&k=1", (), None)
    f.filter(rec)
    assert rec.msg == "GET /f?token=REDACTED&k=1"
    rec2 = logging.LogRecord("x", logging.INFO, "", 0, "%s", ("/a?token=zzz",), None)
    f.filter(rec2)
    assert rec2.args == ("/a?token=REDACTED",)
    assert io.StringIO is not None


# --- direct connections are pinned; behind an egress proxy the proxy resolves (and enforces) -----------------------------

def test_direct_requests_are_pinned_but_proxied_requests_go_by_hostname(monkeypatch):
    from hotload import webhooks

    def resolver(host, port):
        return [(2, 1, 6, "", (PUBLIC, port))]

    direct, headers, ext = webhooks.request_target("https://img.example/t.png", Settings(), resolver)
    assert PUBLIC in direct and headers == {"host": "img.example"} and ext == {"sni_hostname": "img.example"}

    monkeypatch.setenv("HTTPS_PROXY", "http://egress:3128")
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1")  # the no_proxy list alone must never count as "a proxy"
    proxied, headers, ext = webhooks.request_target("https://img.example/t.png", Settings(), resolver)
    assert proxied == "https://img.example/t.png" and headers == {} and ext == {}  # many proxies refuse CONNECT to a bare IP

    monkeypatch.delenv("HTTPS_PROXY")
    assert PUBLIC in webhooks.request_target("https://img.example/t.png", Settings(), resolver)[0]  # only no_proxy: still pinned


def test_the_destination_is_validated_before_either_path(monkeypatch):
    from hotload import webhooks

    monkeypatch.setenv("HTTPS_PROXY", "http://egress:3128")
    with pytest.raises(Exception, match="non-public"):
        webhooks.request_target("https://evil.example/x", Settings(), lambda h, p: [(2, 1, 6, "", ("10.0.0.5", p))])
