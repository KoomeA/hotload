"""Regression tests for the launch-readiness review of the fetch-and-process pipeline."""

import functools
import http.server
import shutil
import socket
import subprocess
import threading
import time
import urllib.request

import pytest
import yt_dlp
from fakeredis import FakeAsyncRedis
from fastapi.testclient import TestClient
from yt_dlp.utils import YoutubeDLError

from hotload import blocklist, engine, netguard, security, storage, webhooks
from hotload.api.app import create_app
from hotload.config import Settings
from hotload.db import BlockedItem

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg required")


def serve(handler_cls):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


# --- redirects, rebinding, literal IPs: the connect-time guard -------------------------------------------------------

class Servers:
    """A 'public' redirector (A) that bounces to an 'internal' server (B). Both are really on loopback, so the test
    models 'A is public' by letting the guard's address rule accept A's port only."""

    def __init__(self):
        self.hits = {"B": 0}
        outer = self

        class B(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                outer.hits["B"] += 1
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"internal-secret")

            def log_message(self, *a):
                pass

        self.b = serve(B)

        class A(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(302)
                self.send_header("location", f"http://127.0.0.1:{outer.b.server_port}/internal")
                self.end_headers()

            def log_message(self, *a):
                pass

        self.a = serve(A)

    def close(self):
        self.a.shutdown()
        self.b.shutdown()


@pytest.fixture
def redirector(monkeypatch):
    s = Servers()
    real_allowed = netguard.allowed
    monkeypatch.setattr(netguard, "allowed", lambda sa: sa[1] == s.a.server_port or real_allowed(sa))
    monkeypatch.setattr(engine.security, "validate_url", lambda url, settings, **kw: url)  # the pre-check passes the first hop
    yield s
    s.close()


def fetch_through(url):
    with engine.SafeYoutubeDL({"quiet": True}, Settings()) as ydl:
        return ydl.urlopen(url).read()


def test_a_redirect_to_an_internal_address_is_blocked_at_connect(redirector):
    """The review's finding 1a: validating only the first URL let a public page redirect into the internal network."""
    with pytest.raises(Exception, match="(?i)blocked|resolve"):
        fetch_through(f"http://127.0.0.1:{redirector.a.server_port}/start")
    assert redirector.hits["B"] == 0, "the internal server must never have been contacted"


def test_control_the_same_redirect_is_followed_when_the_guard_allows_it(redirector, monkeypatch):
    """Positive control: if the guard permitted B, the redirect would succeed, so the test above really measures the guard."""
    monkeypatch.setattr(netguard, "allowed", lambda sa: True)
    assert fetch_through(f"http://127.0.0.1:{redirector.a.server_port}/start") == b"internal-secret"
    assert redirector.hits["B"] == 1


def test_guard_blocks_literals_and_private_names_but_only_inside_the_guarded_thread():
    inside, outside = {}, {}

    def guarded():
        with netguard.guard():
            for host in ("127.0.0.1", "169.254.169.254", "10.1.2.3", "::1", "localhost"):
                try:
                    socket.getaddrinfo(host, 80)
                    inside[host] = "reachable"
                except socket.gaierror:
                    inside[host] = "blocked"

    def unguarded():  # e.g. a Redis or database connection in the same process
        try:
            outside["r"] = bool(socket.getaddrinfo("127.0.0.1", 6379))
        except socket.gaierror:
            outside["r"] = False

    for fn in (guarded, unguarded):
        t = threading.Thread(target=fn)
        t.start()
        t.join()
    assert set(inside.values()) == {"blocked"} and outside["r"] is True


def test_proxy_exemption_is_the_exact_host_and_port_only():
    with netguard.guard({("127.0.0.1", 3128)}):
        assert socket.getaddrinfo("127.0.0.1", 3128)  # the configured proxy
        with pytest.raises(socket.gaierror):
            socket.getaddrinfo("127.0.0.1", 8080)  # the rest of loopback stays unreachable


def test_no_proxy_list_is_never_mistaken_for_a_proxy(monkeypatch):
    for k in list(__import__("os").environ):
        if "proxy" in k.lower():
            monkeypatch.delenv(k)
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:3128")
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1,10.0.0.0/8")
    ydl = engine.SafeYoutubeDL({"quiet": True}, Settings())
    assert ydl._proxy_endpoints() == {("proxy.example", 3128)}
    assert ydl.params is not None and urllib.request.getproxies()["no"].startswith("localhost")


def test_guard_is_off_when_private_targets_are_allowed(redirector):
    srv_port = redirector.a.server_port
    with engine.SafeYoutubeDL({"quiet": True}, Settings(allow_private_targets=True)) as ydl:
        assert ydl.urlopen(f"http://127.0.0.1:{srv_port}/x").read() == b"internal-secret"  # dev/test switch only


# --- every URL handed to FFmpeg --------------------------------------------------------------------------------------

@pytest.fixture
def public_dns(monkeypatch):
    monkeypatch.setattr("socket.getaddrinfo", lambda host, port, *a, **k: [(2, 1, 6, "", ("93.184.216.34", port))])


def check_dl(info):
    with engine.SafeYoutubeDL({"quiet": True}, Settings()) as ydl, pytest.raises(YoutubeDLError, match="Blocked"):
        ydl.dl("x.mp4", info)


def test_dl_checks_every_url_ffmpeg_will_receive(public_dns):
    check_dl({"url": "https://ok.example/v.mp4\nhttp://169.254.169.254/latest/meta-data"})  # merged video+audio inputs
    check_dl({"url": "https://ok.example/v.mp4", "requested_formats": [{"url": "https://ok.example/a"}, {"url": "http://10.0.0.5/b"}]})
    check_dl({"url": "https://ok.example/v.mp4", "requested_formats": [{"url": "https://ok.example/a", "manifest_url": "http://192.168.1.1/m.m3u8"}]})
    check_dl({"url": "https://ok.example/v.mp4", "manifest_url": "http://127.0.0.1/m.m3u8"})


@pytest.mark.parametrize("url", ["ftp://10.0.0.5:6379/x.ts", "data:text/plain;base64,AAAA", "file:///etc/passwd", "gopher://x/", "rtmp://host/app"])
def test_non_http_schemes_are_refused(public_dns, url):
    check_dl({"url": url})
    with engine.SafeYoutubeDL({"quiet": True}, Settings()) as ydl, pytest.raises(YoutubeDLError, match="only http and https"):
        ydl.urlopen(url)


def test_ffmpeg_gets_a_protocol_whitelist_on_trim_and_live(tmp_path):
    s = Settings()
    for kw in ({"start": 1, "end": 9}, {"live_seconds": 60}):
        o = engine.build_download_opts(s, tmp_path, engine.PRESETS["best_mp4"], **kw)
        args = o["external_downloader_args"]["ffmpeg_i"]
        assert args[args.index("-protocol_whitelist") + 1] == "http,https,tcp,tls,crypto"  # no file:, ftp:, rtsp:, concat:
        assert "-fs" in o["external_downloader_args"]["ffmpeg"]


def test_hosted_mode_keeps_ffmpeg_network_features_off_until_the_egress_filter_is_confirmed(tmp_path):
    hosted = Settings(quotas_enabled=True)
    for kw in ({"start": 1, "end": 9}, {"live_seconds": 60}):
        with pytest.raises(engine.EngineError, match="temporarily unavailable"):
            engine.build_download_opts(hosted, tmp_path, engine.PRESETS["720p"], **kw)
        engine.build_download_opts(Settings(quotas_enabled=True, egress_filtered=True), tmp_path, engine.PRESETS["720p"], **kw)
        engine.build_download_opts(Settings(), tmp_path, engine.PRESETS["720p"], **kw)  # self-host: allowed


# --- extraction cannot pin threads or memory -------------------------------------------------------------------------

class Endless(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("content-type", "text/html")
        self.end_headers()
        try:
            while True:
                self.wfile.write(b"<p>" + b"x" * 8000 + b"</p>")
        except OSError:
            pass

    def log_message(self, *a):
        pass


class Drip(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("content-type", "text/html")
        self.end_headers()
        try:
            for _ in range(200):
                self.wfile.write(b"x")
                self.wfile.flush()
                time.sleep(0.3)
        except OSError:
            pass

    def log_message(self, *a):
        pass


def test_an_endless_page_is_cut_off_by_the_byte_cap():
    srv = serve(Endless)
    started = time.monotonic()
    try:
        with pytest.raises(engine.EngineError, match="too large"):
            engine.extract(f"http://127.0.0.1:{srv.server_port}/", Settings(allow_private_targets=True, extract_max_bytes=300_000))
    finally:
        srv.shutdown()
    assert time.monotonic() - started < 15


def test_a_dripping_server_is_cut_off_by_the_deadline():
    srv = serve(Drip)
    started = time.monotonic()
    try:
        with pytest.raises(engine.EngineError, match="too long"):
            engine.extract(f"http://127.0.0.1:{srv.server_port}/", Settings(allow_private_targets=True, extract_deadline_seconds=2))
    finally:
        srv.shutdown()
    assert time.monotonic() - started < 15  # a drip would otherwise hold the thread for 60 seconds, or forever


def make_api(tmp_path, monkeypatch, **o):
    monkeypatch.setattr("socket.getaddrinfo", lambda host, port, *a, **k: [(2, 1, 6, "", ("93.184.216.34", port))])

    async def enqueue(job_id, _q=None):
        pass

    c = TestClient(create_app(Settings(data_dir=tmp_path, rate_limit_per_minute=10000, secret_key="s", **o),
                              redis=FakeAsyncRedis(decode_responses=True), enqueue=enqueue))
    c.__enter__()
    return c


def test_extraction_has_its_own_bounded_pool_and_sheds_load_with_503(tmp_path, monkeypatch):
    c = make_api(tmp_path, monkeypatch, max_concurrent_extractions=1, extract_queue_seconds=0.3)
    release, entered = threading.Event(), threading.Event()

    def slow(url, s, proxy=None, **k):
        entered.set()
        release.wait(10)
        return {"title": "t", "site": "X", "id": "1", "age_limit": 0}

    monkeypatch.setattr(engine, "extract", slow)
    first = {}
    t = threading.Thread(target=lambda: first.update(r=c.post("/api/v1/extract", json={"url": "https://example.com/a"})))
    t.start()
    assert entered.wait(5)
    busy = c.post("/api/v1/extract", json={"url": "https://example.com/b"})
    assert busy.status_code == 503 and "busy" in busy.json()["detail"]
    # the rest of the API is NOT starved while an extraction hangs
    assert c.get("/api/v1/presets").status_code == 200 and c.post("/api/v1/reports", json={
        "url": "https://example.com/x", "kind": "abuse", "contact": "a@b.example"}).status_code == 201
    release.set()
    t.join(10)
    assert first["r"].status_code == 200
    assert c.post("/api/v1/extract", json={"url": "https://example.com/c"}).status_code == 200  # slot is free again
    c.__exit__(None, None, None)


# --- size and duration ---------------------------------------------------------------------------------------------------

def test_ffmpeg_output_is_bounded_whatever_the_source_claims(tmp_path):
    s = Settings()
    o = engine.build_download_opts(s, tmp_path, engine.PRESETS["mp3"], max_duration=300, max_filesize=50 * 1024 * 1024)
    assert o["postprocessor_args"]["extractaudio"] == ["-t", "300", "-fs", str(50 * 1024 * 1024)]
    assert o["max_filesize"] == 50 * 1024 * 1024
    big = engine.build_download_opts(s, tmp_path, engine.PRESETS["mp3"], max_filesize=10**15)
    assert big["max_filesize"] == s.max_filesize_bytes  # a job can ask for less than the server cap, never more


def test_running_byte_count_aborts_a_download_that_lies_about_its_size(tmp_path):
    seen = []
    o = engine.build_download_opts(Settings(), tmp_path, engine.PRESETS["mp3"], max_filesize=1000, progress=seen.append)
    hook = o["progress_hooks"][0]
    hook({"status": "downloading", "downloaded_bytes": 900})
    assert seen and seen[0]["downloaded_bytes"] == 900  # progress still reaches the caller
    with pytest.raises(yt_dlp.utils.DownloadError, match="larger than your plan"):
        hook({"status": "downloading", "downloaded_bytes": 1001})


@needs_ffmpeg
def test_duration_is_verified_after_download_when_the_site_reports_none(tmp_path):
    media = tmp_path / "clip.mp4"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=d=8:s=96x64:r=5", "-pix_fmt", "yuv420p", str(media)], check=True)
    engine.verify_duration(media, 60)  # within the plan: untouched
    assert media.exists()
    with pytest.raises(engine.EngineError, match="longer than your plan"):
        engine.verify_duration(media, 3)  # 8s file, 3s plan (the +5%/+2s container slack does not rescue it)
    assert not media.exists(), "an over-long file is deleted, not delivered"


def test_verify_duration_is_skipped_without_ffprobe(tmp_path, monkeypatch):
    f = tmp_path / "x.mp4"
    f.write_bytes(b"x")

    def boom(*a, **k):
        raise FileNotFoundError("ffprobe")

    monkeypatch.setattr(subprocess, "run", boom)
    engine.verify_duration(f, 1)
    assert f.exists()


@needs_ffmpeg
def test_an_over_long_file_without_a_reported_duration_is_refused_end_to_end(tmp_path):
    src = tmp_path / "srv"
    src.mkdir()
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=d=8:s=96x64:r=5", "-f", "lavfi", "-i", "sine=d=8",
                    "-shortest", "-pix_fmt", "yuv420p", str(src / "long.mp4")], check=True)
    srv = serve(functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(src)))
    s = Settings(allow_private_targets=True)
    url = f"http://127.0.0.1:{srv.server_port}/long.mp4"
    try:
        with pytest.raises(engine.EngineError, match="longer than your plan"):
            engine.download(url, s, tmp_path / "o1", engine.PRESETS["best_mp4"], max_duration=3)
        assert not list((tmp_path / "o1").glob("*.mp4")), "nothing over-long is left behind"
        audio = engine.download(url, s, tmp_path / "o2", engine.PRESETS["mp3"], max_duration=3)  # audio is also truncated by -t
        secs = float(subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(audio.path)]))
        assert audio.path.suffix == ".mp3" and secs < 4.5
    finally:
        srv.shutdown()


