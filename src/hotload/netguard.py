"""Connect-time address guard for yt-dlp's own network traffic.

Why this exists: validating a URL before fetching it only covers the first hop. yt-dlp follows redirects inside its HTTP
handlers and re-resolves hostnames when it connects, so a public page that redirects to http://169.254.169.254/ (or whose
DNS flips to a private address) would bypass a pre-check. This module filters at the one place every connection must pass:
name resolution. Inside the guard, any address that is not publicly routable is dropped from the result; if nothing is
left the lookup fails, so the connection is never attempted. Literal IPs go through the same call, so they are covered too.

Scope and limits (be honest about them):
- It applies only to threads that opted in with `guard()` (the yt-dlp work threads). Redis, the database, the payment
  providers, S3 and webhooks are unaffected: they often live on private addresses on purpose.
- It cannot see what FFmpeg (a separate process) or an upstream proxy connects to. Those need the network-layer egress
  filter (deploy/docker-compose.hardened.yml); see docs/LAUNCH.md.
"""

import socket
import threading
from collections.abc import Iterable
from contextlib import contextmanager

from . import security

_real_getaddrinfo = socket.getaddrinfo
_state = threading.local()


def allowed(sockaddr: tuple) -> bool:
    """May a connection go to this resolved socket address? (Tests substitute this to model a "public" test server.)"""
    return security.is_public_ip(sockaddr[0])


def _guarded_getaddrinfo(host, port, *args, **kwargs):
    results = _real_getaddrinfo(host, port, *args, **kwargs)
    ctx = getattr(_state, "ctx", None)
    if ctx is None:
        return results
    try:
        endpoint = (str(host).lower(), int(port))
    except (TypeError, ValueError):
        endpoint = (str(host).lower(), 0)
    if endpoint in ctx:  # exactly a proxy the operator configured (host AND port): reaching it is intended
        return results
    safe = [r for r in results if allowed(r[4])]
    if not safe:
        raise socket.gaierror(socket.EAI_NONAME, "blocked: the address is not publicly routable")
    return safe


_guarded_getaddrinfo._unguarded = _real_getaddrinfo  # type: ignore[attr-defined]


def install() -> None:
    """Idempotent. Harmless for threads outside a guard()."""
    if socket.getaddrinfo is not _guarded_getaddrinfo:
        socket.getaddrinfo = _guarded_getaddrinfo


@contextmanager
def guard(allow: Iterable[tuple[str, int]] = ()):
    """Guard this thread. `allow` lists (host, port) endpoints that may be reached even if private: the configured proxies.
    Matching the port too matters: a proxy on 127.0.0.1:3128 must not make the rest of loopback reachable."""
    previous = getattr(_state, "ctx", None)
    _state.ctx = frozenset((h.lower(), int(p)) for h, p in allow)
    try:
        yield
    finally:
        _state.ctx = previous


install()
