"""Launch preflight: inspect the effective configuration and report what is unsafe or incomplete for the mode it runs in.

    python -m hotload.preflight            # reads the same HOTLOAD_* environment as the app
    python -m hotload.preflight --json

Exit status is 1 if any FAIL is reported (so it can gate a deploy), 0 otherwise. "Hosted" means plans are enforced
(HOTLOAD_QUOTAS_ENABLED=true): the public, multi-tenant, paid service. Self-hosting is judged more leniently.

Every check here exists because the launch review found the corresponding mistake to be dangerous. The checks read
configuration only; they never connect to Redis, the database, payment providers or the internet.
"""

import argparse
import json
import shutil
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass

from .config import Settings, get_settings

FAIL, WARN, OK, INFO = "FAIL", "WARN", "OK", "INFO"


@dataclass
class Finding:
    level: str
    topic: str
    message: str


def _hosted(s: Settings) -> bool:
    return s.quotas_enabled


def check(s: Settings, which: Callable[[str], str | None] = shutil.which) -> list[Finding]:
    f: list[Finding] = []

    def add(level: str, topic: str, message: str) -> None:
        f.append(Finding(level, topic, message))

    hosted = _hosted(s)
    add(INFO, "mode", "hosted service (plans enforced)" if hosted else "self-hosted (no plan limits)")

    # --- things that must never be on
    if s.allow_private_targets:
        add(FAIL, "ssrf", "HOTLOAD_ALLOW_PRIVATE_TARGETS is on: the outbound address guard is disabled. Test/dev switch only.")
    if not s.require_age_gate:
        add(FAIL if hosted else WARN, "compliance", "HOTLOAD_REQUIRE_AGE_GATE is off: adult content needs no confirmation (docs/COMPLIANCE.md).")

    # --- sessions and cookies
    key = s.secret_key or ""
    if not key:
        add(FAIL if hosted else WARN, "secrets", "HOTLOAD_SECRET_KEY is empty: sessions reset on every restart and break across replicas.")
    elif len(key) < 32:
        add(WARN, "secrets", "HOTLOAD_SECRET_KEY is shorter than 32 characters: use `openssl rand -hex 32`.")
    else:
        add(OK, "secrets", "session secret is set.")
    https = (s.public_url or "").lower().startswith("https://")
    if hosted and not https:
        add(FAIL, "transport", "HOTLOAD_PUBLIC_URL is not an https:// address: cookies, payments and OAuth need HTTPS.")
    if s.secure_cookies:
        add(OK, "cookies", "session cookies are Secure.")
    else:
        add(FAIL if hosted else WARN, "cookies", "session cookies are NOT Secure (set HOTLOAD_PUBLIC_URL to your https address).")

    # --- data stores
    if s.db_url.startswith("sqlite"):
        add(FAIL if hosted else INFO, "database", "SQLite in use. Hosted service: use PostgreSQL (HOTLOAD_DATABASE_URL, pip install 'hotload[postgres]').")
    else:
        add(OK, "database", "a server database is configured.")
    if s.redis_url.startswith("redis://localhost") or s.redis_url.startswith("redis://127."):
        add(WARN if hosted else INFO, "redis", "Redis is on localhost: fine for one host, not for replicas.")
    if hosted and s.storage_backend != "s3":
        add(WARN, "storage", "local file storage: API and workers must share one disk. Use S3/R2 to run replicas (docs/SCALING.md).")
    if s.storage_backend == "s3" and not (s.s3_bucket and s.s3_prefix.strip("/")):
        add(FAIL, "storage", "S3 storage needs HOTLOAD_S3_BUCKET and a non-empty HOTLOAD_S3_PREFIX (cleanup deletes under the prefix).")

    # --- reverse proxy / client IP
    if hosted and not s.trust_proxy_headers:
        add(WARN, "proxy", "HOTLOAD_TRUST_PROXY_HEADERS is off: behind a reverse proxy every visitor shares one IP, so IP limits and anonymous quotas collapse.")
    if s.trust_proxy_headers:
        add(INFO, "proxy", f"client IP = entry {s.trusted_proxy_hops} from the right of X-Forwarded-For: it must equal the number of proxies you run.")

    # --- egress
    if hosted and not s.egress_filtered:
        add(WARN, "egress", "HOTLOAD_EGRESS_FILTERED is off: trimming and live recording are disabled (503). Deploy the egress filter, then set it.")
    elif s.egress_filtered:
        add(INFO, "egress", "egress filtering is declared on: verify that workers can only reach the internet through the filter.")
    if s.proxy_url or s.proxy_urls or s.proxy_routes:
        add(WARN, "proxies", "upstream proxies resolve names themselves: enforce a private-range deny list at the provider or firewall.")

    # --- operations
    if hosted and not s.metrics_token:
        add(INFO, "metrics", "no HOTLOAD_METRICS_TOKEN: /metrics is disabled in hosted mode (set one to scrape it).")
    if not s.canary_enabled:
        add(WARN, "canary", "canaries are off: you will learn a site broke from users.")
    for tool in ("ffmpeg", "ffprobe"):
        if which(tool) is None:
            add(FAIL if tool == "ffmpeg" else WARN, "tools", f"{tool} not found on PATH" + (" (required)" if tool == "ffmpeg" else ": duration verification is skipped"))
    if s.metrics_token and len(s.metrics_token) < 16:
        add(WARN, "metrics", "HOTLOAD_METRICS_TOKEN is short.")

    # --- payments
    providers = [n for n, on in (("paystack", bool(s.paystack_secret_key)),
                                 ("btcpay", all((s.btcpay_url, s.btcpay_store_id, s.btcpay_api_key, s.btcpay_webhook_secret)))) if on]
    if hosted and not providers:
        add(WARN, "payments", "no payment provider configured: plans cannot be bought (docs/PAYMENTS.md).")
    if "paystack" in providers:
        add(WARN, "payments", "Paystack is on: its policy bars adult content and copyright-infringing items. Have written approval first (docs/PAYMENTS.md).")
    if s.btcpay_url and not s.btcpay_url.lower().startswith("https://"):
        add(FAIL, "payments", "HOTLOAD_BTCPAY_URL is not https: the API key would travel unencrypted.")
    if s.paystack_secret_key and s.paystack_secret_key.startswith("sk_test"):
        add(WARN if hosted else INFO, "payments", "Paystack is using a TEST key.")
    for cur in s.prices:
        if cur.upper() not in ("USD", "KES", "NGN", "GHS", "ZAR"):
            add(INFO, "payments", f"prices set for {cur.upper()}: Paystack will not offer it (subunit handling unconfirmed); BTCPay will.")

    # --- optional features
    if s.cookie_key:
        add(WARN, "cookies-vault", "saved cookies are on: keep HOTLOAD_COOKIE_KEY in a secret manager, apart from database backups (docs/COOKIES.md).")
    if s.telegram_bot_token and not (s.telegram_webhook_secret and https):
        add(FAIL, "bot", "Telegram bot needs HOTLOAD_TELEGRAM_WEBHOOK_SECRET and an https HOTLOAD_PUBLIC_URL.")
    if s.github_client_id or s.google_client_id:
        add(INFO, "oauth", "OAuth is on: register callbacks at <PUBLIC_URL>/api/v1/auth/oauth/<provider>/callback and test live.")

    # --- known gaps that are not configuration (so the checklist is honest)
    if hosted and s.registration_open:
        add(WARN, "accounts", "registration is open and email is not verified (no mail provider yet): free accounts can be farmed, passwords cannot be reset.")
    return f


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Hotload launch preflight")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    findings = check(get_settings())
    if args.json:
        print(json.dumps([asdict(x) for x in findings], indent=2))
    else:
        order = {FAIL: 0, WARN: 1, INFO: 2, OK: 3}
        for x in sorted(findings, key=lambda x: order[x.level]):
            print(f"{x.level:<5} {x.topic:<14} {x.message}")
        counts = {lvl: sum(1 for x in findings if x.level == lvl) for lvl in (FAIL, WARN)}
        print(f"\n{counts[FAIL]} failure(s), {counts[WARN]} warning(s)")
    return 1 if any(x.level == FAIL for x in findings) else 0


if __name__ == "__main__":
    sys.exit(main())
