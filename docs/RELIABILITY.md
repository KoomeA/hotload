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

## Containment (corrected after the launch review)
The first version of this section claimed the in-process guard covered redirects. **It did not**: it validated only the
first URL, and yt-dlp follows redirects (and re-resolves names) internally. That was reproduced and fixed. What protects you
now, and what each layer can and cannot do:

| Layer | Stops | Cannot see |
|---|---|---|
| `security.validate_url` on every submitted URL | blocklist, allowlist, private literals/names, bad ports, NAT64/6to4/Teredo forms | anything after the first hop |
| `netguard` connect-time guard (yt-dlp threads only) | **redirects to internal addresses, DNS rebinding, literal IPs**, for yt-dlp's own HTTP | FFmpeg, upstream proxies, other processes |
| `SafeYoutubeDL.dl` | every URL FFmpeg receives (video+audio inputs, `requested_formats`, `manifest_url`); non-http(s) schemes refused | what FFmpeg does after it starts |
| FFmpeg `-protocol_whitelist http,https,tcp,tls,crypto` | `file:`, `ftp:`, `rtsp:`, `concat:` references inside manifests | HTTP redirects FFmpeg follows itself |
| **Egress filter** (`deploy/docker-compose.hardened.yml`, `deploy/egress/squid.conf`) | everything that leaves through the proxy, including FFmpeg's redirects and manifest references | traffic that bypasses the proxy (see below) |

- **FFmpeg-based features (trimming, live recording) are only safe behind the egress filter.** On the hosted service they
  stay off (503) until you set `HOTLOAD_EGRESS_FILTERED=true`, which is your statement that the workers really are behind it.
- **Upstream proxies resolve target names themselves.** With `HOTLOAD_PROXY_URLS`, neither the in-process guard nor the egress
  filter sees the final destination: enforce a private-range deny list at the provider or the host firewall.
- Exemption for configured proxies is by exact host **and port**, and the `no_proxy` list is never treated as a proxy.
- Metadata fetches are bounded: a wall-clock deadline and a byte cap (`HOTLOAD_EXTRACT_DEADLINE_SECONDS`, `..._MAX_BYTES`)
  on their own bounded thread pool (`HOTLOAD_MAX_CONCURRENT_EXTRACTIONS`) that answers 503 when busy.
- Downloads are bounded by plan: `max_filesize` (per plan), a running byte count that aborts lying servers, FFmpeg `-t`/`-fs`,
  one item per URL (no playlist fan-out), and a post-download duration check (`ffprobe`) for sites that report no duration.
- Containers: read-only root, `cap_drop: ALL`, `no-new-privileges`, non-root user, and memory/CPU/PID limits. Not yet:
  a seccomp profile and per-job disk quotas (use a size-limited volume for `/data` and monitor it).

## Logs
JSON lines on stdout. Job events carry `job`, `url` (12-char hash), `state`, `seconds`, `attempts`, `adult`.
