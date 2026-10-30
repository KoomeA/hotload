import asyncio
import functools
import http.server
import shutil
import subprocess
import threading

import pytest
from fakeredis import FakeAsyncRedis

from hotload import worker
from hotload.config import Settings
from hotload.jobs import JobStore


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg required")
async def test_run_download_marks_done_and_error(tmp_path, monkeypatch):
    monkeypatch.setattr(worker, "get_settings", lambda: Settings(data_dir=tmp_path, allow_private_targets=True))
    src = tmp_path / "srv"
    src.mkdir()
    await asyncio.to_thread(subprocess.run, ["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=d=3:s=160x120:r=10",
                    "-pix_fmt", "yuv420p", str(src / "c.mp4")], check=True)
    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(src)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    store = JobStore(FakeAsyncRedis(decode_responses=True), 600)
    try:
        ok = await store.create(url=f"http://127.0.0.1:{server.server_port}/c.mp4", preset="best_mp4")
        await worker.run_download({"store": store}, ok.id)
        done = await store.get(ok.id)
        assert done.state == "done" and done.progress == 100 and done.path.endswith(".mp4")

        bad = await store.create(url=f"http://127.0.0.1:{server.server_port}/missing.mp4", preset="best_mp4")
        await worker.run_download({"store": store}, bad.id)
        failed = await store.get(bad.id)
        assert failed.state == "error" and failed.message
    finally:
        server.shutdown()


async def test_worker_rotates_proxy_on_retryable_failure_and_records_metrics(tmp_path, monkeypatch):
    from hotload import engine, metrics
    from hotload.proxies import ProxyPool

    settings = Settings(data_dir=tmp_path, max_attempts=3)
    monkeypatch.setattr(worker, "get_settings", lambda: settings)
    pool = ProxyPool(default=["http://a:1", "http://b:2"], threshold=1)
    monkeypatch.setattr(worker, "_pool", pool)
    seen = []

    def fake_download(url, s, workdir, preset, proxy=None, **kw):
        seen.append(proxy)
        if proxy == "http://a:1":
            raise engine.EngineError("HTTP Error 403: Forbidden", retryable=True)
        workdir.mkdir(parents=True, exist_ok=True)
        out = workdir / "v.mp4"
        out.write_bytes(b"x")
        return engine.DownloadResult(out, "Title", "Generic")

    monkeypatch.setattr(engine, "download", fake_download)
    redis = FakeAsyncRedis(decode_responses=True)
    store = JobStore(redis, 600)
    job = await store.create(url="https://example.com/v", preset="best_mp4")
    await worker.run_download({"store": store}, job.id)
    done = await store.get(job.id)
    assert seen == ["http://a:1", "http://b:2"] and done.state == "done"
    assert done.title == "Title" and done.site == "Generic"
    assert pool.health()[0]["cooling_down"] is True  # the 403ing proxy was benched
    out = await metrics.render(redis)
    assert 'hotload_jobs_total{adult="false",preset="best_mp4",state="done"} 1' in out


async def test_worker_does_not_retry_permanent_errors(tmp_path, monkeypatch):
    from hotload import engine
    from hotload.proxies import ProxyPool

    monkeypatch.setattr(worker, "get_settings", lambda: Settings(data_dir=tmp_path))
    monkeypatch.setattr(worker, "_pool", ProxyPool(default=["http://a:1", "http://b:2"]))
    calls = []

    def fake_download(url, s, workdir, preset, proxy=None, **kw):
        calls.append(proxy)
        raise engine.EngineError("Unsupported URL")

    monkeypatch.setattr(engine, "download", fake_download)
    store = JobStore(FakeAsyncRedis(decode_responses=True), 600)
    job = await store.create(url="https://example.com/v", preset="best_mp4")
    await worker.run_download({"store": store}, job.id)
    failed = await store.get(job.id)
    assert len(calls) == 1 and failed.state == "error" and failed.message == "Unsupported URL"


async def test_worker_updates_history_for_signed_in_users(tmp_path, monkeypatch):
    from hotload import engine
    from hotload.db import Database
    from hotload.proxies import ProxyPool

    settings = Settings(data_dir=tmp_path)
    monkeypatch.setattr(worker, "get_settings", lambda: settings)
    monkeypatch.setattr(worker, "_pool", ProxyPool())

    def fake_download(url, s, workdir, preset, proxy=None, **kw):
        workdir.mkdir(parents=True, exist_ok=True)
        (workdir / "v.mp3").write_bytes(b"x")
        return engine.DownloadResult(workdir / "v.mp3", "A fine title", "Generic")

    monkeypatch.setattr(engine, "download", fake_download)
    db = Database(settings)
    await db.create_all()
    user = await db.add_user(email="u@example.com", password_hash="x")
    store = JobStore(FakeAsyncRedis(decode_responses=True), 600)
    job = await store.create(url="https://example.com/v", preset="mp3", user_id=user.id)
    await db.add_history(user.id, job)
    await worker.run_download({"store": store, "db": db}, job.id)
    [entry] = await db.list_history(user.id)
    assert entry["state"] == "done" and entry["title"] == "A fine title" and entry["site"] == "Generic"
    await db.dispose()
