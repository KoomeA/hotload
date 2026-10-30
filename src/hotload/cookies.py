"""Bring-your-own cookies: a user's own site session, for content their account can see (members-only, age-gated).

Handling rules (this server becomes a custodian of credentials):
- Encrypted at rest (Fernet, key from HOTLOAD_COOKIE_KEY). No key configured = the feature is off.
- At upload, only cookies that apply to the stated domain are kept; everything else is discarded.
- Values are never returned by the API and never logged.
- Used only by jobs that opt in, only for that user, and only when the job's host matches the saved domain.
- The cookie file given to yt-dlp lives in a private 0700 temp directory *outside* the output folder (so it can never end
  up in a ZIP or a signed download) and is deleted as soon as the download ends.
"""

import contextlib
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from .config import Settings

MAX_UPLOAD_BYTES = 100_000
MAX_COOKIES = 200
MAX_DOMAINS = 10
HEADER = "# Netscape HTTP Cookie File\n"
_HOST = re.compile(r"^(?=.{4,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")


class CookieError(ValueError):
    pass


@dataclass
class Parsed:
    text: str
    kept: int
    dropped: int


def clean_domain(raw: str) -> str:
    host = (urlsplit(raw if "//" in raw else f"//{raw}").hostname or "").lower().strip(".")
    if not _HOST.fullmatch(host):
        raise CookieError("Enter a site name such as example.com")
    return host


def applies(cookie_domain: str, host: str) -> bool:
    """Would a cookie scoped to `cookie_domain` be sent to `host`?"""
    cd = cookie_domain.lower().lstrip(".")
    return bool(cd) and (host == cd or host.endswith("." + cd))


_SECOND_LEVEL = {"co", "com", "org", "net", "gov", "ac", "edu", "or", "ne", "go"}


def too_broad(cookie_domain: str) -> bool:
    """A cookie scoped to a whole TLD (".com") or a public suffix (".co.uk") would be sent to unrelated sites.

    Browsers never issue these, so one in an upload is broken or hostile. This is a heuristic, not the Public Suffix
    List; it errs toward dropping.
    """
    labels = cookie_domain.lower().lstrip(".").split(".")
    return len(labels) < 2 or (len(labels) == 2 and len(labels[1]) == 2 and labels[0] in _SECOND_LEVEL)


def parse_netscape(text: str, domain: str) -> Parsed:
    """Keep only well-formed cookies that apply to `domain`; return them re-serialised."""
    if len(text.encode("utf-8", "ignore")) > MAX_UPLOAD_BYTES:
        raise CookieError("That cookie file is too large")
    kept, dropped = [], 0
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_"):]
            prefix = "#HttpOnly_"
        elif line.startswith("#"):
            continue
        else:
            prefix = ""
        parts = line.split("\t")
        if len(parts) != 7:
            dropped += 1
            continue
        cdom, _flag, path, secure, expiry, name, value = parts
        if (not applies(cdom, domain) or too_broad(cdom) or not name or not path.startswith("/")
                or secure.upper() not in ("TRUE", "FALSE") or not re.fullmatch(r"\d{0,13}", expiry)
                or re.search(r"[\r\n\x00]", name + value) or len(name) > 200 or len(value) > 4000):
            dropped += 1
            continue
        kept.append(prefix + "\t".join(parts))
        if len(kept) > MAX_COOKIES:
            raise CookieError(f"At most {MAX_COOKIES} cookies per site")
    if not kept:
        raise CookieError("No usable cookies for that site were found in the file")
    return Parsed(HEADER + "\n".join(kept) + "\n", len(kept), dropped)


class CookieVault:
    def __init__(self, settings: Settings, db):
        keys = [k.strip() for k in (settings.cookie_key or "").split(",") if k.strip()]
        self.fernet = MultiFernet([Fernet(k) for k in keys]) if keys else None
        self.db = db

    @property
    def enabled(self) -> bool:
        return self.fernet is not None

    async def save(self, user_id: str, domain: str, text: str) -> Parsed:
        parsed = parse_netscape(text, domain)
        existing = {c.domain for c in await self.db.list_cookies(user_id)}
        if domain not in existing and len(existing) >= MAX_DOMAINS:
            raise CookieError(f"At most {MAX_DOMAINS} sites")
        await self.db.put_cookie(user_id, domain, self.fernet.encrypt(parsed.text.encode()).decode(), parsed.kept)
        return parsed

    async def match(self, user_id: str, url: str):
        """The saved entry whose domain covers this URL's host (longest match), or None."""
        host = (urlsplit(url).hostname or "").lower()
        best = None
        for c in await self.db.list_cookies(user_id):
            if applies(c.domain, host) and (best is None or len(c.domain) > len(best.domain)):
                best = c
        return best

    def decrypt(self, row) -> str:
        try:
            return self.fernet.decrypt(row.blob.encode()).decode()
        except InvalidToken as exc:
            raise CookieError("Saved cookies cannot be read (the encryption key changed). Upload them again.") from exc

    @contextlib.asynccontextmanager
    async def file_for(self, user_id: str, url: str):
        """Yield a private cookie file path for this user and URL (or None). Always cleaned up."""
        row = await self.match(user_id, url) if self.enabled else None
        if row is None:
            yield None
            return
        directory = Path(tempfile.mkdtemp(prefix="hl-cookies-"))  # mkdtemp creates it 0700
        try:
            path = directory / "cookies.txt"
            path.touch(mode=0o600)
            path.write_text(self.decrypt(row))
            yield path
        finally:
            shutil.rmtree(directory, ignore_errors=True)
