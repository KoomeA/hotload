"""Discord bot: signature checking, the slash commands, private replies, and the follow-up when a job finishes."""

import json
import re
import time

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fakeredis import FakeAsyncRedis
from fastapi.testclient import TestClient

from hotload import admin, notify
from hotload.api import discord
from hotload.api.app import create_app
from hotload.config import Settings
from hotload.jobs import JobStore

KEY = Ed25519PrivateKey.generate()
PUB = KEY.public_key().public_bytes_raw().hex()
OTHER = Ed25519PrivateKey.generate()
APP_ID = "1122334455667788"
TOKEN = "aW50ZXJhY3Rpb24tdG9rZW4.SECRET_part-1"
CFG = {"discord_public_key": PUB, "discord_application_id": APP_ID, "public_url": "https://hotload.example",
       "rate_limit_per_minute": 1000, "secret_key": "s"}
URL = "/api/v1/bots/discord"


class Discord:
    """Records what the bot sends to Discord's webhook API."""

    def __init__(self):
        self.calls: list[httpx.Request] = []
        self.statuses: list[int] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        status = self.statuses.pop(0) if self.statuses else 200
        return httpx.Response(status, json={"retry_after": 0.01} if status == 429 else {})

    def bodies(self):
        return [json.loads(c.content) for c in self.calls]

    def texts(self):
        return [b["content"] for b in self.bodies()]


@pytest.fixture
def dc():
    return Discord()


@pytest.fixture
def make(tmp_path, monkeypatch, dc):
    monkeypatch.setattr("socket.getaddrinfo", lambda host, port, *a, **k: [(2, 1, 6, "", ("93.184.216.34", port))])
    stack, queued = [], []

    def build(**o):
        async def enqueue(job_id, _q=None):
            queued.append(job_id)

        http = httpx.AsyncClient(transport=httpx.MockTransport(dc.handler))
        c = TestClient(create_app(Settings(data_dir=tmp_path, **{**CFG, **o}), redis=FakeAsyncRedis(decode_responses=True),
                                  enqueue=enqueue, http_client=http))
        c.__enter__()
        c.queued = queued
        stack.append(c)
        return c

    yield build
    for c in stack:
        c.__exit__(None, None, None)


@pytest.fixture
def c(make):
    return make()


def post(c, payload, key=KEY, ts=None, raw=None):
    body = raw if raw is not None else json.dumps(payload).encode()
    ts = str(int(time.time())) if ts is None else ts
    headers = {"x-signature-ed25519": key.sign(ts.encode() + body).hex(), "x-signature-timestamp": ts, "content-type": "application/json"}
    return c.post(URL, content=body, headers=headers)


def command(name, options=None, user="42", token=TOKEN, dm=False):
    who = {"user": {"id": user}} if dm else {"member": {"user": {"id": user}}}
    data = {"name": name, "options": [{"name": k, "type": 3, "value": v} for k, v in (options or {}).items()]}
    return {"type": 2, "id": "1", "application_id": APP_ID, "token": token, "data": data, **who}


def job_of(c, job_id):
    return c.portal.call(c.app.state.store.get, job_id)


# --- the signature is the only way in ------------------------------------------------------------------------------

def test_a_signed_ping_is_answered_with_a_pong(c):
    r = post(c, {"type": 1})
    assert r.status_code == 200 and r.json() == {"type": 1}


def test_unsigned_wrongly_signed_and_tampered_requests_are_refused(c, dc):
    body = json.dumps(command("download", {"link": "https://example.com/v"})).encode()
    ts = str(int(time.time()))
    good = {"x-signature-ed25519": KEY.sign(ts.encode() + body).hex(), "x-signature-timestamp": ts}
    assert c.post(URL, content=body).status_code == 401                                            # no headers
    assert post(c, None, key=OTHER, raw=body).status_code == 401                                   # someone else's key
    assert c.post(URL, content=body + b" ", headers=good).status_code == 401                       # body changed after signing
    assert c.post(URL, content=body, headers={**good, "x-signature-timestamp": str(int(ts) + 1)}).status_code == 401  # timestamp changed
    for bad in ("zz", "", "ab" * 10, "0" * 128):
        assert c.post(URL, content=body, headers={**good, "x-signature-ed25519": bad}).status_code == 401
    assert c.post(URL, content=body, headers={**good, "x-signature-timestamp": "yesterday"}).status_code == 401
    assert c.queued == [] and dc.calls == []
    assert c.post(URL, content=body, headers=good).status_code == 200                              # positive control


