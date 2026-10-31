"""Discord bot: slash commands over the interactions webhook (no gateway connection, no extra process).

Discord signs every request with Ed25519; nothing is read from a request until the signature over timestamp + body checks out
against the application's public key. Every reply is private to the person who ran the command (ephemeral), so download links
(which carry a token) are never posted in a channel, and no message can ping anyone: titles come from untrusted sites.
Replies use the interaction's own webhook, which Discord keeps valid for 15 minutes.
"""

import json
import logging
import time

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request

from .. import botcore, notify
from ..config import Settings
from ..logging_setup import log
from .bots import DEFAULT_PRESET, PRESETS, find_urls

logger = logging.getLogger("hotload.discord")
MAX_BODY = 100_000
MAX_SKEW = 300  # seconds a signed request may differ from our clock (replay window)
EPHEMERAL, NO_EMBEDS = 64, 4
AGE_HINT = " Run /confirm18 if you are 18 or older, then try again."
HELP = (
    "/download link:<link> [format:<mp3|m4a|opus|best_mp4|1080p|720p|480p>]  queue a download\n"
    "/preset format:<...>  change your default format (starts at 720p)\n"
    "/confirm18  confirm you are 18+ to allow adult sites\n"
    "/help  this message\n\n"
    "Replies are visible only to you. Download links work for about an hour, and I can only reply for 15 minutes after you "
    "run a command, so very long downloads may not report back. Only download content you have the right to download."
)

_FORMAT_NAMES = {"mp3": "MP3 audio", "m4a": "M4A audio", "opus": "Opus audio", "best_mp4": "Best MP4", "1080p": "1080p video",
                 "720p": "720p video", "480p": "480p video"}
_FORMAT_CHOICES = [{"name": _FORMAT_NAMES[p], "value": p} for p in PRESETS]

# Registered once with `python -m hotload.admin discord-commands`.
COMMANDS = [
    {"name": "download", "type": 1, "description": "Download the video or audio at a link",
     "options": [{"name": "link", "type": 3, "description": "The page or media link", "required": True, "max_length": 2000},
                 {"name": "format", "type": 3, "description": "Output format (default: your /preset)", "required": False,
                  "choices": _FORMAT_CHOICES}]},
    {"name": "preset", "type": 1, "description": "Set your default download format",
     "options": [{"name": "format", "type": 3, "description": "Format", "required": True, "choices": _FORMAT_CHOICES}]},
    {"name": "confirm18", "type": 1, "description": "Confirm you are 18 or older to allow adult sites"},
    {"name": "help", "type": 1, "description": "How to use this bot"},
]


def reply(text: str) -> dict:
    """An immediate private answer."""
    return {"type": 4, "data": {"content": text[:1900], "flags": EPHEMERAL | NO_EMBEDS, "allowed_mentions": {"parse": []}}}


def verified(public_key_hex: str, signature_hex: str, timestamp: str, body: bytes, now: float | None = None) -> bool:
    try:
        if abs((now or time.time()) - int(timestamp)) > MAX_SKEW:
            return False
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key_hex)).verify(bytes.fromhex(signature_hex), timestamp.encode() + body)
        return True
    except (InvalidSignature, ValueError):
        return False


def user_id_of(interaction: dict) -> int | None:
    """The person who ran the command: `member.user` in a server, `user` in a direct message."""
    user = (interaction.get("member") or {}).get("user") or interaction.get("user") or {}
    raw = user.get("id")
    return int(raw) if isinstance(raw, str) and raw.isdigit() and len(raw) <= 25 else None


def options_of(data: dict) -> dict[str, str]:
    return {o["name"]: o["value"] for o in data.get("options") or []
            if isinstance(o, dict) and isinstance(o.get("name"), str) and isinstance(o.get("value"), str)}


def add_discord_routes(app: FastAPI, settings: Settings) -> None:
    redis = lambda: app.state.redis  # noqa: E731

    async def finish(app_id: str, token: str, text: str) -> None:
        await notify.discord_send(app.state.http, settings, app_id, token, text, edit_original=True)

    async def queue(user: int, link: str, preset: str, app_id: str, token: str) -> None:
        """Runs after the deferred answer is sent (the 3-second limit), then edits that answer with the outcome."""
        try:
            text = await botcore.request_download(app, settings, "dc", user, link, preset,
                                                  {"kind": "discord", "application_id": app_id, "token": token}, AGE_HINT)
        except Exception:  # noqa: BLE001 - the person is waiting on "thinking...": always replace it
            logger.exception("discord command failed")
            text = "Something went wrong. Please try again."
        await finish(app_id, token, text)

    async def command(interaction: dict, background: BackgroundTasks) -> dict:
        data = interaction.get("data") if isinstance(interaction.get("data"), dict) else {}
        user = user_id_of(interaction)
        if user is None:
            raise HTTPException(400, "Invalid interaction")
        if settings.discord_allowed_users and user not in settings.discord_allowed_users:
            return reply("This bot is private.")
        name, opts = data.get("name"), options_of(data)
        if name == "help":
            return reply(HELP)
        if name == "confirm18":
            await redis().set(f"hotload:dc:age:{user}", "1", ex=180 * 86400)
            return reply("Thanks. Adult links are now accepted for you.")
        if name == "preset":
            if opts.get("format") not in PRESETS:
                return reply("Choose one of: " + ", ".join(PRESETS))
            await redis().set(f"hotload:dc:preset:{user}", opts["format"])
            return reply(f"Your default is now {opts['format']}.")
        if name == "download":
            urls = find_urls(opts.get("link", ""), 1)
            if not urls:
                return reply("Send a link that starts with http:// or https://.")
            preset = opts.get("format")
            if preset not in PRESETS:
                preset = (await redis().get(f"hotload:dc:preset:{user}")) or DEFAULT_PRESET
            background.add_task(queue, user, urls[0], preset, str(settings.discord_application_id), str(interaction.get("token")))
            return {"type": 5, "data": {"flags": EPHEMERAL}}  # "thinking..." now; the real answer is edited in
        return reply(HELP)

    @app.post("/api/v1/bots/discord")
    async def discord(request: Request, background: BackgroundTasks):
        if not notify.discord_enabled(settings):
            raise HTTPException(404, "Not found")
        body = await request.body()
        if len(body) > MAX_BODY:
            raise HTTPException(413, "Payload too large")
        if not verified(settings.discord_public_key, request.headers.get("x-signature-ed25519", ""),
                        request.headers.get("x-signature-timestamp", ""), body):
            log(logger, "discord request rejected", logging.WARNING)
            raise HTTPException(401, "invalid request signature")  # Discord probes this: it must be a 401
        try:
            interaction = json.loads(body)
        except ValueError as exc:
            raise HTTPException(400, "Invalid interaction") from exc
        if not isinstance(interaction, dict):
            raise HTTPException(400, "Invalid interaction")
        kind = interaction.get("type")
        if kind == 1:  # PING: Discord checks the endpoint when it is saved in the developer portal
            return {"type": 1}
        if kind == 2:  # APPLICATION_COMMAND
            return await command(interaction, background)
        raise HTTPException(400, "Unsupported interaction")
