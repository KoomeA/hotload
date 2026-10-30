# Reliability runbook

Extractors break whenever a site changes. Hotload's answer is to detect it first, retry through other routes, and
make upgrading the engine boring.

## Detection: canaries
- The worker runs `hotload.canary` every 15 minutes (`HOTLOAD_CANARY_ENABLED`). Each canary extracts metadata,
  checks the format count and probes the first KiB of the best media URL (catches IP blocks/expired signatures).
- Built-in list: `src/hotload/canaries.json` (general sites). **Adult-site canaries are deployment-specific**: put them
  in your own JSON file and set `HOTLOAD_CANARY_FILE`. Format: `[{"name","site","url","min_formats","probe"}]`.
- A site is **degraded** after `HOTLOAD_CANARY_DEGRADED_AFTER` (default 2) consecutive failures. `GET /api/v1/status`
  lists degraded sites and the UI shows a banner.
- `GET /metrics` (Prometheus text; protect with `HOTLOAD_METRICS_TOKEN`): `hotload_canary_up{site,name}`,
  `hotload_canary_seconds`, `hotload_canary_last_run_timestamp`, `hotload_jobs_total{state,preset,adult}`,
  `hotload_job_seconds_{sum,count}`, `hotload_job_errors_total{retryable}`, `hotload_extract_total{result,site}`.

Suggested alerts: `hotload_canary_up == 0` for 30m on any top-5 site; `time() - hotload_canary_last_run_timestamp > 3600`;
error ratio `rate(hotload_jobs_total{state="error"}) / rate(hotload_jobs_total) > 0.3` for 15m.

## Resilience: proxies and retries
- `HOTLOAD_PROXY_URLS` / `HOTLOAD_PROXY_ROUTES` configure the pool. Routing picks the longest matching host suffix.
- A retryable failure (403/429/5xx, timeouts, "Sign in to confirm") benches that proxy after
  `HOTLOAD_PROXY_FAILURE_THRESHOLD` strikes for `HOTLOAD_PROXY_COOLDOWN_SECONDS` and retries on another route, up to
  `HOTLOAD_MAX_ATTEMPTS`. A fully benched pool uses the proxy that recovers soonest; it never silently goes direct.
- Pool state is per process. Proxy credentials are never logged (`mask()`), and URLs are logged only as hashes.

## Upgrades: pin, gate, roll back
- `constraints.txt` pins yt-dlp; Docker and CI install with it.
- `.github/workflows/yt-dlp-bump.yml` checks for a new release daily, runs tests plus the canary gate and opens a PR.
  Merging triggers your normal image build; tag images with the yt-dlp version (e.g. `hotload:1.4.2-ytdlp2026.10.26`).
- **Rollback:** redeploy the previous image tag (or revert the bump PR). Watch the canaries for 15 minutes after each deploy.
- Hot-fixing one site without a release: use a yt-dlp plugin (`yt_dlp_plugins/`) rather than forking.

## Containment
- In-process guard (`engine.SafeYoutubeDL`): every request yt-dlp makes, and the media URL handed to FFmpeg for trims,
  must resolve to a public address and not be blocklisted. Limitation: FFmpeg follows redirects itself and DNS can change
  between check and use (rebinding).
- Network guard (`deploy/docker-compose.hardened.yml` + `deploy/egress/squid.conf`): worker is on an internal-only network
  and reaches the internet only through a proxy that denies private/metadata ranges. Caveat: with upstream proxies
  configured the worker bypasses it; enforce the deny-list at the provider or host firewall.
- Worker container: read-only root, `cap_drop: ALL`, `no-new-privileges`, non-root user. Still to do: CPU/memory/pids
  limits per deployment, and a seccomp profile.

## Logs
JSON lines on stdout. Job events carry `job`, `url` (12-char hash), `state`, `seconds`, `attempts`, `adult`.
