# Discord Voice Time Bot

Discord voice channel activity tracker that records how long members stay in voice channels and posts weekly or monthly summaries.

## Files

- `bot.py`: Discord bot entry point.
- `voice_tracker.py`: Member time tracking, weekly/monthly summary, and JSON persistence logic.
- `guild_settings.py`: Per-server channel settings storage.
- `.env`: Local bot settings. This file is ignored by Git.

## Environment

Create a `.env` file with these values:

```env
DISCORD_TOKEN=your_discord_bot_token
DEV_GUILD_ID=optional_test_guild_id
```

Channel IDs are configured inside Discord with slash commands:

- `/set_log_channel`
- `/set_settlement_channel`
- `/settings`

Server managers can migrate statistics with these slash commands:

- `/get_json`: Download the current server's latest statistics.
- `/upload_json file:<stats.json>`: Replace the current server's statistics with a UTF-8 JSON backup (maximum 5 MB).

## Run

```powershell
python bot.py
```
