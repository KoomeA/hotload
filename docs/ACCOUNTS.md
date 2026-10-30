# Accounts, history and plans

## What exists
- Email + password accounts (argon2id), GitHub and Google sign-in, signed HttpOnly `SameSite=Lax` session cookie (30 days).
- Cross-site request protection: state-changing requests carrying an `Origin` that is not this site (or in
  `HOTLOAD_ALLOWED_ORIGINS`) get 403. Non-browser clients send no `Origin` and are unaffected.
- Login brute-force limit (10 tries / 15 min per address+email), sign-up limit (5 / hour per address), uniform error for
  wrong password vs unknown email.
- History (last 200 downloads per user: URL, title, site, preset, state). `DELETE /api/v1/me/history` clears it,
  `DELETE /api/v1/me` erases the account and its history. Anonymous downloads are never recorded.
- Plans (`src/hotload/tiers.py`): anonymous, free, premium, pro. Enforced only when `HOTLOAD_QUOTAS_ENABLED=true`:
  downloads per 12 h (fixed window per user, or per IP when anonymous), max media minutes (after trimming), batch size,
  video trimming. Billing only needs to set `users.tier` (`Database.set_tier`).

## Known limits (read before launching the hosted service)
- **Email verification and password reset need SMTP.** Set `HOTLOAD_SMTP_HOST`, `HOTLOAD_MAIL_FROM` and `HOTLOAD_PUBLIC_URL`
  (and, usually, `HOTLOAD_SMTP_USER`/`HOTLOAD_SMTP_PASSWORD`; `HOTLOAD_SMTP_PORT`, default 587, and `HOTLOAD_SMTP_SECURITY` =
  `starttls` (default), `ssl` or `none` for a local relay). Without them both features are off and a forgotten password
  cannot be recovered. Confirmed against a local SMTP sink in a browser; never against a real provider, so send yourself a
  test message first and check SPF/DKIM/DMARC for the sending domain or it will land in spam.
- OAuth sign-in only links to an existing account when the provider reports a verified email. A password account whose
  address was never confirmed is taken over (password, sessions, keys and cookies are cut off); one whose address was
  confirmed keeps its password and gains the provider as a second way in.
- Use PostgreSQL (`pip install asyncpg`) rather than SQLite for the hosted service. Migrations run on startup (Alembic).

### Email design
- Links are built from `HOTLOAD_PUBLIC_URL`, never from the request's `Host` header. The token is in the URL *fragment*
  (`/account/reset/#token=...`), so it is never sent to a server, proxy log or Referer header; the page removes it from the
  address bar after reading it, and confirms with a POST so link scanners that open mail links change nothing.
- Tokens are signed and purpose-bound (a verify token is useless for a reset). Verify links last 3 days; reset links 1 hour.
  A reset token is bound to the current password hash, so it works once and dies when the password changes.
- Resetting ends every session, signs the browser in, and counts the address as verified.
- "Forgot password" answers identically for known, unknown and malformed addresses and sends mail in the background (rate
  limited per address and per IP), so it cannot be used to find out who has an account.
- `HOTLOAD_REQUIRE_VERIFIED_EMAIL=true` stops unconfirmed password accounts from buying plans or creating API keys.
- **OAuth is tested with the provider calls mocked**, not against live GitHub/Google. Register an OAuth app with callback
  `<PUBLIC_URL>/api/v1/auth/oauth/<provider>/callback` and test the real flow in staging.
- Quota windows are fixed 12-hour windows, not rolling. Sessions cannot be revoked server-side except by rotating
  `HOTLOAD_SECRET_KEY` (which signs everyone out).
- Set `HOTLOAD_SECRET_KEY` and `HOTLOAD_COOKIE_SECURE=true` in production; without a key every restart signs users out.