# --- playlists, subtitles, parsing, small hardening -------------------------------------------------------------------

def test_one_url_is_one_download_but_playlist_listing_still_works(tmp_path):
    s = Settings()
    assert engine._base_opts(s)["playlist_items"] == "1"
    assert engine.build_download_opts(s, tmp_path, engine.PRESETS["mp3"])["playlist_items"] == "1"
    assert engine._base_opts(s)["concurrent_fragment_downloads"] == 1


@pytest.mark.parametrize("code", ["all", "ALL", "All"])
def test_the_all_subtitles_wildcard_is_refused(code, tmp_path):
    with pytest.raises(ValueError):
        engine.validate_lang(code)
    with pytest.raises(ValueError):
        engine.build_download_opts(Settings(), tmp_path, engine.PRESETS["720p"], subtitle_langs=[code])


def test_all_subtitles_wildcard_is_a_422_over_the_api(tmp_path, monkeypatch):
    c = make_api(tmp_path, monkeypatch)
    assert c.post("/api/v1/jobs", json={"url": "https://example.com/v", "subtitle_langs": ["all"]}).status_code == 422
    c.__exit__(None, None, None)


@pytest.mark.parametrize("url", ["http://example.com:99999/", "http://example.com:abc/", "http://example.com:-1/", "https://[::1]:70000/"])
def test_bad_ports_are_a_clean_refusal_not_a_500(url):
    with pytest.raises(security.UnsafeURL):
        security.validate_url(url, Settings(), resolve=lambda h, p: [(2, 1, 6, "", ("93.184.216.34", 80))])


