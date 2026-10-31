import json
import logging

import httpx
import pytest
from fakeredis import FakeAsyncRedis
from fastapi.testclient import TestClient

from hotload import admin, logging_setup, notify, worker
from hotload.api import bots
from hotload.api.app import create_app
from hotload.config import Settings
from hotload.jobs import JobStore
from hotload.proxies import ProxyPool

TOKEN = "123456:SECRET-BOT-TOKEN"
HOOK = "hook-secret"
CFG = {"telegram_bot_token": TOKEN, "telegram_webhook_secret": HOOK, "public_url": "https://hotload.example",
       "rate_limit_per_minute": 1000, "secret_key": "s"}


class Telegram:
    """Records what the bot sends to Telegram and answers like its API."""

    def __init__(self):
        self.sent: list[dict] = []
        self.statuses: list[int] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/bot{TOKEN}/sendMessage", request.url.path
        self.sent.append(json.loads(request.content))
        status = self.statuses.pop(0) if self.statuses else 200
        return httpx.Response(status, json={"ok": status == 200})

    def texts(self):
        return [m["text"] for m in self.sent]


@pytest.fixture
def tg():
    return Telegram()


@pytest.fixture
def make(tmp_path, monkeypatch, tg):
    monkeypatch.setattr("socket.getaddrinfo", lambda host, port, *a, **k: [(2, 1, 6, "", ("93.184.216.34", port))])
    stack, queued = [], []

    def build(**o):
        async def enqueue(job_id, _q=None):
            queued.append(job_id)

        http = httpx.AsyncClient(transport=httpx.MockTransport(tg.handler))
        c = TestClient(create_app(Settings(data_dir=tmp_path, **{**CFG, **o}), redis=FakeAsyncRedis(decode_responses=True),
                                  enqueue=enqueue, http_client=http))
        c.__enter__()
        c.queued = queued
        stack.append(c)
        return c

    yield build
    for c in stack:
        c.__exit__(None, None, None)


def say(c, text, chat=42, kind="private", secret=HOOK):
    update = {"update_id": 1, "message": {"message_id": 1, "chat": {"id": chat, "type": kind}, "text": text}}
    return c.post("/api/v1/bots/telegram", json=update, headers={"x-telegram-bot-api-secret-token": secret})


def job_of(c, job_id):
    return c.portal.call(c.app.state.store.get, job_id)


# --- security of the endpoint -------------------------------------------------------------------

def test_disabled_without_configuration(make):
    assert say(make(telegram_bot_token=None), "/start").status_code == 404
    assert say(make(public_url=None), "/start").status_code == 404  # replies need absolute links


def test_requires_the_webhook_secret(make, tg):
    c = make()
    for secret in ("", "wrong", HOOK + "x", HOOK[:-1]):
        assert say(c, "https://example.com/v", secret=secret).status_code == 401
    assert tg.sent == [] and c.queued == []
    assert c.post("/api/v1/bots/telegram", json={}).status_code == 401  # header missing


def test_garbage_updates_are_ignored_not_crashing(make, tg):
    c = make()
    h = {"x-telegram-bot-api-secret-token": HOOK}
    assert c.post("/api/v1/bots/telegram", content=b"not json", headers=h).status_code == 400
    for update in ({}, {"edited_message": {}}, {"callback_query": {"id": "1"}}, {"message": {"chat": {"id": "str"}, "text": "x"}},
                   {"message": {"chat": {"id": 1}, "text": 5}}, {"message": {"chat": {"id": 1}}}, [], "x"):
        assert c.post("/api/v1/bots/telegram", json=update, headers=h).status_code == 200
    assert tg.sent == [] and c.queued == []


def test_allowlist_is_silent_for_other_chats(make, tg):
    c = make(telegram_allowed_chats=[7])
    say(c, "/start", chat=99)
    say(c, "https://example.com/v", chat=99)
    assert tg.sent == [] and c.queued == []
    say(c, "/start", chat=7)
    assert len(tg.sent) == 1 and tg.sent[0]["chat_id"] == 7


# --- conversation -----------------------------------------------------------------------------------

def test_help_and_unknown_input(make, tg):
    c = make()
    say(c, "/start")
    say(c, "/help@HotloadBot")
    say(c, "/nonsense")
    say(c, "hello there")
    assert tg.texts()[0] == bots.HELP and tg.texts()[1] == bots.HELP and tg.texts()[2] == bots.HELP
    assert "Send me a link" in tg.texts()[3] and c.queued == []


def test_link_creates_a_job_with_the_default_preset_and_replies(make, tg):
    c = make()
    assert say(c, "look https://example.com/watch?v=1, please!").status_code == 200
    [jid] = c.queued
    job = job_of(c, jid)
    assert job.url == "https://example.com/watch?v=1" and job.preset == "720p"  # punctuation stripped
    assert job.notify == {"kind": "telegram", "chat_id": 42} and job.user_id is None
    assert "Queued" in tg.texts()[0] and tg.sent[0]["chat_id"] == 42


