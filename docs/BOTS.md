# Chat bots (Telegram and Discord)

Both bots share one core (`src/hotload/botcore.py`): the same rate limits, SSRF checks, takedown list, 18+ gate, anonymous-plan
quota and size/duration caps as the web app. Telegram is below; Discord is at the end.

## Telegram

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

## Discord

Slash commands over Discord's *interactions webhook*: no gateway connection, no extra process, and the bot never needs to be
online to be reachable. Every request is signed by Discord (Ed25519) and nothing is read until the signature checks out.

### Set up
1. In the [developer portal](https://discord.com/developers/applications) create an application. Copy the **Application ID**
   and the **Public Key** (General Information) and create a bot to get its **token**.
2. Set `HOTLOAD_DISCORD_PUBLIC_KEY`, `HOTLOAD_DISCORD_APPLICATION_ID` and `HOTLOAD_PUBLIC_URL` (public **HTTPS**: Discord only
   calls HTTPS endpoints, and replies carry absolute links).
3. Set *Interactions Endpoint URL* to `<PUBLIC_URL>/api/v1/bots/discord`. Discord sends a signed ping when you save it and
   refuses the URL unless it is answered correctly, so start the API first.
4. Register the commands once: set `HOTLOAD_DISCORD_BOT_TOKEN` in the shell you run this in (the running API never uses it),
   then `python -m hotload.admin discord-commands`. Global commands can take a while to appear.
5. Invite the bot with the `applications.commands` scope (plus `bot` if you want it in servers).
6. Optional: `HOTLOAD_DISCORD_ALLOWED_USERS=[123456789012345678]` restricts it to specific Discord user ids (others are told it
   is private; Discord requires an answer, so silence is not possible).

### Behaviour
- `/download link:<link> [format:<...>]`, `/preset format:<...>` (default 720p, per person), `/confirm18` (per person, 180 days),
  `/help`. One link per `/download`.
- **Every reply is private** (ephemeral) to the person who ran the command: download links carry a token, so they are never
  posted in a channel. No message can ping anyone (`allowed_mentions` is empty, since titles come from untrusted sites) and
  link previews are suppressed.
- The first answer is "thinking..." (Discord allows 3 seconds) which is then edited to "Queued"; when the job finishes the link
  is sent as a private follow-up through the interaction's own webhook.
- Limits are per Discord user id: 10 requests a minute, plus the anonymous plan's daily allowance when plans are enforced.

### Limits and honest notes
- **Discord keeps an interaction's webhook valid for 15 minutes.** A download that takes longer cannot report back (the user
  can run the command again; nothing is lost but the message). Live recordings are not offered through the bot.
- The interaction token (a short-lived secret for replying) is stored with the job in Redis for the job's lifetime, never
  returned by the API and never logged.
- Requests more than five minutes away from the server clock are refused, so keep the host's clock correct (NTP).
- Not tested against the live Discord API: the tests sign requests with a generated key and use a fake of the webhook API.
  Save the endpoint URL in the portal and run each command once after deployment. The command definitions were written from
  Discord's documented schema; `python -m hotload.admin discord-commands` prints Discord's answer if one is rejected.
- Discord's terms restrict adult content and automated tools in some settings: read the Developer Policy before enabling the
  bot for adult sites, and consider the allow-list.