def test_a_correctly_signed_but_stale_request_is_refused(c):
    old = str(int(time.time()) - 3600)
    assert post(c, {"type": 1}, ts=old).status_code == 401
    assert post(c, {"type": 1}, ts=str(int(time.time()) + 3600)).status_code == 401
    assert post(c, {"type": 1}, ts=str(int(time.time()) - 60)).status_code == 200


def test_disabled_without_configuration(make):
    assert post(make(discord_public_key=None), {"type": 1}).status_code == 404
    assert post(make(discord_application_id=None), {"type": 1}).status_code == 404
    assert post(make(public_url=None), {"type": 1}).status_code == 404  # links in replies must be absolute


def test_garbage_after_a_valid_signature_is_rejected_without_crashing(c, dc):
    assert post(c, None, raw=b"not json").status_code == 400
    assert post(c, None, raw=b"[1, 2]").status_code == 400
    assert post(c, {"type": 99}).status_code == 400                      # an interaction type we do not handle
    assert post(c, {"type": 2, "data": {"name": "download"}}).status_code == 400   # no user
    assert post(c, {"type": 2, "member": {"user": {"id": "x"}}, "data": {}}).status_code == 400
    assert post(c, None, raw=b" " * 200_000).status_code == 413
    assert c.queued == [] and dc.calls == []


# --- commands ------------------------------------------------------------------------------------------------------

def test_replies_are_private_ping_nobody_and_show_no_previews(c):
    for name in ("help", "confirm18"):
        data = post(c, command(name)).json()["data"]
        assert data["flags"] == 64 | 4 and data["allowed_mentions"] == {"parse": []}


def test_help_lists_the_commands(c):
    reply = post(c, command("help")).json()
    assert reply["type"] == 4 and "/download" in reply["data"]["content"] and "15 minutes" in reply["data"]["content"]
    assert post(c, command("nonsense")).json()["data"]["content"] == reply["data"]["content"]  # unknown command: help


def test_download_defers_privately_then_edits_the_answer_with_the_outcome(c, dc):
    r = post(c, command("download", {"link": "https://example.com/video"}))
    assert r.json() == {"type": 5, "data": {"flags": 64}}               # "thinking..." within Discord's 3 seconds, visible to them only
    assert len(c.queued) == 1
    [call] = dc.calls
    assert call.method == "PATCH" and call.url.path == f"/api/v10/webhooks/{APP_ID}/{TOKEN}/messages/@original"
    assert dc.texts() == ["Queued. I'll message you when it's ready."] and dc.bodies()[0]["allowed_mentions"] == {"parse": []}
    job = job_of(c, c.queued[0])
    assert job.preset == "720p" and job.notify == {"kind": "discord", "application_id": APP_ID, "token": TOKEN}


def test_format_option_beats_the_default_and_preset_changes_the_default_for_that_user_only(c):
    post(c, command("download", {"link": "https://example.com/a", "format": "mp3"}))
    assert job_of(c, c.queued[-1]).preset == "mp3"
    assert "now 1080p" in post(c, command("preset", {"format": "1080p"})).json()["data"]["content"]
    post(c, command("download", {"link": "https://example.com/b"}))
    assert job_of(c, c.queued[-1]).preset == "1080p"
    post(c, command("download", {"link": "https://example.com/c"}, user="43"))
    assert job_of(c, c.queued[-1]).preset == "720p"                      # another person's default is untouched
    assert "Choose one of" in post(c, command("preset", {"format": "wav"})).json()["data"]["content"]


def test_a_link_with_no_url_in_it_is_explained(c, dc):
    assert "http" in post(c, command("download", {"link": "just some words"})).json()["data"]["content"]
    assert post(c, command("download")).json()["data"]["content"].startswith("Send a link")
    assert c.queued == [] and dc.calls == []


