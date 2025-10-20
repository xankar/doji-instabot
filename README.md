# Doji InstaBot

Discord bot that mirrors evening Instagram food posts into a Discord channel.

## Features

- Polls an Instagram account during evening hours and relays new posts to Discord.
- Handles single-image and multi-image (carousel) posts.
- Persists the most recent Instagram post it has mirrored to avoid duplicates.

## Prerequisites

- Python 3.10 or newer.
- Discord bot token with access to the target server/channel.
- Instagram credentials are not required, but public access to the profile is assumed. Private profiles require prior authenticated cookies dumped into the session (not yet automated).

## Quick Start

1. Install dependencies:
   ```bash
   python -m venv .venv
   source .venv/bin/activate  # Windows: .venv\Scripts\activate
   python -m pip install -r requirements.txt
   ```
2. Copy `.env.example` to `.env` and fill in your credentials.
3. Run the bot:
   ```bash
   python src/bot.py
   ```

## Configuration

`src/bot.py` reads its settings from environment variables (usually supplied via `.env`):

- `DISCORD_TOKEN` – Bot token created at https://discord.com/developers.
- `INSTAGRAM_USERNAME` – Instagram handle to watch (no leading `@`).
- `DISCORD_CHANNEL_ID` – Target Discord text-channel ID.
- `CHECK_INTERVAL_MINUTES` *(optional)* – How often to poll during evening hours. Default `20`.
- `EVENING_START`/`EVENING_END` *(optional)* – Window expressed as `HH:MM` in the configured timezone. Defaults `17:00` → `23:59`.
- `TIMEZONE` *(optional)* – IANA timezone string (e.g., `America/New_York`). Default `UTC`.
- `BACKFILL_ON_START` *(optional)* – Set to `1` to send the most recent Instagram post immediately on first run. Default disabled (seeds state silently).
- `INSTAGRAM_LOGIN_USERNAME` / `INSTAGRAM_LOGIN_PASSWORD` *(optional but recommended)* – Provide an Instagram login to avoid anonymous rate limits and access private profiles you follow.
- `INSTAGRAM_SESSION_USERNAME` / `INSTAGRAM_SESSION_FILE` *(optional)* – If you already exported a session (e.g., via Instaloader’s `615_import_firefox_session.py`), point the bot at it so it reuses those cookies. `INSTAGRAM_SESSION_FILE` is a full path; omit it to use Instaloader’s default session directory.

The bot persists the shortcode of the most recent mirrored post in `data/state.json`. Delete that file if you want to force a re-sync.

## Notes

- The monitored Instagram account must be publicly accessible or the Instaloader session must be authenticated by placing a valid cookie file in `~/.config/instaloader`. Supplying login credentials or pointing to an exported browser session via the environment variables above is the quickest path; see [Instaloader docs](https://instaloader.github.io/) for details and advanced session management.
- Discord enforces rate limits on message/attachment uploads. The bot batches all images from a post into a single message to stay within limits.
- To run the bot continuously, deploy it on a server or process manager (e.g., systemd, PM2, or a container orchestrator).
