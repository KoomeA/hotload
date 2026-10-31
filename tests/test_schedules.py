"""Scheduled recording: the API, the 30-second release tick, retention of what it records, and the email."""

import asyncio
import time

import boto3
import pytest
from fakeredis import FakeAsyncRedis
from fastapi.testclient import TestClient
from moto import mock_aws

from hotload import engine, scheduler, storage, worker
from hotload.api.app import create_app
from hotload.config import Settings
from hotload.db import Database
from hotload.jobs import JobStore
from hotload.proxies import ProxyPool

PW = "correct horse battery"
HOUR = 3600


# --- the API ---------------------------------------------------------------------------------------------------------

@pytest.fixture
def make(tmp_path, monkeypatch):
    monkeypatch.setattr("socket.getaddrinfo", lambda host, port, *a, **k: [(2, 1, 6, "", ("93.184.216.34", port))])
    stack = []

    def build(**o):
        async def enqueue(job_id, _q=None):
            pass

        c = TestClient(create_app(Settings(data_dir=tmp_path, rate_limit_per_minute=1000, secret_key="s", **o),
                                  redis=FakeAsyncRedis(decode_responses=True), enqueue=enqueue))
        c.__enter__()
        stack.append(c)
        return c

    yield build
    for c in stack:
        c.__exit__(None, None, None)


@pytest.fixture
def c(make):
    return make()


def signup(c, email="rec@example.com"):
    r = c.post("/api/v1/auth/register", json={"email": email, "password": PW})
    assert r.status_code == 201
    return r.json()["id"]


def when(hours=2):
    return time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() + hours * HOUR))


def sched(c, url="https://example.com/live", **kw):
    return c.post("/api/v1/schedules", json={"url": url, "start_at": when(), "minutes": 30, **kw})


def test_scheduling_needs_an_account(c):
    assert sched(c).status_code == 401
    assert c.get("/api/v1/schedules").status_code == 401


def test_a_schedule_is_created_listed_and_cancelled(c):
    signup(c)
    r = sched(c)
    assert r.status_code == 201
    row = r.json()
    assert row["state"] == "waiting" and row["minutes"] == 30 and row["preset"] == "best_mp4"
    assert abs(row["start_at"] - (time.time() + 2 * HOUR)) < 5 and "job" not in row
    assert [x["id"] for x in c.get("/api/v1/schedules").json()] == [row["id"]]
    assert c.delete(f"/api/v1/schedules/{row['id']}").status_code == 204
    assert c.get("/api/v1/schedules").json()[0]["state"] == "cancelled"
    assert c.delete(f"/api/v1/schedules/{row['id']}").status_code == 204      # a finished row can be cleared
    assert c.get("/api/v1/schedules").json() == []


def test_schedules_belong_to_their_owner(c):
    signup(c, "a@example.com")
    sid = sched(c).json()["id"]
    c.post("/api/v1/auth/logout")
    signup(c, "b@example.com")
    assert c.get("/api/v1/schedules").json() == []
    assert c.delete(f"/api/v1/schedules/{sid}").status_code == 404
    c.post("/api/v1/auth/logout")
    c.post("/api/v1/auth/login", json={"email": "a@example.com", "password": PW})
    assert len(c.get("/api/v1/schedules").json()) == 1                        # untouched


@pytest.mark.parametrize("when_, status", [
    ("2020-01-01T00:00:00+00:00", 422),                                        # the past
    (None, 422),                                                               # no timezone (below)
])
def test_bad_start_times_are_refused(c, when_, status):
    signup(c)
    if when_ is None:
        r = c.post("/api/v1/schedules", json={"url": "https://example.com/l", "start_at": "2030-01-01T00:00:00", "minutes": 5})
        assert r.status_code == 422 and "timezone" in r.json()["detail"]
    else:
        assert c.post("/api/v1/schedules", json={"url": "https://example.com/l", "start_at": when_, "minutes": 5}).status_code == status
    far = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() + 30 * 86400))
    r = c.post("/api/v1/schedules", json={"url": "https://example.com/l", "start_at": far, "minutes": 5})
    assert r.status_code == 422 and "14 days" in r.json()["detail"]
    soon = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() + 1))
    assert c.post("/api/v1/schedules", json={"url": "https://example.com/l", "start_at": soon, "minutes": 5}).status_code == 422


