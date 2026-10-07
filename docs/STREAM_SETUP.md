# Gridiron Legends live notifications

Legacy announces a stream only if the streamer belongs to an approved team profile in
that Discord server, the account is registered, the stream is live, and its title
contains `Gridiron Legends` (case-insensitive). Other games/leagues are ignored.
Members do not have to authorize access to their accounts; only public streams are checked.

## Twitch credentials

1. Sign in at https://dev.twitch.tv/console/apps with the bot operator's Twitch account.
2. Verify the email address and enable two-factor authentication if Twitch requests it.
3. Register an application with a unique name, e.g. `Legacy Gridiron Legends`.
4. Use category **Application Integration** (or the closest available bot category),
   OAuth redirect URL `http://localhost`, and **Confidential** client type if offered.
   Legacy uses app credentials, so no member OAuth redirect is actually used.
5. Create the application, open **Manage**, and copy **Client ID**.
6. Use **New Secret** and copy the secret directly into the hosting environment manager.
7. Set `TWITCH_CLIENT_ID` and `TWITCH_CLIENT_SECRET` on bot-hosting.net.

Official instructions: https://dev.twitch.tv/docs/authentication/register-app/

## YouTube credentials

1. Sign in at https://console.cloud.google.com/ and create/select a project for Legacy.
2. Open **APIs & Services → Library**, find **YouTube Data API v3**, and enable it.
3. Open **APIs & Services → Credentials → Create credentials → API key**.
4. Edit the key and restrict its **API restrictions** to **YouTube Data API v3**.
   If the hosting provider supplies a stable outgoing IP, use that IP as an application
   restriction. Browser HTTP-referrer restrictions do not work for this server bot.
5. Copy the key directly into `YOUTUBE_API_KEY` on bot-hosting.net.
6. Leave `YOUTUBE_SEARCH_DAILY_LIMIT=90` unless Google has approved a larger quota.

Official setup: https://developers.google.com/youtube/v3/getting-started

Keys belong in the host environment manager (or an ignored `.env` for local testing),
never Discord, chat messages, GitHub, or screenshots. Restart Legacy after setting them.
One set of operator credentials serves all registered league members.

## Commissioner workflow

1. Assign a member a team with `/assign-team` if they do not already have an approved team.
2. Set the notification channel: `/setstreamchannel channel:#live-now`.
3. Register either or both accounts:
   `/registerstreams member:@Member twitch:theirusername youtube:https://www.youtube.com/@TheirHandle`
4. To remove an account: `/removestream member:@Member platform:Twitch` (or YouTube).
5. Member goes live with a title such as `Gridiron Legends | Week 4 | Away vs Home`.

Accepted Twitch input: username or Twitch channel URL.
Accepted YouTube input: @handle, /@handle URL, UC channel ID, /channel/ URL, or legacy /user/ URL.
YouTube handles require the configured API key to resolve; UC IDs can be registered before it is set.
Omitting a platform leaves that member's existing account unchanged.
Accounts remain guild-scoped; assigning an account already owned by another member in that guild is rejected.

Notifications include the member, their team, platform, actual title, clickable stream link,
and the current season/week matchup when available. Mentions are displayed without mass pings.
Setting the server's `streams` feature off disables monitoring for that server.

## Detection speed and quotas

- Twitch checks every `STREAM_POLL_SECONDS` (default 180 seconds), batching up to 100 accounts.
- YouTube shares a global live search for `"Gridiron Legends"` across registered channels;
  returned accounts and titles are independently checked before any announcement.
- With 90 searches/day, YouTube checks approximately every 16–18 minutes at the default
  service interval. Search indexing can add delay or omit results; very short streams may
  be missed. Private/unlisted streams are not discoverable by this public search.
- Daily search usage and pacing are persisted across restarts. Quota days reset at midnight
  Pacific time. Other tools using the same Google project also consume its quota.
- The current documented default search allowance is 100 calls/day. More than 50 search
  results requires extra pages, which consume additional budget. For faster checks, request
  a quota increase and then raise `YOUTUBE_SEARCH_DAILY_LIMIT`; raising the setting alone
  does not increase Google's allowance.

Quota: https://developers.google.com/youtube/v3/docs/search/list
Extension: https://developers.google.com/youtube/v3/guides/quota_and_compliance_audits

## Delivery and live verification

Each platform's live-session ID is stored per server/account. Repeated checks, title edits,
restarts, and removing/re-registering an account do not reannounce the same session.
A different session can be announced. A stream initially lacking the phrase can qualify
later if its live title changes to include it.

A definitive Discord permission/channel rejection releases the reservation for retry.
For an ambiguous send failure or process crash, the reservation is retained to avoid a
possible duplicate. This favors one notification per session; it can lose an alert during
such a failure, since Discord and SQLite cannot be committed atomically.

To verify on the host, use one registered account per platform:

1. Go live with a title without the phrase; verify no notification.
2. Change the live title to include `GRIDIRON LEGENDS`; allow for the check/indexing delay.
3. Verify member/team, title/link, and matchup information in `#live-now`.
4. Leave the stream live through another check and restart Legacy; verify no second post.
5. End the stream and start a new qualifying session; verify one new notification.

Automated tests simulate platform responses; real credentials and live sessions are
required to confirm production connectivity.
