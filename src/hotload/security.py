"""SSRF-safe URL validation. Every user-supplied URL passes through `validate_url`."""

import ipaddress
import socket
from urllib.parse import urlsplit

from .config import Settings


class UnsafeURL(ValueError):
    pass


def _host_matches(host: str, patterns: list[str]) -> bool:
    host = host.lower().rstrip(".")
    return any(host == p.lower() or host.endswith("." + p.lower()) for p in patterns)


def _is_public_ip(ip: str) -> bool:
    addr = ipaddress.ip_address(ip)
    return addr.is_global and not addr.is_multicast


def validate_url(url: str, settings: Settings, resolve=socket.getaddrinfo) -> str:
    """Return a normalised URL or raise UnsafeURL. `resolve` is injectable for tests."""
    if len(url) > 2048:
        raise UnsafeURL("URL too long")
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https"):
        raise UnsafeURL("Only http and https URLs are allowed")
    if not parts.hostname or parts.username or parts.password:
        raise UnsafeURL("Invalid host or embedded credentials")
    host = parts.hostname
    if _host_matches(host, settings.blocked_hosts):
        raise UnsafeURL("This site is blocked")
    if settings.allowed_hosts and not _host_matches(host, settings.allowed_hosts):
        raise UnsafeURL("This site is not allowed")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        ips = [str(literal)]
    else:
        try:
            infos = resolve(host, parts.port or (443 if parts.scheme == "https" else 80))
        except socket.gaierror as exc:
            raise UnsafeURL("Host cannot be resolved") from exc
        ips = [info[4][0] for info in infos]
    if not ips or not all(_is_public_ip(ip) for ip in ips):
        raise UnsafeURL("URL resolves to a non-public address")
    return parts.geturl()
