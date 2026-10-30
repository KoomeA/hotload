"""Signed job-completion webhooks to user-supplied URLs.

Because the server calls a URL chosen by a customer, delivery is SSRF-hardened: the destination is resolved once,
must be public, and the connection is pinned to that exact IP (so DNS rebinding between check and connect cannot
redirect it). No redirects are followed, the timeout is short, and the response body is never read.
"""

import hashlib
import hmac
import json
import socket
import time
from urllib.parse import urlsplit, urlunsplit

import httpx

from . import security
from .config import Settings


class WebhookError(Exception):
    def __init__(self, message: str, retryable: bool):
        super().__init__(message)
        self.retryable = retryable


def sign(secret: str, timestamp: int, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()


def build(job_public: dict, secret: str, now: float | None = None) -> tuple[bytes, dict]:
    ts = int(now or time.time())
    body = json.dumps({"event": "job.finished", "job": job_public, "sent_at": ts}, separators=(",", ":")).encode()
    headers = {"content-type": "application/json", "user-agent": "Hotload-Webhook/1", "x-hotload-event": "job.finished",
               "x-hotload-timestamp": str(ts), "x-hotload-signature": sign(secret, ts, body)}
    return body, headers


def _pin(url: str, settings: Settings, resolve=None) -> tuple[str, str]:
    """Validate `url`; return (url with the validated IP in place of the host, original hostname).

    The hostname is resolved exactly once. Every address from that single answer must be public, and the
    connection then goes to the first one, so nothing can change between the check and the connect.
    """
    answers: list = []
    resolve = resolve or socket.getaddrinfo

    def resolve_once(host, port):
        if not answers:
            answers.append(resolve(host, port))
        return answers[0]

    clean = security.validate_url(url, settings, resolve=resolve_once, enforce_allowlist=False)
    parts = urlsplit(clean)
    ip = answers[0][0][4][0] if answers else parts.hostname  # no lookup happened: the host was an IP literal
    netloc = f"[{ip}]" if ":" in ip else ip
    if parts.port:
        netloc += f":{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path or "/", parts.query, "")), parts.hostname


async def deliver(url: str, body: bytes, headers: dict, settings: Settings, client: httpx.AsyncClient,
                  resolve=None) -> int:
    """POST once. Returns the status. Raises WebhookError (retryable or not)."""
    try:
        target, host = _pin(url, settings, resolve)
    except security.UnsafeURL as exc:
        raise WebhookError(f"unsafe destination: {exc}", retryable=False) from exc
    try:
        async with client.stream("POST", target, content=body, headers={**headers, "host": host}, follow_redirects=False,
                                 timeout=settings.webhook_timeout_seconds, extensions={"sni_hostname": host}) as r:
            status = r.status_code  # the body is deliberately never read
    except httpx.HTTPError as exc:
        raise WebhookError(f"delivery failed: {type(exc).__name__}", retryable=True) from exc
    if status >= 500 or status in (408, 429):
        raise WebhookError(f"receiver returned {status}", retryable=True)
    if status >= 300:
        raise WebhookError(f"receiver returned {status}", retryable=False)
    return status