@pytest.mark.parametrize("ip,public", [
    ("93.184.216.34", True), ("2606:4700:4700::1111", True), ("::ffff:8.8.8.8", True),
    ("64:ff9b::a9fe:a9fe", False),   # NAT64 form of 169.254.169.254
    ("64:ff9b::808:808", False),     # NAT64 forms are refused even for public IPv4 (conservative)
    ("64:ff9b:1::1", False), ("2002:a00:1::", False), ("2001:0:4136:e378::1", False),
    ("::ffff:10.0.0.1", False), ("::ffff:127.0.0.1", False), ("fe80::1%eth0", False), ("169.254.169.254", False),
    ("100.64.0.1", False), ("0.0.0.0", False), ("not-an-ip", False)])
def test_public_ip_rule(ip, public):
    assert security.is_public_ip(ip) is public


def test_error_text_never_names_the_operators_proxy():
    e = engine._wrap(yt_dlp.utils.DownloadError("ERROR: Unable to connect to proxy http://10.9.8.7:3128 (Connection refused)"))
    assert "10.9.8" not in str(e) and "proxy" not in str(e).lower() and e.retryable is True  # still retried on another route


def test_empty_s3_prefix_is_refused(tmp_path):
    with pytest.raises(ValueError, match="PREFIX"):
        storage.S3Storage(Settings(data_dir=tmp_path, s3_bucket="b", s3_prefix="", storage_backend="s3"), client=object())
    with pytest.raises(ValueError):
        storage.S3Storage(Settings(data_dir=tmp_path, s3_bucket="b", s3_prefix="/", storage_backend="s3"), client=object())
    storage.S3Storage(Settings(data_dir=tmp_path, s3_bucket="b", s3_prefix="hotload/", storage_backend="s3"), client=object())


