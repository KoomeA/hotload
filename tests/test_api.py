import pytest

from hotload import engine


def test_health_and_presets(client):
    assert client.get("/healthz").json() == {"ok": True}
    keys = {p["key"] for p in client.get("/api/v1/presets").json()}
    assert {"best_mp4", "720p", "mp3"} <= keys


def test_extract_flags_adult(client, monkeypatch):
    monkeypatch.setattr(engine, "extract", lambda url, s, proxy=None: {"title": "t", "age_limit": 18})
    r = client.post("/api/v1/extract", json={"url": "https://example.com/v"})
    assert r.status_code == 200 and r.json()["age_gate_required"] is True


def test_extract_engine_error(client, monkeypatch):
    def boom(url, s, proxy=None):
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


def test_job_accepts_subtitle_and_audio_options(client, enqueued):
    body = {"url": "https://example.com/v", "preset": "720p", "subtitle_langs": ["en", "fr"],
            "subtitle_format": "vtt", "subtitle_mode": "embed", "audio_lang": "es"}
    r = client.post("/api/v1/jobs", json=body)
    assert r.status_code == 202
    j = r.json()
    assert j["subtitle_langs"] == ["en", "fr"] and j["audio_lang"] == "es" and j["subtitle_format"] == "vtt"


@pytest.mark.parametrize("patch", [
    {"audio_lang": "en]/best"}, {"subtitle_langs": ["en;x"]}, {"subtitle_format": "exe"},
    {"subtitle_mode": "zip"}, {"subtitle_langs": ["en"] * 11},
])
def test_job_rejects_bad_language_or_format(client, patch):
    assert client.post("/api/v1/jobs", json={"url": "https://example.com/v", **patch}).status_code == 422


def test_subtitles_only_requires_language(client):
    assert client.post("/api/v1/jobs", json={"url": "https://example.com/v", "preset": "subtitles"}).status_code == 400
    ok = client.post("/api/v1/jobs", json={"url": "https://example.com/v", "preset": "subtitles", "subtitle_langs": ["en"]})
    assert ok.status_code == 202


def test_playlist_endpoint_filters_unsafe_entries(client, monkeypatch):
    def fake(url, s, proxy=None, limit=50):
        return {"playlist": True, "title": "P", "count": 3, "entries": [
            {"url": "https://example.com/1", "title": "a", "duration": 3},
            {"url": "http://127.0.0.1/secret", "title": "evil", "duration": 1},
            {"url": "https://example.com/3", "title": "c", "duration": 5}]}

    monkeypatch.setattr(engine, "extract_playlist", fake)
    # entry 2 resolves to a private address; make the resolver treat 127.0.0.1 literally (as production does)
    r = client.post("/api/v1/playlist", json={"url": "https://example.com/list"})
    assert r.status_code == 200
    assert [e["title"] for e in r.json()["entries"]] == ["a", "c"] and r.json()["count"] == 2


def test_batch_create_status_and_zip(client, enqueued, settings):
    r = client.post("/api/v1/batches", json={"urls": ["https://example.com/a", "https://example.com/b",
                                                      "https://example.com/a"], "preset": "mp3"})
    assert r.status_code == 202
    b = r.json()
    assert b["total"] == 2 and len(enqueued) == 2 and b["zip_url"] is None and not b["finished"]  # de-duplicated
    assert client.get(f"/api/v1/batches/{b['id']}/zip?token=x").status_code in (403, 404)
    assert client.get("/api/v1/batches/nope").status_code == 404

    store = client.app.state.store
    token = client.app.state.batches

    async def finish():
        for n, jid in enumerate(enqueued):
            f = settings.output_dir / jid
            f.mkdir(parents=True, exist_ok=True)
            (f / f"v{n}.mp3").write_bytes(b"audio%d" % n)
            job = await store.get(jid)
            job.state, job.path, job.filename = "done", str(f / f"v{n}.mp3"), f"v{n}.mp3"
            await store.save(job)
        return (await token.get(b["id"])).token

    tok = client.portal.call(finish)
    view = client.get(f"/api/v1/batches/{b['id']}").json()
    assert view["finished"] and view["done"] == 2 and view["zip_url"].endswith(tok)
    assert client.get(f"/api/v1/batches/{b['id']}/zip?token=wrong").status_code == 403
    z = client.get(view["zip_url"])
    assert z.status_code == 200 and z.headers["content-type"] == "application/zip"
    import io
    import zipfile
    names = zipfile.ZipFile(io.BytesIO(z.content)).namelist()
    assert names == ["01 - v0.mp3", "02 - v1.mp3"]


def test_batch_limits_and_validation(client, settings):
    too_many = [f"https://example.com/{i}" for i in range(settings.max_batch_size + 1)]
    assert client.post("/api/v1/batches", json={"urls": too_many}).status_code == 400
    assert client.post("/api/v1/batches", json={"urls": []}).status_code == 422
    bad = client.post("/api/v1/batches", json={"urls": ["https://example.com/a", "http://10.0.0.1/x"]})
    assert bad.status_code == 400
    adult = client.post("/api/v1/batches", json={"urls": ["https://www.pornhub.com/view_video.php?viewkey=x"]})
    assert adult.status_code == 451


def test_batch_counts_against_rate_limit(client):
    urls = [f"https://example.com/{i}" for i in range(4)]
    assert client.post("/api/v1/batches", json={"urls": urls}).status_code == 202  # cost 4 of 5
    assert client.post("/api/v1/batches", json={"urls": urls}).status_code == 429