def test_the_same_rules_as_recording_now_apply(c):
    signup(c)
    assert sched(c, url="http://127.0.0.1:8000/x").status_code == 400                      # internal address
    assert sched(c, preset="nonsense").status_code == 400
    assert c.post("/api/v1/schedules", json={"url": "https://example.com/l", "start_at": when(), "minutes": 90}).status_code == 400  # > 60
    assert c.post("/api/v1/schedules", json={"url": "https://example.com/l", "start_at": when(), "minutes": 0}).status_code == 422
    adult = "https://www.pornhub.com/view_video.php?viewkey=abc"
    assert sched(c, url=adult).status_code == 451                                          # age gate
    ok = sched(c, url=adult, age_confirmed=True)
    assert ok.status_code == 201 and c.get("/api/v1/schedules").json()[0]["id"] == ok.json()["id"]
    c.portal.call(c.app.state.db.add_block, "host", "blocked.example", "takedown", "admin1")
    c.app.state.rules.invalidate()
    assert sched(c, url="https://blocked.example/live").status_code == 451


def test_caps_per_account_and_server_wide(make):
    c = make(max_scheduled_per_user=2, max_scheduled_total=3)
    signup(c, "a@example.com")
    first, _second = sched(c).json(), sched(c)
    r = sched(c)
    assert r.status_code == 409 and "2 recordings waiting" in r.json()["detail"]
    assert c.delete(f"/api/v1/schedules/{first['id']}").status_code == 204             # cancelling frees a place
    assert sched(c).status_code == 201
    c.post("/api/v1/auth/logout")
    signup(c, "b@example.com")
    assert sched(c).status_code == 201                                                 # 3rd on the server...
    assert sched(c).status_code == 503                                                 # ...and no 4th


def test_hosted_plans_decide_who_may_schedule_and_for_how_long(make):
    c = make(quotas_enabled=True, egress_filtered=True)
    uid = signup(c)
    assert sched(c).status_code == 403                                                 # free: live recording is not on the plan
    c.portal.call(c.app.state.db.set_tier, uid, "premium", time.time() + 30 * 86400)
    assert sched(c).status_code == 201
    assert c.post("/api/v1/schedules", json={"url": "https://example.com/l", "start_at": when(), "minutes": 45}).status_code == 400  # > 30
    for _ in range(2):
        assert sched(c).status_code == 201
    assert sched(c).status_code == 409                                                 # premium: 3 waiting at most
    c.portal.call(c.app.state.db.set_tier, uid, "pro", time.time() + 30 * 86400)
    assert sched(c).status_code == 201                                                 # pro allows more


def test_hosted_scheduling_needs_the_egress_filter_like_live_recording(make):
    c = make(quotas_enabled=True)
    uid = signup(c)
    c.portal.call(c.app.state.db.set_tier, uid, "pro", time.time() + 30 * 86400)
    assert sched(c).status_code == 503


def test_can_be_switched_off(make):
    c = make(scheduled_enabled=False)
    signup(c)
    assert sched(c).status_code == 503


def test_the_page_can_announce_its_own_start_time(c, monkeypatch):
    signup(c)
    soon = time.time() + 3 * HOUR
    body = {"url": "https://example.com/premiere", "minutes": 20}
    monkeypatch.setattr(engine, "extract", lambda *a, **k: {"id": "x", "site": "Generic", "live_status": "is_upcoming",
                                                            "is_live": False, "release_timestamp": soon})
    r = c.post("/api/v1/schedules", json=body)
    assert r.status_code == 201 and abs(r.json()["start_at"] - soon) < 1
    monkeypatch.setattr(engine, "extract", lambda *a, **k: {"id": "x", "site": "G", "live_status": "is_live", "is_live": True})
    assert c.post("/api/v1/schedules", json=body).status_code == 409                   # already live: record it now instead
    monkeypatch.setattr(engine, "extract", lambda *a, **k: {"id": "x", "site": "G", "live_status": "not_live", "is_live": False})
    assert c.post("/api/v1/schedules", json=body).status_code == 422
    monkeypatch.setattr(engine, "extract", lambda *a, **k: {"id": "x", "site": "G", "live_status": "is_upcoming", "is_live": False,
                                                            "release_timestamp": None})
    assert "choose one" in c.post("/api/v1/schedules", json=body).json()["detail"]

    def boom(*a, **k):
        raise engine.EngineError("nope")

    monkeypatch.setattr(engine, "extract", boom)
    assert c.post("/api/v1/schedules", json=body).status_code == 422


