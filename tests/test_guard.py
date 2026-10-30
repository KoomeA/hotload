import http.server
import threading

import pytest

from hotload import engine
from hotload.config import Settings


@pytest.fixture
def local_server():
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"x")

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}/"
    srv.shutdown()


def test_urlopen_blocks_private_targets(local_server):
    with engine.SafeYoutubeDL({"quiet": True}, Settings()) as ydl, pytest.raises(Exception, match="unsafe address"):
        ydl.urlopen(local_server)


def test_urlopen_allows_when_disabled(local_server):
    with engine.SafeYoutubeDL({"quiet": True}, Settings(allow_private_targets=True)) as ydl:
        assert ydl.urlopen(local_server).status == 200


def test_dl_blocks_private_media_url_used_by_ffmpeg(local_server):
    with engine.SafeYoutubeDL({"quiet": True}, Settings()) as ydl, pytest.raises(Exception, match="unsafe address"):
        ydl.dl("x.mp4", {"url": local_server + "x.mp4"})


def test_extract_error_is_wrapped_and_not_retryable(local_server):
    with pytest.raises(engine.EngineError) as e:
        engine.extract(local_server, Settings())
    assert "unsafe address" in str(e.value) and e.value.retryable is False


def test_retryable_classification():
    assert engine._wrap(Exception("HTTP Error 429: Too Many Requests")).retryable
    assert engine._wrap(Exception("Sign in to confirm you're not a bot")).retryable
    assert not engine._wrap(Exception("Unsupported URL: https://x")).retryable
