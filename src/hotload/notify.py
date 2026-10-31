"""Tell the person who asked (Telegram) when their job finishes. Plain text only: titles are untrusted input."""

import asyncio
import logging
import re

import httpx

from .config import Settings
from .logging_setup import log

logger = logging.getLogger("hotload.notify")
AGE_HINT = "\nSend /confirm18 if you are 18 or older, then send the link again."


def telegram_enabled(settings: Settings) -> bool:
    return bool(settings.telegram_bot_token and settings.telegram_webhook_secret and settings.public_url)


async def telegram_send(client: httpx.AsyncClient, settings: Settings, chat_id: int, text: str) -> bool:
    """Best-effort sendMessage (3 tries). Never raises: a failed notification must not fail a finished job."""
    url = f"{settings.telegram_api_base.rstrip('/')}/bot{settings.telegram_bot_token}/sendMessage"
    for attempt in range(3):
        try:
            r = await client.post(url, json={"chat_id": chat_id, "text": text[:3900], "disable_web_page_preview": True}, timeout=15)
            if r.status_code == 200:
                return True
            if r.status_code in (400, 401, 403, 404):  # blocked the bot, unknown chat, bad token: retrying cannot help
                log(logger, "telegram rejected message", logging.WARNING, status=r.status_code)
                return False
        except httpx.HTTPError as exc:
            log(logger, "telegram unreachable", logging.WARNING, error=type(exc).__name__, attempt=attempt + 1)
    return False


def discord_enabled(settings: Settings) -> bool:
    return bool(settings.discord_public_key and settings.discord_application_id and settings.public_url)


_SNOWFLAKE = re.compile(r"\d{1,25}")
_TOKEN = re.compile(r"[A-Za-z0-9._\-]{1,512}")


async def discord_send(client: httpx.AsyncClient, settings: Settings, application_id: str, token: str, text: str, *,
                       edit_original: bool = False) -> bool:
    """Answer an interaction through its webhook (valid for 15 minutes). Messages are private to the person who asked
    (ephemeral), never ping anyone (a title is untrusted text, so no mentions are parsed) and show no link previews.
    Best-effort: never raises."""
    if not _SNOWFLAKE.fullmatch(str(application_id)) or not _TOKEN.fullmatch(str(token)):
        return False  # never build a URL from something that is not an id and an interaction token
    url = f"{settings.discord_api_base.rstrip('/')}/webhooks/{application_id}/{token}" + ("/messages/@original" if edit_original else "")
    body = {"content": text[:1900], "allowed_mentions": {"parse": []}}
    if not edit_original:
        body["flags"] = 64 | 4  # ephemeral + suppress embeds
    for attempt in range(3):
        try:
            r = await client.request("PATCH" if edit_original else "POST", url, json=body, timeout=15)
            if r.status_code in (200, 204):
                return True
            if r.status_code == 429:
                try:
                    wait = min(float(r.json().get("retry_after", 1)), 5.0)
                except (ValueError, TypeError, AttributeError):
                    wait = 1.0
                await asyncio.sleep(wait)
                continue
            if r.status_code < 500:  # expired interaction (404), bad request: retrying cannot help
                log(logger, "discord rejected message", logging.WARNING, status=r.status_code)
                return False
        except httpx.HTTPError as exc:
            log(logger, "discord unreachable", logging.WARNING, error=type(exc).__name__, attempt=attempt + 1)
    return False


def finished_text(job, settings: Settings) -> str:
    base = (settings.public_url or "").rstrip("/")
    if job.state == "done":
        link = f"{base}/api/v1/jobs/{job.id}/file?token={job.token}"
        return f"Done: {job.title or job.filename or 'your file'}\n{link}\n(The link works for about an hour.)"
    msg = job.message or "The download failed."
    return f"Could not download: {msg}" + (AGE_HINT if "Age confirmation" in msg else "")


async def notify_finished(client: httpx.AsyncClient, settings: Settings, job) -> None:
    n = job.notify or {}
    if n.get("kind") == "discord" and discord_enabled(settings):
        text = finished_text(job, settings).replace(AGE_HINT, "\nRun /confirm18 if you are 18 or older, then try again.")
        await discord_send(client, settings, str(n.get("application_id")), str(n.get("token")), text)
    elif n.get("kind") == "telegram" and telegram_enabled(settings) and isinstance(n.get("chat_id"), int):
        await telegram_send(client, settings, n["chat_id"], finished_text(job, settings))
