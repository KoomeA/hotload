# Hotload

Open-source video and audio downloader platform. Paste a link, pick a quality, get the file.
Built on [yt-dlp](https://github.com/yt-dlp/yt-dlp) with an async job queue, a clean web UI and a public API.

- **Hosted SaaS** and **self-hostable** from the same codebase (`docker compose up`).
- 1000+ sites via yt-dlp, including adult sites (age-gated, see [docs/COMPLIANCE.md](docs/COMPLIANCE.md)).
- Presets (best MP4, 1080p/720p/480p, MP3, M4A, Opus), trim/clip with a visual scrubber, subtitles (SRT/VTT, embedded or
  separate), multi-audio-track selection, playlists and batches (ZIP), live job progress.
- Installable PWA (share a link from your phone straight into Hotload), 6 languages, dark mode, keyboard accessible.
- Optional accounts (email or GitHub/Google), download history and plan limits (off by default when self-hosting).
- Also: SponsorBlock (mark or cut sponsor segments), live-stream recording, saved cookies for your own sessions
  (encrypted), a Chromium extension and bookmarklet (`extension/`), and a Telegram bot ([docs/BOTS.md](docs/BOTS.md)).
- Hosted-service features: prepaid Premium/Pro passes (Paystack cards/M-Pesa, crypto via BTCPay Server), API keys with
  usage metering and signed job webhooks, S3/R2 storage, priority queues, abuse reports and an admin takedown console.
  Read [docs/PAYMENTS.md](docs/PAYMENTS.md) before taking payments: processor policies matter for adult-site support.
- SSRF-hardened, rate limited, short-lived storage (files auto-delete).

Status: early MVP. See [docs/PLAN.md](docs/PLAN.md) for the roadmap.

## Quick start (self-host)

```bash
cp .env.example .env
docker compose -f deploy/docker-compose.yml up --build
# open http://localhost:8000
```

## Development

Backend (API + worker) and frontend (SvelteKit, in `frontend/`) run separately in development:

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest
redis-server &                       # or: docker run -p 6379:6379 redis:7
uvicorn hotload.api.app:app --reload  # API + web UI on :8000
arq hotload.worker.WorkerSettings     # worker

cd frontend && npm ci
npm run dev                           # http://localhost:5173, proxies /api to :8000
npm test                              # unit tests (vitest)
npm run build                         # static build in frontend/build
HOTLOAD_WEB_DIR=frontend/build uvicorn hotload.api.app:app   # serve the built app from the API
BASE=http://localhost:8000 node e2e/smoke.mjs                # browser end-to-end test (needs playwright + internet)
```

The M3 browser test (plans, checkout, API keys, reports, admin) runs against a stack with plans enforced and a local
stand-in for Paystack. Start from a **fresh Redis database** each time (sign-up and report rate limits are real):
```bash
python frontend/e2e/fake_paystack.py 9100 &
HOTLOAD_QUOTAS_ENABLED=true HOTLOAD_PAYSTACK_SECRET_KEY=sk_test_e2e HOTLOAD_PAYSTACK_BASE_URL=http://127.0.0.1:9100 \
  HOTLOAD_PUBLIC_URL=http://localhost:8001 HOTLOAD_SECRET_KEY=dev HOTLOAD_WEB_DIR=frontend/build \
  uvicorn hotload.api.app:app --port 8001 &
BASE=http://localhost:8001 PYTHON=python node frontend/e2e/m3.mjs
```
Make someone an admin (only from the server, never by email): `python -m hotload.admin grant you@example.com`.

## License

[AGPL-3.0-only](LICENSE). If you run a modified version as a network service you must publish your changes.
Commercial licensing for the hosted-service code is available from the maintainers.
