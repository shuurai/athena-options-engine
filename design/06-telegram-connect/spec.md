# 06 · Telegram link

Web settings page. Not a left-nav item. Open it from Settings → Telegram identity.

The bot is created once on the Windows box. This page only binds a Telegram user.

## How a bot is configured

1. `@BotFather` → `/newbot`. Username must end in `bot`.
2. Token goes in `TELEGRAM_BOT_TOKEN` on the box. Never in the browser.
3. `/setdomain` registers the control-web host. Required for the login button.
4. Athena long-polls Telegram after the bots service restarts.

## How this page links a user

Two equivalent paths. Both end in the numeric user id on the allowlist.

- **Login widget** (on the registered domain). Button is “Log in with Telegram”. Widget returns `id`, `username`, `auth_date`, `hash`. The box checks `hash` with SHA-256 of the bot token. `write` access lets the bot message you.
- **Deep link.** One-time code, URL `https://t.me/<username>?start=<code>`. Telegram delivers `/start <code>`. Code is single use.

## States

- `not_configured` — token missing. Setup steps only. No login button.
- `waiting_link` — bot is up, nobody linked. Default mock.
- `linked` — username, user id, unlink.

This page does not Approve orders.