def test_commands_choose_presets_and_the_default_is_per_chat(make, tg):
    c = make()
    say(c, "/mp3 https://example.com/a")
    say(c, "/preset 1080p")
    say(c, "/video https://example.com/b")
    say(c, "https://example.com/c")
    say(c, "/preset nope")
    say(c, "https://example.com/d", chat=43)  # another chat keeps the built-in default
    assert [job_of(c, j).preset for j in c.queued] == ["mp3", "1080p", "1080p", "720p"]
    assert "Default is now 1080p" in tg.texts()[1] and "Choose one of" in tg.texts()[4]


def test_groups_only_react_to_commands(make, tg):
    c = make()
    say(c, "https://example.com/v", kind="group", chat=-100)
    say(c, "just chatting", kind="supergroup", chat=-100)
    assert c.queued == [] and tg.sent == []
    say(c, "/mp3@HotloadBot https://example.com/v", kind="group", chat=-100)
    assert len(c.queued) == 1 and tg.sent[0]["chat_id"] == -100


def test_at_most_three_links_per_message(make):
    c = make()
    say(c, " ".join(f"https://example.com/{i}" for i in range(6)))
    assert len(c.queued) == 3


def test_unsafe_and_blocked_links_are_refused_politely(make, tg):
    c = make()
    say(c, "http://169.254.169.254/latest/meta-data")
    say(c, "http://127.0.0.1:8000/admin")
    assert c.queued == [] and all("can't use that link" in t for t in tg.texts())
    c.portal.call(c.app.state.db.add_block, "host", "gone.example.org", "dmca", None)
    c.app.state.rules.invalidate()
    say(c, "https://cdn.gone.example.org/v")
    assert tg.texts()[-1] == "That content is not available." and c.queued == []


def test_adult_links_need_the_chat_to_confirm_18_plus(make, tg):
    c = make()
    url = "https://www.pornhub.com/view_video.php?viewkey=x"
    say(c, url)
    assert c.queued == [] and "/confirm18" in tg.texts()[0]
    say(c, "/confirm18")
    say(c, url)
    [jid] = c.queued
    assert job_of(c, jid).age_confirmed is True and job_of(c, jid).adult is True
    say(c, url, chat=99)  # confirmation is per chat
    assert len(c.queued) == 1
    before = len(c.queued)
    open_gate = make(require_age_gate=False)  # a server that turned the gate off (self-host option)
    say(open_gate, url)
    assert len(open_gate.queued) == before + 1


def test_rate_limit_per_chat(make, tg):
    c = make()
    for i in range(12):
        say(c, f"https://example.com/{i}")
    assert len(c.queued) == 10 and any("Slow down" in t for t in tg.texts())
    say(c, "https://example.com/other", chat=43)
    assert len(c.queued) == 11  # a different chat has its own allowance


def test_quota_applies_per_chat_when_plans_are_enforced(make, tg, monkeypatch):
    import dataclasses

    from hotload import tiers

    # A small anonymous allowance so the 12-hour quota is reached before the 10-per-minute chat limit.
    monkeypatch.setitem(tiers.TIERS, "anonymous", dataclasses.replace(tiers.TIERS["anonymous"], jobs_per_window=3))
    c = make(quotas_enabled=True)
    for chat in (1, 2):
        for i in range(3):
            say(c, f"https://example.com/{chat}/{i}", chat=chat)
    assert len(c.queued) == 6  # each chat has its own allowance
    say(c, "https://example.com/more", chat=1)
    assert "Daily limit reached (3 downloads" in tg.texts()[-1] and len(c.queued) == 6
    assert job_of(c, c.queued[0]).max_duration == 20 * 60  # anonymous plan limits apply


# --- finishing, notifying ----------------------------------------------------------------------------

async def _notify_ctx(tmp_path, monkeypatch, tg, **o):
    settings = Settings(data_dir=tmp_path, **{**CFG, **o})
    monkeypatch.setattr(worker, "get_settings", lambda: settings)
    monkeypatch.setattr(worker, "_pool", ProxyPool())
    http = httpx.AsyncClient(transport=httpx.MockTransport(tg.handler))
    return settings, http, JobStore(FakeAsyncRedis(decode_responses=True), 600)


async def test_finished_job_messages_the_chat_with_a_download_link(tmp_path, monkeypatch, tg):
    from hotload import engine

    settings, http, store = await _notify_ctx(tmp_path, monkeypatch, tg)

    def fake(url, s, workdir, preset, proxy=None, **kw):
        workdir.mkdir(parents=True, exist_ok=True)
        (workdir / "v.mp3").write_bytes(b"x")
        return engine.DownloadResult(workdir / "v.mp3", "A *bold* title_", "G")

    monkeypatch.setattr(engine, "download", fake)
    job = await store.create(url="https://example.com/v", preset="mp3", notify={"kind": "telegram", "chat_id": 42})
    await worker.run_download({"store": store, "http": http}, job.id)
    [msg] = tg.sent
    assert msg["chat_id"] == 42 and msg["disable_web_page_preview"] is True and "parse_mode" not in msg  # plain text
    done = await store.get(job.id)
    assert f"https://hotload.example/api/v1/jobs/{job.id}/file?token={done.token}" in msg["text"] and "A *bold* title_" in msg["text"]
    assert TOKEN not in msg["text"]
    await http.aclose()


