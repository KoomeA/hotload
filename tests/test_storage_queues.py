import subprocess
import sys
import time
import zipfile
from io import BytesIO

import boto3
import pytest
import requests
from fakeredis import FakeAsyncRedis
from fastapi.testclient import TestClient
from moto import mock_aws

from hotload import engine, storage, tiers, worker
from hotload.api.app import create_app
from hotload.config import Settings
from hotload.jobs import JobStore
from hotload.proxies import ProxyPool

BUCKET = "hotload-test"
S3 = {"storage_backend": "s3", "s3_bucket": BUCKET, "s3_region": "us-east-1", "s3_access_key_id": "k",
      "s3_secret_access_key": "s", "s3_prefix": "hl/", "s3_presign_seconds": 600}


@pytest.fixture
def aws():
    with mock_aws():
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=BUCKET)
        yield


@pytest.fixture
def remote(aws, tmp_path):
    return storage.S3Storage(Settings(data_dir=tmp_path, **S3))


def make_file(tmp_path, name="clip.mp3", data=b"audio-bytes"):
    p = tmp_path / "work" / name
    p.parent.mkdir(exist_ok=True)
    p.write_bytes(data)
    return p


# --- S3Storage ------------------------------------------------------------------------------------

def test_put_signed_url_serves_the_file_as_an_attachment(remote, tmp_path):
    key = remote.put("job1", make_file(tmp_path))
    assert key == "hl/job1/clip.mp3"
    url = remote.presigned_url(key, "My Clip.mp3")
    assert "X-Amz-Signature" in url and "X-Amz-Expires=600" in url
    r = requests.get(url, timeout=10)
    assert r.status_code == 200 and r.content == b"audio-bytes"
    assert r.headers["content-disposition"] == 'attachment; filename="My Clip.mp3"'


def test_filenames_cannot_inject_headers_or_paths(remote, tmp_path):
    evil = 'a"\r\nSet-Cookie: x=1/../../etc.mp3'
    url = remote.presigned_url(remote.key_for("j", evil), evil)
    assert "\r" not in url and "\n" not in url and "%0D" not in url and "%0A" not in url
    for name in ("../../x.mp3", "a/b.mp3", "..", ".", "", ".hidden", "\\..\\x"):
        last = remote.key_for("j", name).removeprefix("hl/j/")
        assert "/" not in last and last not in ("", ".", "..") and not last.startswith(".")  # one safe path segment


def test_chunks_stream_the_object(remote, tmp_path):
    key = remote.put("j", make_file(tmp_path, data=b"x" * 5000))
    assert b"".join(remote.chunks(key, 1024)) == b"x" * 5000 and remote.exists(key) and not remote.exists("hl/nope")


def test_cleanup_deletes_only_expired_objects_under_the_prefix(remote, tmp_path):
    key = remote.put("old", make_file(tmp_path))
    remote.client.put_object(Bucket=BUCKET, Key="other-app/keep.txt", Body=b"keep")
    assert remote.cleanup_expired(3600, now=time.time()) == 0 and remote.exists(key)
    assert remote.cleanup_expired(3600, now=time.time() + 7200) == 1
    assert not remote.exists(key) and remote.exists("other-app/keep.txt")


def test_upload_failure_raises_storage_error(aws, tmp_path):
    bad = storage.S3Storage(Settings(data_dir=tmp_path, **{**S3, "s3_bucket": "no-such-bucket"}))
    with pytest.raises(storage.StorageError):
        bad.put("j", make_file(tmp_path))


def test_backend_selection(tmp_path):
    assert storage.build_remote(Settings(data_dir=tmp_path)) is None
    assert storage.build_remote(Settings(data_dir=tmp_path, storage_backend="s3")) is None  # no bucket: stay local


# --- worker --------------------------------------------------------------------------------------

def patch_download(monkeypatch, tmp_path):
    def fake(url, s, workdir, preset, proxy=None, **kw):
        workdir.mkdir(parents=True, exist_ok=True)
        (workdir / "v.mp3").write_bytes(b"audio-bytes")
        return engine.DownloadResult(workdir / "v.mp3", "T", "Generic")

    monkeypatch.setattr(engine, "download", fake)
    monkeypatch.setattr(worker, "_pool", ProxyPool())


