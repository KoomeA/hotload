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
- [x] Outbound SSRF guard in the engine (covers redirects yt-dlp follows and FFmpeg trim URLs) + egress-filter overlay.
- [x] Metrics (`/metrics`, Prometheus text) and structured JSON logs.
- [x] yt-dlp pinned (`constraints.txt`) with a daily bump PR gated by tests + canary.
- [ ] Still open: per-container resource limits/seccomp, FFmpeg wall-clock timeouts, canary run from the deployed
  environment in CI, alert rules shipped as code, S3-shared proxy health.

### M2: Product (weeks 3-6)
- SvelteKit PWA replacing the static UI (share target, i18n, dark mode, a11y).
- Visual trim scrubber, subtitles download/convert, multi-audio tracks, playlist/batch to ZIP stream.
- Accounts (email + OAuth), history, quotas.

### M3: Monetization and scale (weeks 6-10)
- Free/Premium/Pro tiers, high-risk payment processor integration, webhooks for billing.
- API keys, usage metering, webhooks on job completion.
- S3/R2 storage backend, autoscaling workers, priority queues per tier.
- Abuse tooling: admin takedown/blocklist UI, hash-list checks, audit log.

### M4: Differentiators (10+ weeks)
- Browser extension + bookmarklet, Telegram/Discord bot, live-stream DVR, BYO cookies (encrypted), SponsorBlock.

## Non-negotiables
1. No DRM circumvention. 2. No long-term media storage. 3. SSRF protection on every outbound fetch.
4. Age gate + CSAM/abuse reporting path before public launch. 5. Pin and canary every yt-dlp upgrade.