def test_works_in_direct_messages_too(c):
    post(c, command("download", {"link": "https://example.com/dm"}, dm=True))
    assert len(c.queued) == 1


def test_internal_addresses_are_refused_like_everywhere_else(c, dc):
    post(c, command("download", {"link": "http://127.0.0.1:8000/secret"}))
    assert c.queued == [] and "can't use that link" in dc.texts()[0]


def test_adult_links_need_the_person_to_confirm_they_are_18(c, dc):
    link = {"link": "https://www.pornhub.com/view_video.php?viewkey=abc"}
    post(c, command("download", link))
    assert c.queued == [] and "/confirm18" in dc.texts()[0] and "adult" in dc.texts()[0]
    post(c, command("confirm18"))
    post(c, command("download", link))
    assert len(c.queued) == 1 and job_of(c, c.queued[0]).adult
    post(c, command("download", link, user="43"))                         # confirmation is per person
    assert len(c.queued) == 1


def test_the_allow_list_turns_everyone_else_away(make, dc):
    c = make(discord_allowed_users=[42])
    assert post(c, command("download", {"link": "https://example.com/x"}, user="99")).json()["data"]["content"] == "This bot is private."
    assert c.queued == [] and dc.calls == []
    assert post(c, command("download", {"link": "https://example.com/x"}, user="42")).json()["type"] == 5
    assert len(c.queued) == 1


def test_each_person_is_rate_limited(c, dc):
    for i in range(10):
        post(c, command("download", {"link": f"https://example.com/{i}"}))
    post(c, command("download", {"link": "https://example.com/eleventh"}))
    assert len(c.queued) == 10 and "Slow down" in dc.texts()[-1]
    post(c, command("download", {"link": "https://example.com/other"}, user="43"))
    assert len(c.queued) == 11


def test_a_taken_down_site_is_not_available(c, dc):
    c.portal.call(c.app.state.db.add_block, "host", "blocked.example", "takedown", "admin1")
    c.app.state.rules.invalidate()
    post(c, command("download", {"link": "https://blocked.example/video"}))
    assert c.queued == [] and dc.texts() == ["That content is not available."]


# --- secrets and the job view --------------------------------------------------------------------------------------

def test_the_interaction_token_is_never_logged_or_shown_by_the_api(c, logs):
    post(c, command("download", {"link": "https://example.com/video"}))
    assert "bot job queued" in logs.text                                  # positive control: the request was logged
    assert TOKEN not in logs.text and "SECRET_part" not in logs.text
    job = c.get(f"/api/v1/jobs/{c.queued[0]}")
    assert job.status_code == 200 and TOKEN not in job.text and "notify" not in job.json()


# --- telling the person when it is done ----------------------------------------------------------------------------

async def finished_job(c, **kw):
    store: JobStore = c.app.state.store
    job = await store.create(url="https://example.com/v", preset="mp3", notify={"kind": "discord", "application_id": APP_ID, "token": TOKEN})
    for k, v in kw.items():
        setattr(job, k, v)
    return job


def test_a_finished_job_sends_the_link_privately_without_pinging_anyone(c, dc):
    job = c.portal.call(lambda: finished_job(c, state="done", title="@everyone free money <@123>", filename="x.mp3"))
    c.portal.call(notify.notify_finished, c.app.state.http, Settings(**CFG), job)
    [call] = dc.calls
    body = json.loads(call.content)
    assert call.method == "POST" and call.url.path == f"/api/v10/webhooks/{APP_ID}/{TOKEN}"
    assert body["flags"] == 64 | 4 and body["allowed_mentions"] == {"parse": []}
    assert f"https://hotload.example/api/v1/jobs/{job.id}/file?token={job.token}" in body["content"]


def test_failures_and_the_age_hint_are_worded_for_discord(c, dc):
    settings = Settings(**CFG)
    failed = c.portal.call(lambda: finished_job(c, state="failed", message="Age confirmation required"))
    c.portal.call(notify.notify_finished, c.app.state.http, settings, failed)
    text = dc.texts()[0]
    assert text.startswith("Could not download:") and "/confirm18" in text and "Send /confirm18" not in text


