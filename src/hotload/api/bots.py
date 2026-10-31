"""Telegram bot: send a link, get a download link back when the job finishes. Webhook-driven (no polling process).

Anyone who can message the bot is an anonymous user for limits and quotas, keyed by chat id. Adult links need the chat to
confirm it is 18+. Replies are plain text because titles and URLs are untrusted input.
"""

import hmac
import logging
import re

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request

from .. import botcore, notify
from ..config import Settings

logger = logging.getLogger("hotload.bot")
URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
PRESETS = ("mp3", "m4a", "opus", "best_mp4", "1080p", "720p", "480p")
DEFAULT_PRESET = "720p"
COMMAND_PRESETS = {"/mp3": "mp3", "/audio": "mp3", "/video": None, "/dl": None}  # None = the chat's default preset
HELP = (
    "Send me a link and I'll send back a download link when it's ready.\n\n"
    "/mp3 <link>   audio only\n"
    "/video <link>  video in this chat's default quality\n"
    "/preset <mp3|m4a|opus|best_mp4|1080p|720p|480p>   change the default\n"
    "/confirm18   confirm you are 18+ to allow adult sites in this chat\n"
    "/help   this message\n\n"
    "Only download content you have the right to download. Files are deleted after an hour."
)


def parse_message(update: dict):
    """Return (chat_id, chat_type, text) for a plain text message, else None."""
    msg = update.get("message") if isinstance(update, dict) else None
    if not isinstance(msg, dict):
        return None
    chat, text = msg.get("chat") or {}, msg.get("text")
    if not isinstance(chat.get("id"), int) or not isinstance(text, str):
        return None
    return chat["id"], str(chat.get("type", "private")), text[:4000]


def split_command(text: str) -> tuple[str | None, str]:
    """"/mp3@HotloadBot https://x" -> ("/mp3", "https://x"); plain text -> (None, text)."""
    if not text.startswith("/"):
        return None, text
    head, _, rest = text.partition(" ")
    return head.split("@", 1)[0].lower(), rest.strip()


def find_urls(text: str, limit: int = 3) -> list[str]:
    return list(dict.fromkeys(u.rstrip(").,;!?") for u in URL_RE.findall(text)))[:limit]


def add_bot_routes(app: FastAPI, settings: Settings) -> None:
    redis = lambda: app.state.redis  # noqa: E731

    async def say(chat_id: int, text: str) -> None:
        await notify.telegram_send(app.state.http, settings, chat_id, text)

    async def preset_of(chat_id: int) -> str:
        return (await redis().get(f"hotload:tg:preset:{chat_id}")) or DEFAULT_PRESET

    async def request_download(chat_id: int, url: str, preset: str) -> None:
        await say(chat_id, await botcore.request_download(
            app, settings, "tg", chat_id, url, preset, {"kind": "telegram", "chat_id": chat_id}, notify.AGE_HINT))

    async def handle(update: dict) -> None:
        parsed = parse_message(update)
        if parsed is None:
            return
        chat_id, chat_type, text = parsed
        if settings.telegram_allowed_chats and chat_id not in settings.telegram_allowed_chats:
            return  # silent: do not reveal the bot to chats that are not allowed
        command, rest = split_command(text)
        if command in ("/start", "/help"):
            return await say(chat_id, HELP)
        if command == "/confirm18":
            await redis().set(f"hotload:tg:age:{chat_id}", "1", ex=180 * 86400)
            return await say(chat_id, "Thanks. Adult links are now accepted in this chat.")
        if command == "/preset":
            if rest not in PRESETS:
                return await say(chat_id, "Choose one of: " + ", ".join(PRESETS))
            await redis().set(f"hotload:tg:preset:{chat_id}", rest)
            return await say(chat_id, f"Default is now {rest}.")
        if command is not None and command not in COMMAND_PRESETS:
            return await say(chat_id, HELP)
        if command is None and chat_type != "private":
            return  # in groups only commands trigger the bot, so it never reacts to every link people post
        urls = find_urls(rest)
        if not urls:
            return await say(chat_id, "Send me a link to download. /help shows what I can do.")
        preset = COMMAND_PRESETS.get(command) or await preset_of(chat_id)
        for url in urls:
            await request_download(chat_id, url, preset)

    @app.post("/api/v1/bots/telegram")
    async def telegram(request: Request, background: BackgroundTasks):
        if not notify.telegram_enabled(settings):
            raise HTTPException(404, "Not found")
        sent = request.headers.get("x-telegram-bot-api-secret-token", "")
        if not hmac.compare_digest(sent.encode(), (settings.telegram_webhook_secret or "").encode()):
            raise HTTPException(401, "Invalid secret")
        try:
            update = await request.json()
        except ValueError as exc:
            raise HTTPException(400, "Invalid update") from exc
        background.add_task(handle, update)  # answer Telegram immediately; the work happens after
        return {"ok": True}
