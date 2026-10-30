# Hotload implementation plan

Decisions (2026-10-30): adult sites supported at launch; hosted SaaS + self-host; Python/FastAPI + web PWA;
license AGPL-3.0-only (protects the hosted service from closed forks while staying open source);
proxies and a high-risk payment processor are budgeted.

## Architecture

Browser/API client -> FastAPI (REST + SSE) -> Redis (jobs, queue, rate limits) -> Arq workers (yt-dlp library + FFmpeg)
-> file storage with TTL (local disk now, S3/R2 backend in M3) -> signed/one-time download URL.

Code layout (`src/hotload`):

| Module | Purpose |
|---|---|
| `config.py` | Environment-driven settings |
| `security.py` | SSRF-safe URL validation, host deny/allow lists |
| `engine.py` | yt-dlp wrapper: extract, presets, trim, download with progress |
| `compliance.py` | Adult-content detection, age gate, blocklist |
| `jobs.py` | Job model + Redis store |
| `ratelimit.py` | Redis fixed-window limiter |
| `storage.py` | Output dir, TTL cleanup |
| `api/app.py` | HTTP API and static web UI |
| `worker.py` | Arq worker tasks (download, cleanup) |

## Milestones

### M0: Foundation (this commit)
- AGPL license, README, plan, compliance policy.
- Python package, config, CI (lint + tests).
- Engine wrapper with presets; SSRF guard; age gate; rate limit; job store.
- API: `POST /api/v1/extract`, `POST /api/v1/jobs`, `GET /api/v1/jobs/{id}`, `/events` (SSE), `/file`.
- Worker + Docker Compose; minimal static web UI.
- Unit/API tests (mocked engine).

### M1: Reliability (weeks 2-3) - implemented, see docs/RELIABILITY.md
- [x] Canary monitor (extract + media probe, degraded-site status, CLI gate), cron in worker.
- [x] Proxy pool: per-site routing, rotation, cooldown, retry on retryable failures.
- [x] Outbound SSRF protection, in layers (CORRECTED after the launch review: the first version only validated the
  first URL and did NOT stop redirects or DNS rebinding): pre-check of every URL, a connect-time address guard for yt-dlp's
  own traffic (`netguard`), validation of every URL handed to FFmpeg, and a network-layer egress filter overlay for FFmpeg.
- [x] Metrics (`/metrics`, Prometheus text) and structured JSON logs.
- [x] yt-dlp pinned (`constraints.txt`) with a daily bump PR gated by tests + canary.
- [ ] Still open: per-container resource limits/seccomp, FFmpeg wall-clock timeouts, canary run from the deployed
  environment in CI, alert rules shipped as code, S3-shared proxy health.

### M2: Product (weeks 3-6) - implemented
- [x] SvelteKit PWA (static build served by the API): share target, installable, 6 languages, dark mode, a11y basics.
- [x] Trim scrubber (audio and video), subtitles (SRT/VTT, embedded or separate, auto-captions, subtitles-only),
  multi-audio-track selection, playlist expansion and batches with a streamed ZIP.
- [x] Accounts (email+password, GitHub/Google), history, plan limits (see docs/ACCOUNTS.md).
- [ ] Still open: email verification and password reset (needs a mail provider), Alembic migrations, live OAuth test,
  native-speaker review of the es/fr/de/pt/sw translations, PWA offline page, axe/Lighthouse audits in CI,
  proxying thumbnails (they currently load from the origin site and reveal the visitor's IP to it).

### M3: Monetization and scale - backend implemented (see docs/PAYMENTS.md, docs/SCALING.md)
- [x] Processor research and decision (Paystack's policy conflicts with adult/download content: needs written approval).
- [x] Provider-agnostic billing, Paystack and BTCPay (crypto) providers, prepaid passes, verified idempotent webhooks.
- [x] Alembic migrations (0001-0003).
- [x] API keys, usage metering, signed job webhooks (SSRF-safe, IP-pinned).
- [x] Abuse tooling: public reports, admin blocklist/takedown (host, URL, media id), manual plan grants.
- [x] S3/R2 storage backend and priority queues.
- [ ] Not live-tested: Paystack and BTCPay (tests use faithful fakes), S3 (tests use moto), priority pools under load.
- [ ] Still open: adult-friendly card processor module (CCBill/Segpay/Verotel after quotes), refunds/chargeback handling,
  invoices/receipts by email (needs mail provider), tax (VAT) handling, queue-depth metric, autoscaling rules.

### M4: Differentiators - implemented (see docs/COOKIES.md, docs/BOTS.md, extension/README.md)
- [x] SponsorBlock (mark/remove), live-stream recording (FFmpeg-capped, plan-limited), encrypted saved cookies.
- [x] Chromium extension (MV3, minimal permissions) and a drag-to-bookmarks bookmarklet; share-target already existed.
- [x] Telegram bot (webhook, per-chat limits, 18+ confirmation, link replies).
- [ ] Not built: Discord bot, scheduled "record at time X"/wait-for-upcoming (would pin a worker for hours), Firefox/Safari
  extension builds and store listings, extension context-menu test (native menus cannot be driven by the test harness),
  localised extension UI, sending files (not just links) through Telegram.
- [ ] Not verified end to end: SponsorBlock against real YouTube segments (options and postprocessor definitions are tested,
  the live fetch was blocked from the test environment), the Telegram bot against the real Telegram API.

### Launch readiness (2026-10-30) - review done, gates remain (see docs/LAUNCH.md)
- [x] Four independent security reviews; every finding verified, fixed with regression tests, or recorded as accepted.
- [x] `python -m hotload.preflight` checks a deployment's configuration; drafts of Terms/Privacy/DMCA/Refund in docs/legal.
- [ ] Gates only you can pass: legal review, payment-processor decisions, staging with the hardened Compose overlay (never run),
  real Paystack/BTCPay/S3/OAuth/Telegram runs, email verification and password reset, load testing (none done).
