# Telegram bot

Send the bot a link; it replies with a download link when the job finishes. Webhook driven: no extra process.

## Set up
1. Create a bot with [@BotFather](https://t.me/BotFather) and copy the token.
2. Set `HOTLOAD_TELEGRAM_BOT_TOKEN`, `HOTLOAD_TELEGRAM_WEBHOOK_SECRET` (any long random string) and
   `HOTLOAD_PUBLIC_URL` (must be public HTTPS: Telegram only calls HTTPS webhooks, and replies contain absolute links).
3. Register the webhook once: `python -m hotload.admin telegram-webhook`.
4. Optional: `HOTLOAD_TELEGRAM_ALLOWED_CHATS=[123456789]` restricts the bot to specific chat ids (others get silence).

## Behaviour
- Private chats: send a link, or `/mp3 <link>`, `/video <link>`. `/preset <key>` sets the chat's default (starts at 720p).
- Groups: only commands trigger it (`/mp3@YourBot <link>`), so it never reacts to every link people post.
- Up to 3 links per message. Per-chat limit of 10 requests a minute; when plans are enforced each chat gets the *anonymous*
  plan's daily allowance.
- Adult links: the chat must send `/confirm18` once (remembered for 180 days). The server's age gate otherwise applies.
- Same protections as the web app: SSRF checks, takedown blocklist, size/duration caps, files deleted after an hour.
- Replies are plain text. The bot token travels in Telegram API URLs, so the `httpx` logger is silenced to keep it out of logs.

## Limits and honest notes
- Not tested against the live Telegram API (tests use a faithful fake of `sendMessage` and the webhook shape). Do a test chat
  after deployment.
- The bot sends *links*, not files (Telegram's bot upload limit is small). Links work about an hour.
- Telegram restricts adult content on public bots in some regions and can ban bots; consider an allow-listed private bot for
  adult use, and read Telegram's Bot terms.
- Discord is not implemented. A Discord bot would be an interactions webhook with Ed25519 request verification
  (`pynacl` is the usual library) calling the same job-creation path as `api/bots.py`.
