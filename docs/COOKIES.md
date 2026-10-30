# Saved cookies (bring your own session)

Lets a signed-in user download content their *own* account can see (members-only, age-verified on the site, regional
logins) by saving that site's cookies in Hotload. Off unless `HOTLOAD_COOKIE_KEY` is set.

## How users use it
1. Export cookies for the site from their browser in Netscape `cookies.txt` format (common browser extensions do this).
2. Account page → Saved cookies → enter the site (for example `example.com`) and paste the file.
3. Tick "Use my saved cookies" when fetching/downloading a link from that site.

## What the server does (and refuses to do)
- **Encrypted at rest** with Fernet (`python -m hotload.admin cookie-key` makes a key). Several comma-separated keys are
  accepted for rotation: the first encrypts, all decrypt. Losing the key makes saved cookies unreadable (users re-upload).
- **Scoped at upload:** only cookies that would be sent to the stated site are kept; cookies for other sites, for a whole
  TLD (`.com`) or a public suffix (`.co.uk`, heuristic) are discarded. Malformed lines are dropped.
- **Opt-in per job**, only for that user, only when the job's host matches a saved site. Never attached implicitly.
- **Never returned, never logged.** The API shows site name, count and date only. Job JSON and history never carry them.
- **Private file lifecycle:** decrypted into a 0700 temp directory *outside* the output folder (so it cannot be zipped or
  served), passed to yt-dlp, deleted when the download ends, even on failure.
- Session-only management: API keys cannot upload, list or delete cookies. Deleted with the account.
- Hosted service: Premium and Pro only (when plans are enforced). Limits: 10 sites, 200 cookies per site, 100 KB per upload.

## Risks to understand before enabling
- You are holding people's login sessions. A database leak without the key is useless, but a leak *with* the key is not:
  keep `HOTLOAD_COOKIE_KEY` in a secret manager, separate from database backups.
- yt-dlp may rewrite the temp cookie file when a site rotates tokens; those refreshed values are discarded, so long-lived
  sessions may need re-uploading.
- Using someone's session may violate a site's terms. The feature is for a user's own account; the Terms must say so.
- Downloads that use cookies go out from your server's (or proxy's) IP, which some sites flag. Users should expect
  their session to be challenged or logged out by the site.