async def test_failed_job_explains_and_hints_for_age_confirmation(tmp_path, monkeypatch, tg):
    from hotload import engine

    settings, http, store = await _notify_ctx(tmp_path, monkeypatch, tg)
    monkeypatch.setattr(engine, "download", lambda *a, **k: (_ for _ in ()).throw(engine.EngineError("Age confirmation required for this content")))
    job = await store.create(url="https://example.com/v", preset="mp3", notify={"kind": "telegram", "chat_id": 5})
    await worker.run_download({"store": store, "http": http}, job.id)
    assert "Could not download: Age confirmation required" in tg.texts()[0] and "/confirm18" in tg.texts()[0]
    await http.aclose()


async def test_no_message_when_the_bot_is_not_configured_or_job_has_no_notify(tmp_path, monkeypatch, tg):
    from hotload import engine

    settings, http, store = await _notify_ctx(tmp_path, monkeypatch, tg, telegram_bot_token=None)
    monkeypatch.setattr(engine, "download", lambda *a, **k: (_ for _ in ()).throw(engine.EngineError("nope")))
    for n in ({"kind": "telegram", "chat_id": 1}, None):
        job = await store.create(url="https://example.com/v", preset="mp3", notify=n)
        await worker.run_download({"store": store, "http": http}, job.id)
    assert tg.sent == []
    await http.aclose()


async def test_send_retries_transient_failures_but_not_permanent_ones(tg):
    s = Settings(**CFG)
    async with httpx.AsyncClient(transport=httpx.MockTransport(tg.handler)) as http:
        tg.statuses = [500, 200]
        assert await notify.telegram_send(http, s, 1, "x") is True and len(tg.sent) == 2
        tg.sent.clear()
        tg.statuses = [403]  # the user blocked the bot: retrying cannot help
        assert await notify.telegram_send(http, s, 1, "x") is False and len(tg.sent) == 1

    def boom(request):
        raise httpx.ConnectError("down")

    async with httpx.AsyncClient(transport=httpx.MockTransport(boom)) as http:
        assert await notify.telegram_send(http, s, 1, "x") is False  # never raises


def test_bot_token_never_reaches_the_logs(make, tg, logs):
    logging_setup.setup()  # as the real app does
    c = make()
    say(c, "/start")
    say(c, "https://example.com/v")
    tg.statuses = [403]
    say(c, "https://example.com/w")
    # positive controls: the capture works, and the bot's own log lines are there
    assert any("telegram rejected message" in r.getMessage() for r in logs.records)
    secret = TOKEN.split(":")[1]
    assert TOKEN not in logs.text and secret not in logs.text
    assert logging.getLogger("httpx").level == logging.WARNING


def test_the_httpx_silencing_is_what_keeps_the_token_out(make, tg, logs):
    """Control: with httpx logging at INFO (its default), the Telegram URL, token included, IS logged."""
    c = make()
    logging.getLogger("httpx").setLevel(logging.INFO)
    try:
        say(c, "/start")
    finally:
        logging.getLogger("httpx").setLevel(logging.WARNING)
    assert TOKEN in logs.text, "if this fails the token test above no longer proves anything"


def test_parsing_helpers():
    assert bots.split_command("/MP3@Bot  https://x") == ("/mp3", "https://x")
    assert bots.split_command("plain") == (None, "plain")
    assert bots.find_urls("a https://x.org/1). b https://x.org/1 c http://y.org/2!") == ["https://x.org/1", "http://y.org/2"]
    assert bots.find_urls("javascript:alert(1) ftp://x") == []
    assert bots.parse_message({"message": {"chat": {"id": 5, "type": "group"}, "text": "hi"}}) == (5, "group", "hi")


# --- admin: register the webhook --------------------------------------------------------------------

def test_admin_registers_the_webhook(monkeypatch):
    calls = []

    class R:
        status_code = 200

        def json(self):
            return {"ok": True}

    monkeypatch.setattr(admin, "get_settings", lambda: Settings(**CFG))
    monkeypatch.setattr(httpx, "post", lambda url, json, timeout: calls.append((url, json)) or R())
    assert admin.main(["telegram-webhook"]) == 0
    url, body = calls[0]
    assert url == f"https://api.telegram.org/bot{TOKEN}/setWebhook"
    assert body == {"url": "https://hotload.example/api/v1/bots/telegram", "secret_token": HOOK, "allowed_updates": ["message"]}
    monkeypatch.setattr(admin, "get_settings", lambda: Settings(data_dir="x"))
    assert admin.main(["telegram-webhook"]) == 1  # not configured: explains, does not call Telegram
    assert len(calls) == 1




def test_many_throwaway_chats_cannot_add_up_to_unlimited_work(make):
    """Each chat is within its own limit, but the bot as a whole stops at 300 requests a minute."""
    c = make()
    for chat in range(1, 306):
        say(c, f"https://example.com/{chat}", chat=chat)
    assert len(c.queued) == 300