def test_deleting_an_account_removes_its_waiting_schedules(c):
    uid = signup(c)
    sched(c)
    c.portal.call(c.app.state.db.delete_user, uid)
    assert c.portal.call(c.app.state.db.counts)["scheduled_waiting"] == 0


# --- the release tick --------------------------------------------------------------------------------------------------

class FakeArq:
    def __init__(self):
        self.enqueued: list[tuple] = []

    async def enqueue_job(self, name, *args, **kwargs):
        self.enqueued.append((name, args, kwargs))


class Mailer:
    def __init__(self):
        self.sent: list[tuple] = []

    async def send(self, to, subject, body):
        self.sent.append((to, subject, body))


class Pool:
    def select(self, url, exclude=()):
        return None

    def report(self, *a):
        pass


@pytest.fixture
async def tick(tmp_path, monkeypatch):
    """A worker context with a real database and fake redis; `probe` decides which URLs are live."""
    monkeypatch.setattr("socket.getaddrinfo", lambda host, port, *a, **k: [(2, 1, 6, "", ("93.184.216.34", port))])
    redis = FakeAsyncRedis(decode_responses=True)
    settings = Settings(data_dir=tmp_path, public_url="https://hotload.example")
    db = Database(settings)
    await db.migrate()
    ctx = {"db": db, "store": JobStore(redis, 7200), "redis_client": redis, "redis": FakeArq(), "mailer": Mailer(),
           "pool": Pool(), "settings": settings}
    ctx["live"] = set()
    ctx["probes"] = []

    def probe(url, s, pool):
        ctx["probes"].append(url)
        return url in ctx["live"]

    ctx["probe"] = probe
    ctx["run"] = lambda now=None: scheduler.release_due(ctx, now, probe)
    ctx["user"] = await db.add_user(email="rec@example.com", password_hash="x")
    yield ctx
    await db.dispose()


async def add(ctx, url="https://example.com/live", start_in=-10, minutes=30, user=None, **kw):
    u = user or ctx["user"]
    row = await ctx["db"].add_schedule(99, 999, user_id=u.id, url=url, start_at=time.time() + start_in, minutes=minutes,
                                       preset="best_mp4", **kw)
    assert not isinstance(row, str)
    return row


async def state(ctx, row):
    return await ctx["db"].get_schedule(row.id)


async def test_a_schedule_that_is_not_due_is_left_alone(tick):
    row = await add(tick, start_in=600)
    stats = await tick["run"]()
    assert stats["probed"] == 0 and tick["probes"] == [] and (await state(tick, row)).state == "waiting"


async def test_a_due_stream_that_is_not_live_keeps_waiting_and_is_checked_every_30_seconds(tick):
    row = await add(tick)
    now = time.time()
    assert (await tick["run"](now))["waiting"] == 1 and len(tick["probes"]) == 1
    assert (await state(tick, row)).next_check == pytest.approx(now + 30, abs=1)
    assert (await tick["run"](now + 10))["probed"] == 0                         # not yet time to look again
    assert len(tick["probes"]) == 1
    assert (await tick["run"](now + 31))["probed"] == 1


async def test_a_live_stream_becomes_a_queued_recording_job(tick):
    row = await add(tick, minutes=45, adult=True, age_confirmed=True)
    tick["live"].add(row.url)
    stats = await tick["run"]()
    assert stats["queued"] == 1
    done = await state(tick, row)
    assert done.state == "queued" and done.job_id
    job = await tick["store"].get(done.job_id)
    assert job.live_seconds == 45 * 60 and job.user_id == tick["user"].id and job.preset == "best_mp4"
    assert job.adult and job.age_confirmed and job.retention == 24 * HOUR
    assert job.notify == {"kind": "email", "user_id": tick["user"].id}
    assert tick["redis"].enqueued == [("run_download", (job.id,), {})]
    assert [h["id"] for h in await tick["db"].list_history(tick["user"].id)] == [job.id]
    assert await tick["redis_client"].zscore(scheduler.RUNNING, job.id) > time.time()
    assert (await tick["run"]())["queued"] == 0                                 # released exactly once


async def test_a_stream_that_never_starts_is_marked_missed_and_the_owner_is_told(tick):
    row = await add(tick, start_in=-31 * 60)
    assert (await tick["run"]())["missed"] == 1
    gone = await state(tick, row)
    assert gone.state == "missed" and "30 minutes" in gone.message
    [(to, subject, body)] = tick["mailer"].sent
    assert to == "rec@example.com" and "did not happen" in subject and "#recordings" in body
    assert (await state(tick, await add(tick, start_in=-29 * 60))).state == "waiting"  # inside the grace period


