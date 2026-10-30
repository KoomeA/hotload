# Hotload

Open-source video and audio downloader platform. Paste a link, pick a quality, get the file.
Built on [yt-dlp](https://github.com/yt-dlp/yt-dlp) with an async job queue, a clean web UI and a public API.

- **Hosted SaaS** and **self-hostable** from the same codebase (`docker compose up`).
- 1000+ sites via yt-dlp, including adult sites (age-gated, see [docs/COMPLIANCE.md](docs/COMPLIANCE.md)).
- Presets (best MP4, 720p, MP3...), trim/clip, subtitles, live job progress.
- SSRF-hardened, rate limited, short-lived storage (files auto-delete).

Status: early MVP. See [docs/PLAN.md](docs/PLAN.md) for the roadmap.

## Quick start (self-host)

```bash
cp .env.example .env
docker compose -f deploy/docker-compose.yml up --build
# open http://localhost:8000
```

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest
redis-server &                       # or: docker run -p 6379:6379 redis:7
uvicorn hotload.api.app:app --reload  # API + web UI on :8000
arq hotload.worker.WorkerSettings     # worker
```

## License

[AGPL-3.0-only](LICENSE). If you run a modified version as a network service you must publish your changes.
Commercial licensing for the hosted-service code is available from the maintainers.
