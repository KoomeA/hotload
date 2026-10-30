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
    monkeypatch.setattr(worker, "get_settings", lambda: Settings(data_dir=tmp_path))
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
