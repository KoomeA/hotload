import functools
import http.server
import json
import shutil
import subprocess
import threading

import pytest
from fakeredis import FakeAsyncRedis

from hotload import canary, metrics
from hotload.config import Settings


async def test_metrics_render_and_labels():
    r = FakeAsyncRedis(decode_responses=True)
    await metrics.inc(r, "hotload_jobs_total", state="done", preset="mp3")
    await metrics.inc(r, "hotload_jobs_total", state="done", preset="mp3")
    await metrics.observe(r, "hotload_job_seconds", 2.5, state="done")
    await metrics.set_gauge(r, "hotload_canary_up", 1, site='a"b')
    await metrics.set_gauge(r, "hotload_canary_last_run_timestamp", 1793365336)
    out = await metrics.render(r)
    assert 'hotload_jobs_total{preset="mp3",state="done"} 2' in out
    assert 'hotload_job_seconds_sum{state="done"} 2.5' in out and 'hotload_job_seconds_count{state="done"} 1' in out
    assert 'hotload_canary_up{site="a\\"b"} 1' in out
    assert "hotload_canary_last_run_timestamp 1793365336\n" in out  # no exponent notation


def test_load_canaries_builtin_and_validation(tmp_path):
    assert {c["name"] for c in canary.load_canaries()} >= {"youtube", "archive-org"}
    bad = tmp_path / "c.json"
    bad.write_text(json.dumps([{"name": "x"}]))
    with pytest.raises(ValueError):
        canary.load_canaries(bad)


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg required")
async def test_canary_ok_fail_and_degraded_status(tmp_path):
    src = tmp_path / "srv"
    src.mkdir()
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=d=2:s=96x64:r=5",
                    "-pix_fmt", "yuv420p", str(src / "c.mp4")], check=True)
    srv = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(src)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"
    settings = Settings(allow_private_targets=True, canary_degraded_after=2)
    canaries = [{"name": "good", "site": "local", "url": f"{base}/c.mp4"},
                {"name": "bad", "site": "broken", "url": f"{base}/missing.mp4"}]
    redis = FakeAsyncRedis(decode_responses=True)
    try:
        for _ in range(2):
            results = await canary.run_canaries(settings, canaries=canaries)
        by = {r["name"]: r for r in results}
        assert by["good"]["ok"] and by["good"]["probe"] in (200, 206)
        assert not by["bad"]["ok"] and by["bad"]["error"]

        async def fake_load(path=None):
            return canaries

        orig = canary.load_canaries
        canary.load_canaries = lambda p=None: canaries
        try:
            await canary.run_and_record(redis, settings)
            summary = await canary.run_and_record(redis, settings)
        finally:
            canary.load_canaries = orig
        assert summary["degraded"] == ["broken"]
        assert "hotload_canary_up" in await metrics.render(redis)
    finally:
        srv.shutdown()


def test_status_and_metrics_endpoints(client, settings):
    assert client.get("/api/v1/status").json()["canary"] is None
    assert client.get("/metrics").status_code == 200


def test_canary_probes_audio_and_video_urls_separately(monkeypatch):
    """A site can serve the video URL but block the audio-only one; the canary must notice."""
    from hotload import engine

    info = {"formats": [
        {"url": "https://cdn.example/v.mp4", "height": 720, "acodec": "none", "vcodec": "avc1", "protocol": "https"},
        {"url": "https://cdn.example/a.m4a", "acodec": "aac", "vcodec": "none", "abr": 128, "protocol": "https"},
    ]}

    class FakeYDL:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=False):
            return info

    probed = []
    monkeypatch.setattr(engine, "SafeYoutubeDL", FakeYDL)
    monkeypatch.setattr(canary, "_probe", lambda url, h, s, p: probed.append(url) or (206 if url.endswith("mp4") else 403))
    r = canary.check_one({"name": "x", "site": "x", "url": "https://example.com/v"}, Settings())
    assert probed == ["https://cdn.example/v.mp4", "https://cdn.example/a.m4a"]
    assert r["probes"] == [206, 403] and r["ok"] is False and "403" in r["error"]
