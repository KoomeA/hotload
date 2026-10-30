# Compliance and abuse policy (draft, needs legal review)

Hotload supports adult sites. That is lawful when operated responsibly; the following controls are required
before a public launch of the hosted service and are enabled by default in the code.

1. **Age gate.** Jobs for adult content (detected via yt-dlp `age_limit >= 18` or the adult domain list) require
   `age_confirmed=true` on the request; the UI shows an 18+ confirmation. Self-hosters can set `HOTLOAD_REQUIRE_AGE_GATE=false`.
2. **No hosting/indexing.** Files are processed transiently and deleted after `HOTLOAD_FILE_TTL_SECONDS` (default 1h).
   No public library, search or sharing of downloaded media.
3. **Blocklist.** `HOTLOAD_BLOCKED_HOSTS` and a maintained denylist (illegal content, DRM-only services).
   Admins can add entries without redeploying (planned M3).
4. **Abuse reporting.** Public report/DMCA contact, 24h takedown SLA, audit log of jobs (URL hash, not content).
5. **CSAM.** Zero tolerance. Report to NCMEC/IWF as required by jurisdiction, hash-list matching in M3. Do not store media.
6. **Privacy.** Minimal logs (hashed IPs, 30-day retention), GDPR/CCPA policy, no media retention.
7. **No DRM circumvention.** DRM-protected streams are refused.
8. **Terms.** Users are responsible for having the right to download content; ToS must say so.

Payment processors: disclose adult-site support to the high-risk processor during underwriting.
