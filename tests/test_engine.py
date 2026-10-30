import functools
import http.server
import shutil
import subprocess
import threading

import pytest

from hotload import engine, storage
from hotload.config import Settings


def test_presets_build_options(tmp_path):
    opts = engine.build_download_opts(Settings(), tmp_path, engine.PRESETS["mp3"])
    assert opts["postprocessors"][0]["preferredcodec"] == "mp3" and opts["allow_unplayable_formats"] is False
    clip = engine.build_download_opts(Settings(), tmp_path, engine.PRESETS["720p"], proxy="http://p:1", start=5, end=15)
    assert "download_ranges" in clip and clip["proxy"] == "http://p:1" and clip["merge_output_format"] == "mp4"


def test_filter_rules():
    f = engine._duration_filter(60, None, None, block_adult=True)
    assert f({"duration": 30}, incomplete=False) is None
    assert "limit" in f({"duration": 120}, incomplete=False)
    assert "Age" in f({"age_limit": 18, "duration": 1}, incomplete=False)
    assert "live stream" in f({"is_live": True}, incomplete=False)
    assert engine._duration_filter(60, 0, 30)({"duration": 600}, incomplete=False) is None  # trimmed selection fits


def test_cleanup_expired(tmp_path):
    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir(), new.mkdir()
    assert storage.cleanup_expired(tmp_path, 3600, now=old.stat().st_mtime + 7200) == 2
    assert storage.cleanup_expired(tmp_path, 3600) == 0


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg required")
def test_download_and_trim_local_media(tmp_path):
    """End-to-end engine test against a locally served MP4 (bypasses the API's SSRF guard on purpose)."""
    src = tmp_path / "srv"
    src.mkdir()
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=d=6:s=160x120:r=10",
         "-f", "lavfi", "-i", "sine=d=6", "-shortest", "-pix_fmt", "yuv420p", str(src / "clip.mp4")],
        check=True,
    )
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(src))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/clip.mp4"
        out = engine.download(url, Settings(allow_private_targets=True), tmp_path / "o1", engine.PRESETS["best_mp4"])
        assert out.path.stat().st_size > 0 and out.title == "clip"
        mp3 = engine.download(url, Settings(allow_private_targets=True), tmp_path / "o2", engine.PRESETS["mp3"])
        assert mp3.path.suffix == ".mp3"
    finally:
        server.shutdown()
