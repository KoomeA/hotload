# Launch readiness

Status of the pre-launch review (2026-10-30) and what still has to happen before real users and real money.
Run `python -m hotload.preflight` against the production environment first: it reports the configuration mistakes below.

## 1. What the review did
Four independent reviews (accounts/sessions, billing, the fetch-and-process pipeline, client side) were commissioned from
fresh readers with no knowledge of my conclusions. Every finding was verified against the code before acting, several were
reproduced with an experiment, and each fix has a regression test. The reviews found real problems, including three that
contradicted things I had told you earlier. They are listed here so nobody relies on the old claims.

| Finding | Severity | Status |
|---|---|---|
| **SSRF guard did not cover redirects/DNS rebinding** (my M1 claim was wrong; reproduced: an internal server was reached) | High | Fixed: connect-time guard, every FFmpeg URL validated, FFmpeg protocol whitelist; FFmpeg features need the egress filter |
| Hardened overlay did not protect the API container; FFmpeg ignores upper-case proxy vars | High | Fixed (overlay rewritten); **untested: no container runtime here** |
| Cheaper plan could extend a higher plan (~395 Pro days for ~$114) | High | Fixed: such payments are held for review |
| Pre-hijack: unverified registration + OAuth link kept the attacker's password | High | Fixed: takeover on first verified login (password, sessions, keys, cookies cut off) |
| Metadata fetch could pin threads/memory (endless or dripping page) | High | Fixed: deadline + byte cap, bounded pool, 503 when busy |
| Duration/size limits bypassable (site reports no duration) | High | Fixed: per-plan size cap, running watchdog, FFmpeg -t/-fs, ffprobe check |
| X-Forwarded-For left-most entry trusted | Med/High | Fixed: Nth from the right, validated |
| One URL could download a whole channel/playlist | Med | Fixed: one item per URL |
| Late payments (after our window) never granted; payment for deleted account stuck; lost update on concurrent grants | Med | Fixed (grant late, hold, row lock / BEGIN IMMEDIATE) |
| Sessions not revocable; login limits too coarse; tokens in URLs/logs; job status leaked tokens | Med | Fixed (session epoch, 3-layer limits, redaction, owner-only status) |
| Thumbnails disclosed the visitor's IP to arbitrary hosts | Med | Fixed: signed same-origin proxy, CSP `img-src 'self'` |
| Service worker never served the offline shell | Low | Fixed (verified offline in a browser) |
| Registered emails can be enumerated (409 on register) | Low | **Open, accepted** until email verification exists |
| Bookmarklet sends the full page URL (incl. query) to your server | Low | Open: user-initiated, documented |
| `?url=` links auto-fetch (forced server-side fetch on the visitor's quota) | Low | Open, accepted (needed for share target / extension) |

Also found by testing, not by the reviewers: the `no_proxy` list was being treated as a proxy list (exempting loopback), a
hostname-only proxy exemption exposed every port on a loopback proxy, a test that "proved" bot tokens stay out of logs was
capturing nothing, and the pinned-IP technique fails behind proxies that refuse CONNECT to a bare IP.

## 2. Gates: do not launch publicly until these are true
Things that **only you / a staging environment can verify**. I could not do them here.

**Legal and money**
- [ ] Lawyer reviews Terms, Privacy, DMCA policy, refund policy (`docs/legal/` are drafts, not advice) and the adult-content model.
- [ ] DMCA designated agent registered (US) and an abuse/legal mailbox that is monitored; CSAM reporting procedure agreed.
- [ ] Payment processor decisions made (`docs/PAYMENTS.md`): Paystack's written answer; quotes from adult-friendly card processors.
- [ ] Tax treatment (VAT/digital services) decided; business bank account and payout flow in place.

**Infrastructure (staging first)**
- [ ] **Build the images and bring up the hardened overlay** (`docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.hardened.yml --profile hosted up`). It has never been run.
- [ ] **Prove the egress filter works** from inside a worker container: `curl -m 5 http://169.254.169.254/` and `curl -m 5 http://10.0.0.1/` must fail; `curl -m 5 https://example.com/` must work. Then submit a trim job whose media URL redirects to an internal address and confirm it fails. Only then set `HOTLOAD_EGRESS_FILTERED=true`.
- [ ] PostgreSQL (not SQLite), managed Redis, S3/R2 bucket (private), TLS via the edge proxy, backups of the database **and a restore drill**.
- [ ] Pin the squid and Caddy images to tested tags/digests; add memory/disk alerts; put `/data` (or the scratch volume) on a size-limited volume.
- [ ] Migrations: run `alembic upgrade head` once before starting several replicas (two simultaneous first runs can collide).

**Integrations with real services**
- [ ] Paystack **test mode** end to end (checkout, webhook, verify, return page). API details were cross-checked from third-party guides only.
- [ ] BTCPay staging store: invoice, testnet payment, `InvoiceSettled` grants the plan; a late/underpaid/expired invoice behaves as documented.
- [ ] OAuth (GitHub/Google) live; Telegram webhook registered and a test chat; S3/R2 upload, signed URL, ZIP stream, cleanup.
- [ ] Confirm YouTube and the other top sites work from your production egress IPs (datacentre IPs get blocked); budget proxies.

**Product gaps to decide on**
- [ ] **Email verification and password reset** (needs a mail provider). Until then: open registration is farmable and lost passwords are unrecoverable.
- [ ] Refund/chargeback handling is manual (held payments, provider dashboards). Decide who does it and how fast.
- [ ] Native-speaker review of the es/fr/de/pt/sw translations.

## 3. Hosted configuration (minimum)
```
HOTLOAD_QUOTAS_ENABLED=true
HOTLOAD_SECRET_KEY=<openssl rand -hex 32>
HOTLOAD_PUBLIC_URL=https://your.domain            # also makes session cookies Secure
HOTLOAD_DATABASE_URL=postgresql+asyncpg://...
HOTLOAD_REDIS_URL=redis://redis:6379/0
HOTLOAD_STORAGE_BACKEND=s3  (+ bucket, endpoint, keys, prefix)
HOTLOAD_TRUST_PROXY_HEADERS=true  HOTLOAD_TRUSTED_PROXY_HOPS=1   # = proxies in front of the API
HOTLOAD_METRICS_TOKEN=<random>
HOTLOAD_EGRESS_FILTERED=true                       # only after the filter is proven (section 2)
HOTLOAD_COOKIE_KEY=<python -m hotload.admin cookie-key>   # only if you offer saved cookies
```
Then: `python -m hotload.preflight` must report **0 failures**, and every warning should be one you have chosen to accept.

## 4. Operating it
- Alerts: canary down for a top site; queue depth; `payment HELD` and `payment amount mismatch` (ERROR logs); `report received kind=csam` (ERROR); webhook failure rate; 5xx rate; disk usage on `/data`.
- Weekly: review held payments and open reports; rotate nothing automatically (there is no key rotation tooling except `HOTLOAD_COOKIE_KEY` lists).
- Rollback: redeploy the previous image tag; migrations are additive.
- Admin: `python -m hotload.admin grant <email>` (server only); the admin console is `/admin/`.

## 5. Known limitations (honest list)
FFmpeg-based trimming/live recording depend on the network egress filter; upstream proxies resolve names themselves;
sessions are cookie-based (no device list UI, but `POST /api/v1/auth/logout-all` revokes all); quota windows are fixed 12 h;
no email; no automatic refunds; no per-job disk quota; no seccomp profile; performance and load have **not** been
tested beyond functional runs (no load test was done).