async def test_many_waiting_for_one_page_cost_one_check(tick):
    other = await tick["db"].add_user(email="other@example.com", password_hash="x")
    a, b = await add(tick), await add(tick, user=other)
    tick["live"].add(a.url)
    stats = await tick["run"]()
    assert stats["probed"] == 1 and stats["queued"] == 2 and len(tick["probes"]) == 1
    assert (await state(tick, a)).job_id != (await state(tick, b)).job_id


async def test_each_tick_checks_a_limited_number_of_pages(tick):
    rows = [await add(tick, url=f"https://example.com/{i}", start_in=-100 + i) for i in range(12)]
    stats = await tick["run"]()
    assert stats["probed"] == 10 and len(tick["probes"]) == 10
    assert sorted(set(tick["probes"])) == sorted(r.url for r in rows[:10])            # oldest planned starts first
    # the other two were not looked at, so they are still due: the next tick takes them
    stats = await tick["run"](time.time() + 31)
    assert {"https://example.com/10", "https://example.com/11"} <= set(tick["probes"])


async def test_scheduled_recordings_cannot_take_every_worker(tick):
    tick["settings"] = tick["settings"].model_copy(update={"max_scheduled_running": 1})
    a, b = await add(tick, url="https://example.com/a"), await add(tick, url="https://example.com/b")
    tick["live"].update({a.url, b.url})
    stats = await tick["run"]()
    assert stats["queued"] == 1 and stats["deferred"] == 1
    waiting = await state(tick, b)
    assert waiting.state == "waiting"                                           # not lost: it waits for a free place
    job_id = (await state(tick, a)).job_id
    await tick["redis_client"].zrem(scheduler.RUNNING, job_id)                  # the first recording finished
    assert (await tick["run"]())["queued"] == 1


async def test_a_crashed_worker_does_not_hold_its_place_forever(tick):
    tick["settings"] = tick["settings"].model_copy(update={"max_scheduled_running": 1})
    await tick["redis_client"].zadd(scheduler.RUNNING, {"ghost-job": time.time() - 5})   # its "should be done by" has passed
    row = await add(tick)
    tick["live"].add(row.url)
    assert (await tick["run"]())["queued"] == 1


async def test_the_plan_is_checked_again_at_release_time(tick):
    tick["settings"] = tick["settings"].model_copy(update={"quotas_enabled": True, "egress_filtered": True})
    row = await add(tick)
    tick["live"].add(row.url)
    stats = await tick["run"]()                                                 # the user is on the free plan now
    assert stats["failed"] == 1
    gone = await state(tick, row)
    assert gone.state == "failed" and "no longer includes" in gone.message
    assert "did not happen" in tick["mailer"].sent[0][1]


async def test_a_lower_plan_shortens_the_recording_and_the_daily_allowance_applies(tick):
    tick["settings"] = tick["settings"].model_copy(update={"quotas_enabled": True, "egress_filtered": True})
    await tick["db"].set_tier(tick["user"].id, "premium", time.time() + 30 * 86400)       # premium: 30 minute cap
    row = await add(tick, minutes=60)
    tick["live"].add(row.url)
    await tick["run"]()
    assert (await tick["store"].get((await state(tick, row)).job_id)).live_seconds == 30 * 60
    from hotload import tiers

    for _ in range(100):                                                         # use up the whole allowance
        if not await tiers.consume(tick["redis_client"], tiers.subject(tick["user"], ""), tiers.TIERS["premium"]):
            break
    again = await add(tick, url="https://example.com/again")
    tick["live"].add(again.url)
    assert (await tick["run"]())["failed"] == 1
    assert "Daily limit" in (await state(tick, again)).message


async def test_a_link_that_became_unsafe_or_blocked_is_not_recorded(tick, monkeypatch):
    row = await add(tick, url="https://blocked.example/live")
    await tick["db"].add_block("host", "blocked.example", "takedown", "admin1")
    tick["live"].add(row.url)
    assert (await tick["run"]())["failed"] == 1 and "not available" in (await state(tick, row)).message
    monkeypatch.setattr("socket.getaddrinfo", lambda host, port, *a, **k: [(2, 1, 6, "", ("10.0.0.5", port))])  # DNS now points inside
    row2 = await add(tick, url="https://moved.example/live")
    tick["live"].add(row2.url)
    assert (await tick["run"]())["failed"] == 1 and "no longer allowed" in (await state(tick, row2)).message
    assert tick["redis"].enqueued == []


