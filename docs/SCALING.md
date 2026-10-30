# Scaling and operations

## Components and what scales
| Component | Scale by | State |
|---|---|---|
| API (`uvicorn`) | more replicas behind a load balancer | none (Redis + DB + object store) |
| Workers (`arq`) | more replicas; one pool per queue | none when `HOTLOAD_STORAGE_BACKEND=s3` |
| Redis | one instance (managed Redis recommended) | jobs, queues, rate limits, counters |
| Database | PostgreSQL (`pip install "hotload[postgres]"`) | users, payments, keys, blocklist |
| Files | S3 / R2 / MinIO | short-lived (TTL, default 1 h) |

With **local** storage the API and workers must share one disk (a single host or a shared volume). With **S3/R2** workers
upload the finished file and delete their local copy, downloads are 15-minute signed redirects, and batch ZIPs stream from
the bucket, so replicas can run anywhere.

## Priority queues
When `HOTLOAD_QUOTAS_ENABLED=true`, Premium/Pro jobs are enqueued on `hotload:priority`; everyone else uses the default queue.
Run separate worker pools:
```bash
arq hotload.worker.WorkerSettings                                   # default queue (free + anonymous)
HOTLOAD_WORKER_QUEUE=hotload:priority arq hotload.worker.WorkerSettings   # paying plans
```
Size the priority pool for paying traffic so free-tier bursts never delay it. Webhook deliveries follow the queue of the job
that triggered them. Self-hosters without plans enforced use only the default queue (`worker-priority` is behind the
`hosted` Compose profile).

## Autoscaling
Scale workers on queue depth, not CPU (jobs are bursty and network-bound). Per-process proxy health means each worker learns
about a bad proxy on its own; keep pools of at least 2-3 workers per queue.
A simple signal: Redis `ZCARD arq:queue` and `ZCARD hotload:priority` (arq queues are sorted sets). Alert when waiting
jobs exceed ~2x `max_jobs` x worker count for 5 minutes. Note: the exported `/metrics` does not yet include queue depth.

## Object storage notes
- Cloudflare R2: endpoint `https://<account-id>.r2.cloudflarestorage.com`, region `auto`, S3 API token with object read/write
  on the bucket. Files expire via the worker's cleanup cron (every 10 minutes); add a bucket lifecycle rule (1 day) as a second layer.
- Keep the bucket **private**. Access is only through short-lived signed URLs minted after the job token is checked.
- Signed URLs are bearer links: anyone holding one can download until it expires (`HOTLOAD_S3_PRESIGN_SECONDS`, default 900).

## Operations checklist
- Migrations run on startup (API and worker). With several replicas, start one first or run `python -c "..."` once to avoid
  racing; Alembic is safe on PostgreSQL but two simultaneous first-time upgrades can collide.
- Back up the database (users, payments, keys). Redis and the bucket hold only transient data.
- Alerts: canary down, queue depth, `hotload_webhooks_total{result="failed"}`, payment amount-mismatch log lines (`ERROR`), CSAM
  report log lines (`ERROR`, `report received` with `kind=csam`), and 5xx rate.
