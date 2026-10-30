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


# Ranges `is_global` still calls global but that embed or tunnel to other (possibly internal) IPv4 addresses.
_EMBEDDED = tuple(ipaddress.ip_network(n) for n in ("64:ff9b::/96", "64:ff9b:1::/48", "2002::/16", "2001::/32"))


def is_public_ip(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip.split("%")[0])
    except ValueError:
        return False
    if addr.version == 6 and (addr.ipv4_mapped is not None or any(addr in net for net in _EMBEDDED)):
        embedded = addr.ipv4_mapped
        return embedded is not None and is_public_ip(str(embedded))  # NAT64/6to4/Teredo can reach internal IPv4: refuse
    return addr.is_global and not addr.is_multicast


_is_public_ip = is_public_ip


def validate_url(url: str, settings: Settings, resolve=None, enforce_allowlist: bool = True) -> str:
    """Return a normalised URL or raise UnsafeURL. `resolve` is injectable for tests.

    `enforce_allowlist=False` is used for outbound requests made *while processing* an allowed URL
    (CDNs, manifests): they must still be public and not blocked, but need not be on the allowlist.
    """
    if len(url) > 2048:
        raise UnsafeURL("URL too long")
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https"):
        raise UnsafeURL("Only http and https URLs are allowed")
    try:
        parts.port  # noqa: B018 - raises ValueError for http://host:99999/ or http://host:abc/
    except ValueError as exc:
        raise UnsafeURL("Invalid port") from exc
    if not parts.hostname or parts.username or parts.password:
        raise UnsafeURL("Invalid host or embedded credentials")
    host = parts.hostname
    if _host_matches(host, settings.blocked_hosts):
        raise UnsafeURL("This site is blocked")
    if enforce_allowlist and settings.allowed_hosts and not _host_matches(host, settings.allowed_hosts):
        raise UnsafeURL("This site is not allowed")
    # Looked up at call time (tests substitute a resolver). The connect-time guard is deliberately bypassed here: this
    # function makes its own, clearer, decision and reports it.
    resolve = resolve or getattr(socket.getaddrinfo, "_unguarded", socket.getaddrinfo)
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