async def test_a_cancel_that_races_the_release_wins(tick):
    row = await add(tick)
    stale = await state(tick, row)                                               # the tick read the row...
    assert await tick["db"].cancel_schedule(tick["user"].id, row.id) == "cancelled"  # ...then the user cancelled
    outcome = await scheduler.release(tick, tick["settings"], stale, 0)
    assert outcome == "deferred" and tick["redis"].enqueued == []
    assert (await state(tick, row)).state == "cancelled"


async def test_two_workers_releasing_the_same_schedule_make_one_recording(tick):
    row = await add(tick)
    stale = await state(tick, row)
    outcomes = await asyncio.gather(scheduler.release(tick, tick["settings"], stale, 0), scheduler.release(tick, tick["settings"], stale, 0))
    assert sorted(outcomes) == ["deferred", "queued"] and len(tick["redis"].enqueued) == 1


async def test_a_deleted_account_fails_its_schedule_cleanly(tick):
    ghost = await tick["db"].add_user(email="ghost@example.com", password_hash="x")
    row = await add(tick, user=ghost)
    await tick["db"].delete_user(ghost.id)
    # delete_user removes waiting rows, so recreate the orphan the way a race could leave it
    orphan = await add(tick, user=tick["user"])
    await tick["db"].delete_user(tick["user"].id)
    assert await tick["db"].get_schedule(row.id) is None and await tick["db"].get_schedule(orphan.id) is None


async def test_finished_recordings_email_a_link_to_the_account_page_never_the_tokened_download(tick):
    store = tick["store"]
    job = await store.create(url="https://example.com/live", preset="best_mp4", live_seconds=60, user_id=tick["user"].id,
                             notify={"kind": "email", "user_id": tick["user"].id})
    job.state, job.title = "done", "Evening stream"
    await scheduler.notify_owner(tick, job)
    [(to, subject, body)] = tick["mailer"].sent
    assert to == "rec@example.com" and "ready" in subject and "Evening stream" in body and "24 hours" in body
    assert job.token not in body and "/file?token" not in body and "https://hotload.example/account/#recordings" in body
    tick["mailer"].sent.clear()
    job.state, job.message = "error", "The stream ended early"
    await scheduler.notify_owner(tick, job)
    assert "did not happen" in tick["mailer"].sent[0][1] and "ended early" in tick["mailer"].sent[0][2]


async def test_without_a_mailer_nothing_is_sent_and_nothing_breaks(tick):
    tick["mailer"] = None
    row = await add(tick, start_in=-31 * 60)
    assert (await tick["run"]())["missed"] == 1 and (await state(tick, row)).state == "missed"


def test_the_worker_runs_the_tick_every_30_seconds():
    names = {j.name: j for j in worker.WorkerSettings.cron_jobs}
    assert "cron:release_schedules" in names and names["cron:release_schedules"].second == {0, 30}


def test_probe_live_treats_every_failure_as_not_yet(monkeypatch):
    s = Settings()
    monkeypatch.setattr(engine, "extract", lambda *a, **k: {"is_live": True})
    assert scheduler.probe_live("https://example.com/l", s, ProxyPool.from_settings(s)) is True
    monkeypatch.setattr(engine, "extract", lambda *a, **k: {"live_status": "is_live"})
    assert scheduler.probe_live("https://example.com/l", s, ProxyPool.from_settings(s)) is True
    monkeypatch.setattr(engine, "extract", lambda *a, **k: {"live_status": "is_upcoming", "is_live": False})
    assert scheduler.probe_live("https://example.com/l", s, ProxyPool.from_settings(s)) is False

    def offline(*a, **k):
        raise engine.EngineError("The channel is offline")

    monkeypatch.setattr(engine, "extract", offline)
    assert scheduler.probe_live("https://example.com/l", s, ProxyPool.from_settings(s)) is False


# --- keeping the result longer than an hour --------------------------------------------------------------------------------

async def test_a_job_with_retention_is_kept_that_long_in_redis():
    redis = FakeAsyncRedis(decode_responses=True)
    store = JobStore(redis, 7200)
    plain = await store.create(url="https://example.com/a", preset="mp3")
    kept = await store.create(url="https://example.com/b", preset="mp3", retention=24 * HOUR)
    assert 7000 < await redis.ttl(store.key(plain.id)) <= 7200
    assert 24 * HOUR - 100 < await redis.ttl(store.key(kept.id)) <= 24 * HOUR
    assert "retention" not in kept.public()


