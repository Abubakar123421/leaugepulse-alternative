# Website integration and restart checklist

Website: https://legacy-bot-three.vercel.app. Client presentation: `/demo`.

The optional `leaguebot.web_api` listener uses the same process, existing SQLite file, command handlers and permission checks as Discord. There is no separate website database. New `web_*` tables and notification triggers are additive; existing league records remain in place.

## Update safely

1. Keep a consistent backup of the existing database before updating. Use the normal hosting backup workflow; do not replace `data/leaguebot.sqlite3`, remove `data/`, rerun fresh setup or overwrite production secrets with `.env.example`.
2. The bot-hosting.net deployment is linked to this repository's `main` branch with auto-pull at restart. Check that the latest commit is selected. Restart through the hosting panel; the operator, not this repository update, controls the restart.
3. With `WEB_API_SECRET` absent, the website API remains disabled and the existing Discord workflow continues. With it configured, startup validates the secret, HTTPS origin and maximum five enabled guilds before binding the allocated port.
4. Check the startup logs and verify the bot comes online before connecting the website. Install the existing `requirements.txt`; no new dependency was added.

## Bot environment

Keep `DATABASE_PATH` pointed at the actual existing database (`data/leaguebot.sqlite3` in the inspected deployment). Never commit secret values.

- `WEB_API_SECRET`: a random secret of at least 32 characters, identical to Vercel's signing secret.
- `WEB_ORIGIN`: `https://legacy-bot-three.vercel.app`, without a trailing slash.
- `SERVER_PORT`: use the hosting-provided allocation. The inspected deployment's primary port is **25207**, not the example fallback 8080. Do not override the host-injected port with a copied template value.
- `WEB_GUILD_IDS`: optional comma-separated approved guild IDs; can be empty to register servers through the dashboard.
- `WEB_OPERATOR_IDS`: explicit Discord IDs allowed full-database backup/download, also requiring commissioner permission. A backup includes every server, so it must not be available to unrelated league commissioners.

Activate an HTTPS route in the hosting panel only when ready to expose the signed API. During inspection, Domains was available but not activated. `/health` reports readiness only; league APIs require signed requests. Do not expose the SQLite file, `.env` or backups through a static file server.

## Vercel and Discord OAuth

Configure server-side `APP_ORIGIN`, `BOT_API_URL`, `DISCORD_CLIENT_ID`, `DISCORD_CLIENT_SECRET`, `SESSION_SECRET` and `WEB_API_SECRET` on Vercel. Use the activated HTTPS bot origin for `BOT_API_URL` and independent 32+ character session/signing secrets. Do not use `VITE_` prefixes for secrets. The Discord application's OAuth redirect must be exactly:

`https://legacy-bot-three.vercel.app/api/auth/callback`

Redeploy the website after configuring its environment. Restarting only the Discord bot does not supply these settings.

## Access and verification

Appointed accounts `741159046819741708` and `1014858487970414663` can register server IDs and receive commissioner access on enabled leagues while they remain server members. The bot must already be in the server. Registration is private by default, persistent and audited. The total maximum is five across environment and dashboard registrations. Existing administrator/configured commissioner-role permissions continue working; real Discord identities are required, not usernames supplied in a form.

Verify in a test guild: Discord → website changes, website → Discord scheduling/result review, screenshot uploads, opposing-owner confirmation, commissioner's result correction, removed-role/member denial, cross-guild privacy, backups, and Twitch/YouTube title qualification. Existing commands retain their Discord-side delivery behavior; not every command is covered by a new universal outbox.

Before release, 141 tests pass using temporary databases and mocked Discord, plus Python compilation. Live OAuth, hosting HTTPS and full Discord delivery require account configuration and live acceptance checks.

## Disable / roll back

Remove `WEB_API_SECRET` and restart to disable website integration. Preserve the existing database. Restore the prior code release if necessary; additive website tables can remain. Restoring an older database backup loses changes made since the backup and must be a separate deliberate operator action.
