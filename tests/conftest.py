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
