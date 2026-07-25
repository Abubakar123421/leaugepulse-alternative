# bot-hosting.net Deployment and Transfer

The repository is prepared for a fresh database deployment and intentionally excludes `.env`, SQLite files, logs, PIDs, caches, backups, and virtual environments.

## Create the deployment

1. In bot-hosting.net, create an **Application** deployment.
2. Select GitHub as the source (or upload a ZIP) and choose Python 3.11 or newer.
3. Keep the project files in the deployment root.
4. Set **Entry File (`STARTUP_FILE`)** to `main.py`.
5. Keep `requirements.txt` in the root. The platform reads it during startup; do not type `pip install` into the console.
6. Add environment variables from `.env.example` in the host panel.
7. Start the deployment and inspect console logs for command sync and the connected guild count.

Official references:

- https://bot-hosting.net/docs/guides/create-a-server
- https://bot-hosting.net/docs/guides/set-up-a-server
- https://bot-hosting.net/docs/guides/clone-a-github-repository

## Required environment

```text
DISCORD_TOKEN=<new token>
DATABASE_PATH=data/leaguebot.sqlite3
BACKUP_DIR=data/backups
LOG_LEVEL=INFO
STREAM_POLL_SECONDS=180
REMINDER_POLL_SECONDS=300
AI_ENABLED=true
AI_DAILY_LIMIT=100
GEMINI_MODEL=gemini-2.5-flash
```

Optional secrets:

```text
GEMINI_API_KEY=
TWITCH_CLIENT_ID=
TWITCH_CLIENT_SECRET=
YOUTUBE_API_KEY=
```

Use the host’s secret/environment manager. Never upload a populated `.env`.

## First startup

No database is included. `Database.initialize()` creates `data/leaguebot.sqlite3` and applies the complete schema automatically. After the bot connects:

1. Run `/setup`.
2. Map existing permanent channels with the destination setters.
3. Import rosters.
4. Assign owners.
5. Import fixtures with `start_now:True`.

## Updating from GitHub

The bot-hosting.net GitHub sync offers replace and merge strategies. A replace-style sync can remove host-only runtime files. Before every update:

1. Stop or pause league-changing operations.
2. Run `/backup` and download the backup.
3. Save `data/leaguebot.sqlite3` through SFTP/file manager or platform backup.
4. Use merge-style sync for normal code updates.
5. Restart and check logs.
6. Run `/destinations` and a read-only command such as `/roster`.

Never store the production database or backup in Git.

## Transfer checklist

Transfer through GitHub or a ZIP containing source, CSV snapshots, templates, and Markdown guides. Exclude `.env`, `data/`, `.venv`, caches, and logs. Rotate developer-owned credentials after the new host is confirmed.