async def test_worker_uploads_then_removes_the_local_copy(aws, tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path, **S3)
    monkeypatch.setattr(worker, "get_settings", lambda: settings)
    patch_download(monkeypatch, tmp_path)
    remote = storage.S3Storage(settings)
    store = JobStore(FakeAsyncRedis(decode_responses=True), 600)
    job = await store.create(url="https://example.com/v", preset="mp3")
    await worker.run_download({"store": store, "remote": remote}, job.id)
    done = await store.get(job.id)
    assert done.state == "done" and done.storage_key == f"hl/{job.id}/v.mp3" and done.path is None
    assert remote.exists(done.storage_key) and not (settings.output_dir / job.id).exists()
    assert "storage_key" not in done.public() and done.public()["download_url"]


async def test_worker_reports_upload_failure_and_keeps_nothing_dangling(aws, tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path, **S3)
    monkeypatch.setattr(worker, "get_settings", lambda: settings)
    patch_download(monkeypatch, tmp_path)

    class Broken:
        def put(self, *a):
            raise storage.StorageError("Could not store the finished file")

    store = JobStore(FakeAsyncRedis(decode_responses=True), 600)
    job = await store.create(url="https://example.com/v", preset="mp3", key_id="k", webhook_url="https://hooks.example.com/x")
    queued = []

    class Q:
        async def enqueue_job(self, *a, **k):
            queued.append((a, k))

    await worker.run_download({"store": store, "remote": Broken(), "redis": Q()}, job.id)
    failed = await store.get(job.id)
    assert failed.state == "error" and failed.message == "Could not store the finished file" and failed.storage_key is None


async def test_cleanup_cron_covers_the_bucket_too(aws, tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path, file_ttl_seconds=-1, **S3)  # everything counts as expired
    monkeypatch.setattr(worker, "get_settings", lambda: settings)
    remote = storage.S3Storage(settings)
    key = remote.put("j", make_file(tmp_path))
    await worker.cleanup({"remote": remote})
    assert not remote.exists(key)


async def test_webhook_follow_up_goes_to_the_same_queue(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path)
    monkeypatch.setattr(worker, "get_settings", lambda: settings)
    patch_download(monkeypatch, tmp_path)
    calls = []

    class Q:
        async def enqueue_job(self, name, *args, **kwargs):
            calls.append((name, kwargs))

    store = JobStore(FakeAsyncRedis(decode_responses=True), 600)
    job = await store.create(url="https://example.com/v", preset="mp3", key_id="k", webhook_url="https://hooks.example.com/x")
    await worker.run_download({"store": store, "redis": Q(), "queue_name": "hotload:priority"}, job.id)
    assert calls == [("deliver_webhook", {"_queue_name": "hotload:priority"})]