def test_blocklist_matches_unicode_and_punycode_forms():
    rules = blocklist.Blocklist([BlockedItem(kind="host", value=blocklist.normalise_value("host", "https://Bücher.de/"))])
    assert rules.blocks_url("https://xn--bcher-kva.de/x") and rules.blocks_url("https://shop.xn--bcher-kva.de/") and rules.blocks_url("https://bücher.de/")
    assert not rules.blocks_url("https://notbucher.de/")
    url_rule = blocklist.Blocklist([BlockedItem(kind="url", value=blocklist.normalise_url("https://Bücher.de/a/"))])
    assert url_rule.blocks_url("https://xn--bcher-kva.de/a")


def test_webhook_host_header_keeps_a_non_default_port():
    target, host = webhooks._pin("https://hooks.example.com:8443/in", Settings(), lambda h, p: [(2, 1, 6, "", ("93.184.216.34", p))])
    assert host == "hooks.example.com:8443" and target.startswith("https://93.184.216.34:8443/")
    assert webhooks._pin("https://hooks.example.com/in", Settings(), lambda h, p: [(2, 1, 6, "", ("93.184.216.34", p))])[1] == "hooks.example.com"


def test_packaging_ignores_sidecar_files(tmp_path):
    (tmp_path / "v.mp4").write_bytes(b"video")
    for name in ("v.description", "v.meta", "v.orig", "v.uncut", "v.info.json", "v.jpg"):
        (tmp_path / name).write_bytes(b"x")
    assert engine.package(tmp_path).name == "v.mp4"  # one deliverable, no sidecars zipped in


# The bot-wide cap on top of the per-requester one is tested by behaviour in test_bot.py and test_discord.py.
