import pytest
from fakeredis import FakeAsyncRedis
from fastapi.testclient import TestClient

from hotload.api.app import create_app
from hotload.config import Settings


@pytest.fixture
def settings(tmp_path):
    return Settings(data_dir=tmp_path, rate_limit_per_minute=5, require_age_gate=True)


@pytest.fixture
def enqueued():
    return []


@pytest.fixture
def client(settings, enqueued, monkeypatch):
    # Resolve every hostname to a public address so tests never touch DNS.
    monkeypatch.setattr("socket.getaddrinfo", lambda host, port, *a, **k: [(2, 1, 6, "", ("93.184.216.34", port))])

    async def enqueue(job_id, _queue=None):
        enqueued.append(job_id)

    app = create_app(settings, redis=FakeAsyncRedis(decode_responses=True), enqueue=enqueue)
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def frozen_rate_limit_clock(monkeypatch):
    """Rate limits use fixed windows. A test that sends N requests must not straddle a window boundary and see the
    counter reset halfway (a ~1-2% flake per test otherwise), so the limiter's clock is pinned for each test."""
    import time
    import types

    from hotload import ratelimit

    now = time.time()
    monkeypatch.setattr(ratelimit, "time", types.SimpleNamespace(time=lambda: now))


class _Collector:
    def __init__(self):
        import logging

        self.records: list = []

        class H(logging.Handler):
            def emit(inner, record):
                self.records.append(record)

        self.handler = H(level=logging.DEBUG)

    @property
    def text(self) -> str:
        return "\n".join(r.getMessage() + " " + str(getattr(r, "ctx", "")) for r in self.records)


@pytest.fixture
def logs():
    """Capture log records from the ROOT logger. pytest's caplog is unusable once the app has started: its startup runs
    logging_setup.setup(), which replaces the root handlers (so caplog silently records nothing and negative assertions
    like "the token is not in the logs" pass vacuously). Attach after the app starts, and always add a positive control."""
    import logging

    c = _Collector()
    root = logging.getLogger()
    root.addHandler(c.handler)
    previous = root.level
    root.setLevel(logging.DEBUG)
    yield c
    root.removeHandler(c.handler)
    root.setLevel(previous)