def test_worker_queue_is_configurable_by_environment():
    out = subprocess.run([sys.executable, "-c", "from hotload.worker import WorkerSettings as W; print(W.queue_name)"],
                         capture_output=True, text=True, env={"HOTLOAD_WORKER_QUEUE": "hotload:priority", "PATH": "/usr/bin:/bin"},
                         cwd="/tmp", timeout=60)
    assert out.stdout.strip().splitlines()[-1] == "hotload:priority", out.stderr
    default = subprocess.run([sys.executable, "-c", "from hotload.worker import WorkerSettings as W; print(W.queue_name)"],
                             capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"}, cwd="/tmp", timeout=60)
    assert default.stdout.strip().splitlines()[-1] == "arq:queue"


# --- API: redirect, ZIP, queues ------------------------------------------------------------------------

@pytest.fixture
def s3app(aws, tmp_path, monkeypatch):
    monkeypatch.setattr("socket.getaddrinfo", lambda host, port, *a, **k: [(2, 1, 6, "", ("93.184.216.34", port))])
    seen = []

    async def enqueue(job_id, queue=None):
        seen.append((job_id, queue))

    def build(**overrides):
        settings = Settings(data_dir=tmp_path, rate_limit_per_minute=1000, secret_key="s", **{**S3, **overrides})
        c = TestClient(create_app(settings, redis=FakeAsyncRedis(decode_responses=True), enqueue=enqueue))
        c.__enter__()
        c.seen = seen
        return c

    clients = []

    def tracked(**o):
        c = build(**o)
        clients.append(c)
        return c

    yield tracked
    for c in clients:
        c.__exit__(None, None, None)


def finish(c, job, key=None, path=None, name="v.mp3"):
    async def go():
        j = await c.app.state.store.get(job)
        j.state, j.filename, j.storage_key, j.path = "done", name, key, path
        await c.app.state.store.save(j)
        return j.token

    return c.portal.call(go)


def test_file_download_redirects_to_a_short_lived_signed_url(s3app, tmp_path):
    c = s3app()
    job = c.post("/api/v1/jobs", json={"url": "https://example.com/v"}).json()["id"]
    key = c.app.state.remote.put(job, make_file(tmp_path))
    token = finish(c, job, key=key)
    assert c.get(f"/api/v1/jobs/{job}/file?token=wrong", follow_redirects=False).status_code == 403
    r = c.get(f"/api/v1/jobs/{job}/file?token={token}", follow_redirects=False)
    assert r.status_code == 302 and "X-Amz-Signature" in r.headers["location"]
    assert r.headers["cache-control"] == "no-store" and r.headers["referrer-policy"] == "no-referrer"
    assert requests.get(r.headers["location"], timeout=10).content == b"audio-bytes"


def test_batch_zip_streams_from_the_bucket_and_mixes_in_local_files(s3app, tmp_path):
    c = s3app()
    b = c.post("/api/v1/batches", json={"urls": ["https://example.com/a", "https://example.com/b"]}).json()
    ids = [j["id"] for j in b["jobs"]]
    key = c.app.state.remote.put(ids[0], make_file(tmp_path, "remote.mp3", b"from-bucket"))
    finish(c, ids[0], key=key, name="remote.mp3")
    local = tmp_path / "out" / ids[1]
    local.mkdir(parents=True)
    (local / "local.mp3").write_bytes(b"from-disk")
    finish(c, ids[1], path=str(local / "local.mp3"), name="local.mp3")
    zip_url = c.get(f"/api/v1/batches/{b['id']}").json()["zip_url"]
    z = zipfile.ZipFile(BytesIO(c.get(zip_url).content))
    assert z.namelist() == ["01 - remote.mp3", "02 - local.mp3"]
    assert z.read("01 - remote.mp3") == b"from-bucket" and z.read("02 - local.mp3") == b"from-disk"


def test_remote_file_is_unavailable_if_the_app_runs_without_the_bucket(s3app, tmp_path):
    c = s3app()
    job = c.post("/api/v1/jobs", json={"url": "https://example.com/v"}).json()["id"]
    token = finish(c, job, key="hl/x/v.mp3")
    c.app.state.remote = None  # misconfiguration: never crash, never leak
    assert c.get(f"/api/v1/jobs/{job}/file?token={token}", follow_redirects=False).status_code == 404


def test_paying_plans_use_the_priority_queue_only_when_plans_are_enforced(s3app):
    on = s3app(quotas_enabled=True)
    on.post("/api/v1/jobs", json={"url": "https://example.com/anon"})
    on.post("/api/v1/auth/register", json={"email": "p@example.com", "password": "correct horse battery"})
    uid = on.get("/api/v1/me").json()["id"]
    on.post("/api/v1/jobs", json={"url": "https://example.com/free"})
    on.portal.call(on.app.state.db.set_tier, uid, "premium", time.time() + 86400)
    on.post("/api/v1/jobs", json={"url": "https://example.com/premium"})
    on.post("/api/v1/batches", json={"urls": ["https://example.com/b1", "https://example.com/b2"]})
    assert [q for _, q in on.seen] == [None, None, tiers.PRIORITY_QUEUE, tiers.PRIORITY_QUEUE, tiers.PRIORITY_QUEUE]


def test_self_hosting_never_uses_the_priority_queue(s3app):
    off = s3app()
    off.post("/api/v1/auth/register", json={"email": "q@example.com", "password": "correct horse battery"})
    uid = off.get("/api/v1/me").json()["id"]
    off.portal.call(off.app.state.db.set_tier, uid, "pro", None)
    n = len(off.seen)
    off.post("/api/v1/jobs", json={"url": "https://example.com/x"})
    assert off.seen[n][1] is None
