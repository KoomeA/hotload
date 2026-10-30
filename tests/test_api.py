from hotload import engine


def test_health_and_presets(client):
    assert client.get("/healthz").json() == {"ok": True}
    keys = {p["key"] for p in client.get("/api/v1/presets").json()}
    assert {"best_mp4", "720p", "mp3"} <= keys


def test_extract_flags_adult(client, monkeypatch):
    monkeypatch.setattr(engine, "extract", lambda url, s: {"title": "t", "age_limit": 18})
    r = client.post("/api/v1/extract", json={"url": "https://example.com/v"})
    assert r.status_code == 200 and r.json()["age_gate_required"] is True


def test_extract_engine_error(client, monkeypatch):
    def boom(url, s):
        raise engine.EngineError("Unsupported URL")

    monkeypatch.setattr(engine, "extract", boom)
    r = client.post("/api/v1/extract", json={"url": "https://example.com/v"})
    assert r.status_code == 422 and r.json()["detail"] == "Unsupported URL"


def test_rejects_ssrf(client):
    r = client.post("/api/v1/jobs", json={"url": "http://127.0.0.1/admin"})
    assert r.status_code == 400


def test_job_lifecycle(client, enqueued):
    r = client.post("/api/v1/jobs", json={"url": "https://example.com/v", "preset": "720p"})
    assert r.status_code == 202
    job = r.json()
    assert enqueued == [job["id"]] and "token" not in job and "url" not in job
    assert client.get(f"/api/v1/jobs/{job['id']}").json()["state"] == "queued"
    assert client.get("/api/v1/jobs/missing").status_code == 404


def test_adult_host_requires_age_confirmation(client, enqueued):
    body = {"url": "https://www.pornhub.com/view_video.php?viewkey=x"}
    assert client.post("/api/v1/jobs", json=body).status_code == 451
    assert enqueued == []
    ok = client.post("/api/v1/jobs", json={**body, "age_confirmed": True})
    assert ok.status_code == 202 and ok.json()["adult"] is True


def test_validation(client):
    assert client.post("/api/v1/jobs", json={"url": "https://example.com/v", "preset": "nope"}).status_code == 400
    bad = {"url": "https://example.com/v", "start": 10, "end": 5}
    assert client.post("/api/v1/jobs", json=bad).status_code == 422


def test_rate_limit(client):
    codes = [client.get("/api/v1/jobs/x") for _ in range(3)]  # not limited (no guard on GET)
    assert all(c.status_code == 404 for c in codes)
    statuses = [client.post("/api/v1/jobs", json={"url": "https://example.com/v"}).status_code for _ in range(7)]
    assert statuses[:5] == [202] * 5 and statuses[5:] == [429, 429]


def test_file_download_requires_token(client, settings):
    r = client.post("/api/v1/jobs", json={"url": "https://example.com/v"}).json()
    f = settings.output_dir / r["id"]
    f.mkdir(parents=True)
    (f / "a.mp4").write_bytes(b"data")
    store = client.app.state.store

    async def finish():
        job = await store.get(r["id"])
        job.state, job.path, job.filename = "done", str(f / "a.mp4"), "a.mp4"
        await store.save(job)
        return job.token

    # TestClient runs the app on its own loop; use the portal to reach the same loop/redis.
    token = client.portal.call(finish)
    assert client.get(f"/api/v1/jobs/{r['id']}/file?token=wrong").status_code == 403
    ok = client.get(f"/api/v1/jobs/{r['id']}/file?token={token}")
    assert ok.status_code == 200 and ok.content == b"data"
