# bot-hosting.net and Source Transfer

## Upload

Upload this project as a ZIP or Git repository. Do not include `.venv`,
`__pycache__`, `.pytest_cache`, or a real `.env` file.

Configure:

- Startup file: `main.py`
- Python version: 3.11 or newer
- Package file: `requirements.txt`
- Persistent directory: `data/`

Create environment variables from `.env.example`. `DISCORD_TOKEN` is required.
Twitch and YouTube values are optional. Secrets belong in the hosting control
panel, never in source files or support messages.

## Before transfer

Include:

- Complete source tree.
- `requirements.txt` and `.env.example`.
- The latest database backup, transferred privately.
- README and both guides.
- `Discord_League_Bot_Project_Plan.pdf`.

Do not include a live `.env` file. The recipient should create new credentials or
receive them through a secure password-sharing method. Revoke developer-owned
credentials after the handoff is confirmed.

## Restore

Place the selected backup at the path configured by `DATABASE_PATH` (the default
is `data/leaguebot.sqlite3`), restore environment variables, install packages, and
start `main.py`. Because Discord channel and role IDs are stored in the database,
the same database should only be restored for the same Discord servers.

