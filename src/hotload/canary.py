"""Synthetic checks that tell us a site's extraction broke *before* users do.

For each canary we (1) extract metadata, (2) check enough formats came back, and (3) probe the first bytes
of the best media URL (catches IP blocks and expired signatures that extraction alone does not). Results feed
`/api/v1/status`, Prometheus gauges and the CI gate used when bumping yt-dlp.

CLI (no Redis needed):  python -m hotload.canary --min-success 0.8 [--file canaries.json] [--json]
"""

import argparse
import asyncio
import json
import logging
import sys
import time
from importlib import resources
from pathlib import Path

import yt_dlp
import yt_dlp.version
from redis.asyncio import Redis
from yt_dlp.networking import Request

from . import engine, metrics
from .config import Settings, get_settings
from .logging_setup import log
from .proxies import NoProxyAvailable, ProxyPool

logger = logging.getLogger("hotload.canary")
LATEST_KEY = "hotload:canary:latest"
STREAK_KEY = "hotload:canary:streak"


def load_canaries(path: Path | None = None) -> list[dict]:
    text = path.read_text() if path else resources.files("hotload").joinpath("canaries.json").read_text()
    items = json.loads(text)
    for item in items:
        if not {"name", "site", "url"} <= item.keys():
            raise ValueError(f"canary needs name, site and url: {item}")
    return items


def _probe(url: str, headers: dict, settings: Settings, proxy: str | None) -> int:
    """Fetch the first KiB of a media URL; return the HTTP status."""
    opts = engine._base_opts(settings, proxy)
    with engine.SafeYoutubeDL(opts, settings) as ydl:
        req = Request(url, headers={**headers, "Range": "bytes=0-1023"})
        resp = ydl.urlopen(req)
        try:
            resp.read(1024)
            return resp.status
        finally:
            resp.close()


def check_one(canary: dict, settings: Settings, proxy: str | None = None) -> dict:
    started = time.monotonic()
    result = {"name": canary["name"], "site": canary["site"], "ok": False, "error": None, "formats": 0, "probe": None}
    try:
        with engine.SafeYoutubeDL({**engine._base_opts(settings, proxy), "skip_download": True}, settings) as ydl:
            info = ydl.extract_info(canary["url"], download=False)
        formats = [f for f in (info or {}).get("formats") or [] if f.get("url") and not f.get("has_drm")]
        result["formats"] = len(formats) or (1 if (info or {}).get("url") else 0)
        if result["formats"] < canary.get("min_formats", 1):
            result["error"] = f"only {result['formats']} formats"
        elif canary.get("probe", True):
            best = max(formats, key=lambda f: f.get("height") or 0) if formats else info
            if best.get("protocol", "https") in ("https", "http") and best.get("url"):
                result["probe"] = _probe(best["url"], best.get("http_headers") or {}, settings, proxy)
                result["ok"] = result["probe"] in (200, 206)
                if not result["ok"]:
                    result["error"] = f"media probe returned HTTP {result['probe']}"
            else:
                result["ok"] = True  # manifest-based: extraction success is the best cheap signal
        else:
            result["ok"] = True
    except Exception as exc:  # noqa: BLE001 - a canary must never crash the runner
        result["error"] = engine._clean(exc)[:200]
    result["seconds"] = round(time.monotonic() - started, 2)
    return result


async def run_canaries(settings: Settings, pool: ProxyPool | None = None, canaries: list[dict] | None = None) -> list[dict]:
    canaries = canaries if canaries is not None else load_canaries(settings.canary_file)
    pool = pool or ProxyPool.from_settings(settings)
    sem = asyncio.Semaphore(3)

    async def one(c):
        async with sem:
            try:
                proxy = pool.select(c["url"])
            except NoProxyAvailable:
                proxy = None
            res = await asyncio.to_thread(check_one, c, settings, proxy)
            if proxy:
                pool.report(proxy, res["ok"] or not engine._RETRYABLE.search(res["error"] or ""))
            return res

    return list(await asyncio.gather(*(one(c) for c in canaries)))


async def run_and_record(redis: Redis, settings: Settings, pool: ProxyPool | None = None) -> dict:
    results = await run_canaries(settings, pool)
    degraded = []
    for r in results:
        if r["ok"]:
            await redis.hset(STREAK_KEY, r["name"], 0)
            streak = 0
        else:
            streak = await redis.hincrby(STREAK_KEY, r["name"], 1)
        r["consecutive_failures"] = streak
        if streak >= settings.canary_degraded_after and r["site"] not in degraded:
            degraded.append(r["site"])
        await metrics.set_gauge(redis, "hotload_canary_up", 1 if r["ok"] else 0, site=r["site"], name=r["name"])
        await metrics.set_gauge(redis, "hotload_canary_seconds", r["seconds"], site=r["site"], name=r["name"])
        if not r["ok"]:
            log(logger, "canary failed", logging.WARNING, name=r["name"], error=r["error"])
    summary = {
        "checked_at": int(time.time()),
        "ytdlp_version": yt_dlp.version.__version__,
        "degraded": sorted(degraded),
        "results": [{k: r[k] for k in ("name", "site", "ok", "error", "seconds", "consecutive_failures")} for r in results],
    }
    await redis.set(LATEST_KEY, json.dumps(summary))
    await metrics.set_gauge(redis, "hotload_canary_last_run_timestamp", summary["checked_at"])
    return summary


async def latest(redis: Redis) -> dict | None:
    raw = await redis.get(LATEST_KEY)
    return json.loads(raw) if raw else None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Run Hotload extraction canaries")
    ap.add_argument("--file", type=Path, help="JSON canary list (default: built-in)")
    ap.add_argument("--min-success", type=float, default=0.8, help="exit 1 if the success ratio is lower")
    ap.add_argument("--json", action="store_true", help="print JSON instead of a table")
    args = ap.parse_args(argv)
    settings = get_settings()
    results = asyncio.run(run_canaries(settings, canaries=load_canaries(args.file or settings.canary_file)))
    ratio = sum(r["ok"] for r in results) / max(len(results), 1)
    if args.json:
        print(json.dumps({"ytdlp_version": yt_dlp.version.__version__, "success_ratio": ratio, "results": results}, indent=2))
    else:
        print(f"yt-dlp {yt_dlp.version.__version__}")
        for r in results:
            print(f"{'OK  ' if r['ok'] else 'FAIL'} {r['name']:<14} {r['seconds']:>6}s  {r['error'] or ''}")
        print(f"success ratio {ratio:.0%} (required {args.min_success:.0%})")
    return 0 if ratio >= args.min_success else 1


if __name__ == "__main__":
    sys.exit(main())
