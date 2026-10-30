"""Takedown enforcement. Three kinds of rule:

- host:  a domain and all its subdomains
- url:   one exact normalised URL (weak: the same media is often reachable through many URLs)
- media: "<extractor>:<id>" such as "Youtube:jNQXAC9IVRw" (robust: matches however the link was written)
"""

from urllib.parse import urlsplit, urlunsplit

KINDS = ("host", "url", "media")


def _idna(host: str) -> str:
    """Compare hosts in punycode, so a takedown for bücher.de also matches xn--bcher-kva.de (and vice versa)."""
    host = host.lower().strip(".")
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError:
        return host


def normalise_url(url: str) -> str:
    p = urlsplit(url.strip())
    netloc = p.netloc.lower()
    if p.hostname:
        netloc = netloc.replace(p.hostname.lower(), _idna(p.hostname), 1)
    return urlunsplit((p.scheme.lower(), netloc, p.path.rstrip("/") or "/", p.query, ""))


def normalise_value(kind: str, value: str) -> str:
    value = value.strip()
    if kind == "host":
        return _idna(urlsplit(value).hostname if "//" in value else value)
    if kind == "url":
        return normalise_url(value)
    return value


class Blocklist:
    """Immutable snapshot of the rules; cheap to query."""

    def __init__(self, items=()):
        self.hosts = {i.value for i in items if i.kind == "host"}
        self.urls = {i.value for i in items if i.kind == "url"}
        self.media = {i.value for i in items if i.kind == "media"}

    def blocks_url(self, url: str) -> bool:
        p = urlsplit(url)
        host = _idna(p.hostname or "")
        if any(host == h or host.endswith("." + h) for h in self.hosts):
            return True
        return normalise_url(url) in self.urls

    def blocks_media(self, extractor: str | None, media_id: str | None) -> bool:
        return bool(extractor and media_id and f"{extractor}:{media_id}" in self.media)
