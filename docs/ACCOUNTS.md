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
- **No email verification or password reset yet** (no mail sender configured). A forgotten password cannot be recovered.
  OAuth sign-in only links to an existing account when the provider reports a verified email.
- **No database migrations yet.** Tables are created on startup (`create_all`). Add Alembic before the first schema change
  in production. Use PostgreSQL (`pip install asyncpg`) rather than SQLite for the hosted service.
- **OAuth is tested with the provider calls mocked**, not against live GitHub/Google. Register an OAuth app with callback
  `<PUBLIC_URL>/api/v1/auth/oauth/<provider>/callback` and test the real flow in staging.
- Quota windows are fixed 12-hour windows, not rolling. Sessions cannot be revoked server-side except by rotating
  `HOTLOAD_SECRET_KEY` (which signs everyone out).
- Set `HOTLOAD_SECRET_KEY` and `HOTLOAD_COOKIE_SECURE=true` in production; without a key every restart signs users out.
