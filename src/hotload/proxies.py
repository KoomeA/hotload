"""Proxy pool with per-site routing, round-robin rotation and failure cooldown.

State is in-process (one pool per worker/API process). That is deliberate: it keeps the hot path free of
Redis round trips, and a failing proxy is quickly rediscovered by each process independently.
"""

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from .config import Settings

DIRECT = "direct"


def mask(proxy: str | None) -> str:
    """Return a log-safe label for a proxy URL (never includes credentials)."""
    if not proxy or proxy == DIRECT:
        return DIRECT
    parts = urlsplit(proxy)
    return f"{parts.scheme}://{parts.hostname}:{parts.port}" if parts.port else f"{parts.scheme}://{parts.hostname}"


class NoProxyAvailable(Exception):
    pass


@dataclass
class _State:
    failures: int = 0
    cooldown_until: float = 0.0


@dataclass
class ProxyPool:
    default: list[str] = field(default_factory=list)
    routes: dict[str, list[str]] = field(default_factory=dict)
    threshold: int = 3
    cooldown: int = 300
    clock: Callable[[], float] = time.monotonic
    _state: dict[str, _State] = field(default_factory=dict)
    _cursor: dict[str, int] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @classmethod
    def from_settings(cls, s: Settings) -> "ProxyPool":
        default = list(s.proxy_urls) + ([s.proxy_url] if s.proxy_url else [])
        return cls(default, dict(s.proxy_routes), s.proxy_failure_threshold, s.proxy_cooldown_seconds)

    def _candidates(self, host: str) -> list[str]:
        host = host.lower().rstrip(".")
        best = ""
        for suffix in self.routes:
            if (host == suffix or host.endswith("." + suffix)) and len(suffix) > len(best):
                best = suffix
        return self.routes[best] if best else self.default

    def select(self, url: str, exclude: set[str] | None = None) -> str | None:
        """Pick the next proxy for `url`: None means a direct connection.

        Healthy proxies rotate round-robin. If every candidate is cooling down, the one whose cooldown
        ends soonest is used (never silently go direct and expose the server IP). Raises
        NoProxyAvailable once all candidates are in `exclude` (already tried for this job).
        """
        host = urlsplit(url).hostname or ""
        candidates = self._candidates(host)
        if not candidates:
            return None
        exclude = exclude or set()
        untried = [p for p in candidates if p not in exclude]
        if not untried:
            raise NoProxyAvailable("All proxies were tried")
        now = self.clock()
        with self._lock:
            healthy = [p for p in untried if self._state.get(p, _State()).cooldown_until <= now]
            if healthy:
                start = self._cursor.get(host, 0)
                choice = min(healthy, key=lambda p: (candidates.index(p) - start) % len(candidates))
                self._cursor[host] = candidates.index(choice) + 1
            else:
                choice = min(untried, key=lambda p: self._state[p].cooldown_until)
        return None if choice == DIRECT else choice

    def report(self, proxy: str | None, ok: bool) -> None:
        key = proxy or DIRECT
        with self._lock:
            st = self._state.setdefault(key, _State())
            if ok:
                st.failures = 0
                st.cooldown_until = 0.0
            else:
                st.failures += 1
                if st.failures >= self.threshold:
                    st.cooldown_until = self.clock() + self.cooldown
                    st.failures = 0

    def health(self) -> list[dict]:
        now = self.clock()
        every = {p for ps in [self.default, *self.routes.values()] for p in ps}
        with self._lock:
            return [
                {"proxy": mask(p), "cooling_down": self._state.get(p, _State()).cooldown_until > now}
                for p in sorted(every)
            ]

