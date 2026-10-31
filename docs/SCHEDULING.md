# Scheduled recording

Record a live stream *when it starts*, without anyone being there and without holding a worker while waiting.
Account page -> *Scheduled recordings*, or `POST /api/v1/schedules`.

## How it works
1. A schedule is a row in the database (`scheduled_recordings`): link, planned start, minutes, format. Waiting costs nothing.
2. A timer on the workers (every 30 s) looks at schedules whose planned start has passed and checks whether the stream is live,
   once per page however many people wait for it, and at most `HOTLOAD_SCHEDULE_PROBES_PER_TICK` (10) pages per tick, least
   recently checked first so none starves.
3. A live stream becomes an ordinary live-recording job (same code, caps and plan rules as *Record live stream*). One that has
   not started `HOTLOAD_SCHEDULE_GRACE_MINUTES` (30) after the planned time is marked *missed*. Pages that are offline or
   unannounced count as "not live yet", so channel-style sites (where the page only works while live) work too.
4. When it finishes the owner is emailed (if mail is configured) with a link to the account page. The email never contains the
   download link. The file stays available `HOTLOAD_SCHEDULED_KEEP_HOURS` (24) instead of the usual hour: it finishes while
   nobody is watching.

## Start time
Give a time with its timezone (the account page converts from the browser's), or leave it out to use the time the page announces
for itself (a YouTube premiere or scheduled stream: `release_timestamp`). Times must be 5 s to `HOTLOAD_SCHEDULE_HORIZON_DAYS`
(14) ahead. Right now? Use *Record live stream* instead.

## Limits (all enforced where the work happens, and again at release time)
- Needs an account. Hosted plans: Premium 3 and Pro 10 schedules waiting at once; free: none. Self-hosted:
  `HOTLOAD_MAX_SCHEDULED_PER_USER` (10). Server-wide: `HOTLOAD_MAX_SCHEDULED_TOTAL` (500).
- **At most `HOTLOAD_MAX_SCHEDULED_RUNNING` (default 2) scheduled recordings run at once**, so a popular stream cannot take
  every worker. A worker runs 4 jobs at once: keep this below your total slots (preflight warns). The rest wait, and are
  missed if the grace period runs out first. Recordings are not queued behind each other.
- Recording length: the plan's live limit (Premium 30 min, Pro 60 min; self-host `HOTLOAD_MAX_LIVE_MINUTES`). If the plan has
  changed since scheduling, the new limit applies; a lapsed plan fails the schedule with a message.
- Each release costs one download from the daily allowance, taken when the recording starts, not when it was scheduled.
- Same safeguards as everything else: takedown list and address checks are repeated at release time (DNS can change),
  and the hosted service needs the egress filter on (`HOTLOAD_EGRESS_FILTERED`), like live recording.
- Saved cookies and webhooks are not supported for scheduled recordings.

## Honest notes
- **Never run against a real live stream.** The tests cover the API, every branch of the release tick (with a fake liveness
  check), the worker's keep/free/email path, storage retention, the browser form, and one real arq worker firing the timer.
  Do a real one (a Twitch/YouTube channel that goes live) before announcing the feature.
- Recording starts up to ~30 s after the stream goes live (the polling interval) plus startup time, so the first seconds are
  missed. A shorter interval means more requests to the site.
- Each waiting schedule is a request to the site every 30 s from the planned start until it is live. With many schedules
  for different pages, raise `HOTLOAD_SCHEDULE_PROBES_PER_TICK` knowingly: sites rate-limit or block, and YouTube in
  particular blocks datacentre addresses (budget proxies).
- A worker that dies mid-recording loses it (the job fails or is retried by arq); its place in the running count frees itself
  once its planned end passes.
- Everything is in UTC on the server; only the browser form deals in local time.