def test_local_files_with_a_keep_marker_outlive_the_normal_ttl(tmp_path):
    for name in ("plain", "kept"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "f.mp4").write_bytes(b"x")
    storage.mark_keep(tmp_path, "kept", time.time() + 24 * HOUR)
    later = time.time() + 2 * HOUR                                                # past the 1 h TTL
    assert storage.cleanup_expired(tmp_path, 3600, now=later) == 1
    assert (tmp_path / "kept").exists() and not (tmp_path / "plain").exists()
    assert storage.cleanup_expired(tmp_path, 3600, now=time.time() + 25 * HOUR) == 1 and not (tmp_path / "kept").exists()
    (tmp_path / "bad").mkdir()
    (tmp_path / "bad" / storage.KEEP_MARKER).write_text("not a number")
    assert storage.cleanup_expired(tmp_path, 3600, now=time.time() + 2 * HOUR) == 1  # a broken marker falls back to the normal TTL


def test_s3_retained_objects_live_under_keep_and_use_the_longer_ttl(tmp_path):
    with mock_aws():
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="sched-bucket")
        remote = storage.S3Storage(Settings(data_dir=tmp_path, storage_backend="s3", s3_bucket="sched-bucket", s3_region="us-east-1",
                                            s3_access_key_id="k", s3_secret_access_key="s", s3_prefix="hl/"))
        f = tmp_path / "rec.mp4"
        f.write_bytes(b"x")
        plain, kept = remote.put("plain1", f), remote.put("kept1", f, retain=True)
        assert plain == "hl/plain1/rec.mp4" and kept == "hl/keep/kept1/rec.mp4"
        two_hours = time.time() + 2 * HOUR
        assert remote.cleanup_expired(3600, now=two_hours, keep_ttl_seconds=24 * HOUR) == 1
        assert not remote.exists(plain) and remote.exists(kept)
        assert remote.cleanup_expired(3600, now=time.time() + 25 * HOUR, keep_ttl_seconds=24 * HOUR) == 1
        assert not remote.exists(kept)


async def test_a_finished_scheduled_recording_is_kept_marked_done_freed_and_announced(tmp_path, monkeypatch):
    """The worker side: keep marker beside the file, the running-count slot released, and the owner emailed."""
    settings = Settings(data_dir=tmp_path, public_url="https://hotload.example")
    monkeypatch.setattr(worker, "get_settings", lambda: settings)
    monkeypatch.setattr(scheduler, "get_settings", lambda: settings)
    monkeypatch.setattr(worker, "_pool", ProxyPool.from_settings(settings))

    def fake_download(url, s, workdir, preset, proxy=None, **kw):
        assert kw["live_seconds"] == 600                                  # the recording length reached the engine
        workdir.mkdir(parents=True, exist_ok=True)
        out = workdir / "rec.mp4"
        out.write_bytes(b"x")
        return engine.DownloadResult(out, "Evening stream", "Generic")

    monkeypatch.setattr(engine, "download", fake_download)
    redis = FakeAsyncRedis(decode_responses=True)
    db = Database(settings)
    await db.migrate()
    user = await db.add_user(email="owner@example.com", password_hash="x")
    store, mailer = JobStore(redis, 7200), Mailer()
    job = await store.create(url="https://example.com/live", preset="best_mp4", live_seconds=600, user_id=user.id,
                             retention=24 * HOUR, notify={"kind": "email", "user_id": user.id})
    await redis.zadd(scheduler.RUNNING, {job.id: time.time() + 3600})
    ctx = {"store": store, "db": db, "mailer": mailer, "redis": FakeArq()}
    await worker.run_download(ctx, job.id)
    done = await store.get(job.id)
    assert done.state == "done"
    marker = tmp_path / "out" / job.id / storage.KEEP_MARKER
    assert abs(float(marker.read_text()) - (time.time() + 24 * HOUR)) < 30       # kept a day, not an hour
    assert await redis.zscore(scheduler.RUNNING, job.id) is None                 # its place in the running count is free again
    assert 24 * HOUR - 100 < await redis.ttl(store.key(job.id)) <= 24 * HOUR      # and so is its record
    [(to, subject, body)] = mailer.sent
    assert to == "owner@example.com" and "ready" in subject and "Evening stream" in body and done.token not in body
    await db.dispose()
