"""Thumbnail proxy.

Showing a site's thumbnail directly makes the VISITOR's browser contact whatever host the page's metadata names, so a crafted
link (?url=https://attacker/page) would reveal the visitor's IP and act as a timing beacon. Instead the server fetches the
image and the page only ever loads same-origin images.

- Only URLs the server itself produced can be proxied: they are signed (HMAC) when `/extract` returns them, so this is not an
  open image proxy.
- The fetch is SSRF-safe like webhook delivery: resolved once, every answer public, connection pinned to that address, no
  redirects, short timeout, byte cap.
- Only raster image types are served (never SVG, which can carry script), with nosniff and a sandboxing CSP.
"""

import base64
import hashlib
import hmac
import socket

import httpx

from . import security, webhooks
from .config import Settings

MAX_BYTES = 1_500_000
ALLOWED_TYPES = ("image/jpeg", "image/png", "image/webp", "image/gif", "image/avif")


class ThumbError(Exception):
    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status


def _sig(secret: str, url: str) -> str:
    return hmac.new(secret.encode(), b"thumb:" + url.encode(), hashlib.sha256).hexdigest()[:32]


def sign(secret: str, url: str | None) -> str | None:
    """A same-origin URL for `url`, or None if it is not a plain web address."""
    if not url or not url.lower().startswith(("http://", "https://")) or len(url) > 2000:
        return None
    token = base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")
    return f"/api/v1/thumb?u={token}&s={_sig(secret, url)}"


def verify(secret: str, token: str, sig: str) -> str:
    try:
        url = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).decode()
    except (ValueError, UnicodeDecodeError) as exc:
        raise ThumbError("Bad thumbnail address", 403) from exc
    if not hmac.compare_digest(sig.encode("utf-8", "ignore"), _sig(secret, url).encode()):
        raise ThumbError("Bad thumbnail signature", 403)
    return url


async def fetch(url: str, settings: Settings, client: httpx.AsyncClient, resolve=None) -> tuple[bytes, str]:
    try:
        target, extra_headers, extensions = webhooks.request_target(url, settings, resolve)
    except security.UnsafeURL as exc:
        raise ThumbError(f"Thumbnail address refused: {exc}", 400) from exc
    except socket.gaierror as exc:
        raise ThumbError("Thumbnail host cannot be resolved", 502) from exc
    try:
        async with client.stream("GET", target, headers={**extra_headers, "accept": "image/*", "user-agent": "Hotload-Thumb/1"},
                                 follow_redirects=False, timeout=8, extensions=extensions) as r:
            ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
            if r.status_code != 200:
                raise ThumbError(f"Thumbnail host answered {r.status_code}")
            if ctype not in ALLOWED_TYPES:
                raise ThumbError("Not a supported image type", 415)
            data = bytearray()
            async for chunk in r.aiter_bytes(16384):
                data += chunk
                if len(data) > MAX_BYTES:
                    raise ThumbError("Thumbnail is too large", 413)
    except httpx.HTTPError as exc:
        raise ThumbError("Thumbnail could not be fetched") from exc
    return bytes(data), ctype