def test_sending_survives_rate_limits_expiry_and_outages(dc):
    import asyncio

    settings = Settings(**CFG)
    client = httpx.AsyncClient(transport=httpx.MockTransport(dc.handler))

    async def send(**kw):
        return await notify.discord_send(client, settings, APP_ID, TOKEN, "hi", **kw)

    dc.statuses = [429, 200]
    assert asyncio.run(send()) and len(dc.calls) == 2                      # waited and retried
    dc.calls.clear()
    dc.statuses = [404]
    assert asyncio.run(send()) is False and len(dc.calls) == 1             # the 15 minutes ran out: no pointless retries
    dc.calls.clear()
    dc.statuses = [500, 500, 500]
    assert asyncio.run(send()) is False and len(dc.calls) == 3


def test_urls_are_never_built_from_things_that_are_not_ids_and_tokens(dc):
    import asyncio

    settings = Settings(**CFG)
    client = httpx.AsyncClient(transport=httpx.MockTransport(dc.handler))
    for app_id, token in ((APP_ID, "../../x"), (APP_ID, "a/b"), (APP_ID, "a?x=1"), ("12/34", TOKEN), ("abc", TOKEN), (APP_ID, ""), (APP_ID, "a" * 600)):
        assert asyncio.run(notify.discord_send(client, settings, app_id, token, "x")) is False
    assert dc.calls == []


# --- configuration, command definitions, registration --------------------------------------------------------------

def test_settings_reject_a_malformed_public_key_or_application_id(tmp_path):
    for bad in ("nothex", "ab" * 31, "ab" * 33):
        with pytest.raises(ValueError):
            Settings(data_dir=tmp_path, discord_public_key=bad)
    with pytest.raises(ValueError):
        Settings(data_dir=tmp_path, discord_application_id="not-a-number")
    assert Settings(data_dir=tmp_path, discord_public_key=PUB.upper()).discord_public_key == PUB


def test_command_definitions_obey_discords_rules():
    names = [c["name"] for c in discord.COMMANDS]
    assert names == ["download", "preset", "confirm18", "help"]
    for cmd in discord.COMMANDS:
        assert re.fullmatch(r"[-_a-z0-9]{1,32}", cmd["name"]) and 1 <= len(cmd["description"]) <= 100
        for opt in cmd.get("options", []):
            assert re.fullmatch(r"[-_a-z0-9]{1,32}", opt["name"]) and len(opt["description"]) <= 100
            assert len(opt.get("choices", [])) <= 25 and all(len(ch["name"]) <= 100 for ch in opt.get("choices", []))
    download = discord.COMMANDS[0]["options"]
    assert [o["name"] for o in download if o["required"]] == ["link"]
    assert [ch["value"] for ch in download[1]["choices"]] == list(discord.PRESETS)


def test_registering_commands_sends_the_bot_token_only_to_discord_and_never_prints_it(monkeypatch, capsys):
    from hotload import config

    sent = {}

    def fake_put(url, json=None, headers=None, timeout=None):
        sent.update(url=url, json=json, headers=headers)
        return httpx.Response(200, json=[])

    monkeypatch.setattr(httpx, "put", fake_put)
    for k, v in {"HOTLOAD_DISCORD_PUBLIC_KEY": PUB, "HOTLOAD_DISCORD_APPLICATION_ID": APP_ID, "HOTLOAD_PUBLIC_URL": "https://hotload.example",
                 "HOTLOAD_DISCORD_BOT_TOKEN": "BOT-TOKEN-SECRET"}.items():
        monkeypatch.setenv(k, v)
    config.get_settings.cache_clear()
    try:
        assert admin.main(["discord-commands"]) == 0
    finally:
        config.get_settings.cache_clear()
    assert sent["url"] == f"https://discord.com/api/v10/applications/{APP_ID}/commands"
    assert sent["headers"] == {"Authorization": "Bot BOT-TOKEN-SECRET"} and sent["json"] == discord.COMMANDS
    assert "BOT-TOKEN-SECRET" not in capsys.readouterr().out
