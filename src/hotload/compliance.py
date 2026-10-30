"""Adult-content detection and age gate."""

from urllib.parse import urlsplit

# Non-exhaustive hint list; yt-dlp's `age_limit` is the primary signal.
ADULT_DOMAINS = (
    "pornhub.com", "xvideos.com", "xnxx.com", "xhamster.com", "redtube.com",
    "youporn.com", "spankbang.com", "tube8.com", "eporner.com", "tnaflix.com",
)


class AgeGateRequired(Exception):
    pass


def is_adult_host(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in ADULT_DOMAINS)


def is_adult(url: str, info: dict | None = None) -> bool:
    if info and (info.get("age_limit") or 0) >= 18:
        return True
    return is_adult_host(url)


def enforce_age_gate(url: str, info: dict | None, age_confirmed: bool, required: bool) -> bool:
    """Return whether the content is adult; raise AgeGateRequired if confirmation is missing."""
    adult = is_adult(url, info)
    if adult and required and not age_confirmed:
        raise AgeGateRequired("Confirm you are 18 or older to continue")
    return adult
