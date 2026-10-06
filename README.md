# Delta Chat Bouncer Bot

Delta Chat bot designed to maintain group quality by monitoring inactivity and save server resouces by not sending mails to stale users. It scans group members and reports users who haven't been seen online for over 21 days.

## Features

- ⚠️ **Inactivity Reports (`/bounce`):** In groups with `/autokick` enabled, displays members approaching the auto-kick threshold (< 7 days or < 1 day remaining) as well as observation progress for silent members still in their grace period. In other groups, reports members inactive for over 21 days. Displays user role badges (`👑` Admin, `⭐` Autokick Ignored, `💤` Away) and member age indicators (colored circles `🔴`..`⚪` or squares `🟥`..`⬜` for multi-group members). Operates on an independent 60s cooldown.
- 🧹 **Automatic Inactivity Kick with Warnings (`/autokick`):** Automatically purge stale, inactive members from group chats in the background. Features a two-stage warning system: sends a private 1-on-1 direct message warning to inactive candidates and broadcasts a daily summary to the group (once every 24h). Members are kicked only after receiving a warning and passing a 24-hour grace period. Supports cryptographic fingerprint exemptions (`/autokick ignore`) and `/away` vacation status exemptions.
- 🛡️ **Auto-kick Fingerprint Ignore List (`/autokick ignore`):** Exempt specific members or service bots from auto-kick by resolving and storing their cryptographic key fingerprint.
- 👞 **Manual Member Kick (`/kick <userid>`):** Remove a specific member or multiple members from a group chat by contact ID (e.g. `/kick 123` or `/kick /contact123`), search query, or by replying to their message.
- 💤 **Away Status & Auto-Reply (`/away`, `/back`):** Set a temporary away message (e.g. `/away on vacation until Monday`). When other members mention you by name in group chats, the bot informs them privately of your away status. Bare `/away` displays your current status or usage instructions. Running `/back` clears away status and notifies users who messaged you.
- 📖 **Group Chat Catalog (`/chats`):** Users can browse all group chats cataloged by the bot, complete with name, description, and real-time membership count.
- 📢 **Delta Chat Channels Catalog (`/dchannels`):** Users can browse all channels cataloged by the bot, complete with name and description.
- 🌐 **Channel Web Preview & RSS Feeds (`/c/{token}`):** Public web previews for registered channels with live message history, attachments, accessible QR code join modals (with Tab focus trapping, Escape key support, focus restoration, and ARIA dialog attributes), inline ✓ Copy Link feedback (no blocking alerts), OpenGraph/Twitter Card metadata, zero-repaint background rendering, Light / Dark / System theme switcher with instant zero-transition toggling and `localStorage` persistence, and standard RSS 2.0 feeds (`/c/{token}/rss.xml`) featuring per-post permalinks (`#post-{msg_id}`), exact media enclosure byte sizes, and CDATA breakout escaping. Soft-delete tombstone handling ensures graceful messaging when channels are removed from the catalog.
- 🪐 **Fediverse / ActivityPub Federation (`@<token>@domain`):** Every registered channel acts as a full Fediverse actor (`type: "Service"` / 🤖 Bot). Users on Mastodon, Pleroma, Misskey, etc., can search `@<token>@<domain>` via WebFinger (`RFC 7033`), follow channels, and receive new messages and media attachments in their home feeds via authenticated HTTP Signatures (`RFC 9421` & `draft-cavage-http-signatures`) with strict KeyId/Actor origin binding, signing key ownership verification (key `owner`/`publicKey` cross-check), 300s anti-replay protection, foreign inbox delivery protection, and bounded backfill concurrency.
- 🔐 **Join Approval Workflows:** Supports public and private groups. Requests to join public groups immediately receive an invite link, while private groups (`🔐`) require approvals from existing members in the group via dynamic `/approve<ID>` commands.
- 👋🏻 **Custom Welcome Messages (`/welcome`):** Configure customizable welcoming greetings for new members joining the group with custom rules text and visual age badges.
- 🔗 **Invite Link (`/invite`):** Generate a SecureJoin invite link and QR code image for the current group chat. Available to all users with an independent 1-minute cooldown (admins are exempt). For private group chats, the generated link is single-use and will be automatically deleted from the chat once a new member joins.
- 🔍 **Member Search (`/search [email1] ...`):** Find group members by one or more email terms (case-insensitive substring matching) or by replying to a message containing email addresses. Displays user role badges (`👑`, `⭐`, `💤`), multi-chat indicators (`🟥`..`⬜`), and searches across all active transports (both primary and secondary addresses).
- 📬 **Relay Check (`/relays`):** Scan for group members using public Russian mail providers (Yandex, Mail.ru, etc.).
- 🏆 **Activity Ranking (`/top`):** Show the 10 most active members in the last 24 hours (independent 60s cooldown).
- 🏓 **ChatMail Ping (`/cmping`):** Ping mail relays (transports) to/from specified target servers using the `cmping` utility. Features real-time reaction-based progress tracking (`⏳`, `☑️`, `❌`) and runs asynchronously.
- 📞 **Echo Calls:** Call the bot from any Delta Chat app to test calls: it answers, plays a short two-tone greeting and echoes your voice back. After you hang up it sends a `📞 Echo call report` with the call duration, the media path (direct / peer-to-peer via STUN / TURN relay), how much of your voice it heard, packet loss, jitter and round-trip time. The WebRTC side is [`cmcall`](https://github.com/mrgluek/cmcall)'s `EchoPeer` (aiortc) running on a dedicated event-loop thread, so bot work never stalls call audio. Calls are capped (`CALL_ECHO_MAX_SECONDS`, default 300) and limited in parallel (`CALL_ECHO_MAX_CONCURRENT`, default 2; further callers get a "line busy" message). See [Echo Call Settings](#echo-call-settings).
- 📲 **Call Test Between Relays (`/cmcall`):** Runs the `cmcall` utility: a test profile on the first relay calls a profile on the second one, which echoes. Reports signaling delivery in both directions, the TURN servers used (media is forced through the relays' TURN), ICE connect time, echoed-audio round trip and RTP loss/jitter.
- 📞 **Call Monitoring Between Relays:** The call-side twin of the cmping monitor, over the same server list. Every hour (`CMCALL_MONITOR_INTERVAL`) one source relay calls every other relay with `cmcall` (TURN-only media, 5 s of audio); one call per pair covers both mail directions and both TURN servers, and the direction alternates each cycle. A lone relay calls itself. Hard failures are retried once and then raise a `📞🚨 Calls failing` alert in the `/cmreport` chats, edited in place when it resolves; packet loss ≥ `CMCALL_DEGRADED_LOSS_PCT` (10 %) in two checks in a row raises `📞⚠️ Calls degraded`. Relays without a TURN server are shown as "no TURN" and never alert. Quality is measured from the bot's host, so it never counts as an outage.
- 📡 **Server Connectivity Monitoring:** Automatic periodic monitoring of server connectivity using a round-robin algorithm. Employs an **incident-based alerting system** with in-place dynamic message editing (`🚨 Ongoing` → `⚠️ Ongoing (Partial Recovery)` → `✅ Resolved`) to prevent notification noise, along with accurate root-cause fault isolation. Configurable interval via `CMPING_MONITOR_INTERVAL` env var (default: 30 min).
- 👤 **Contact Sharing:** Reports include `/contact<ID>` links to quickly share a contact card for any user.
- 🔄 **Multiple Mail Relays:** Supports multiple mail servers. Relay selection and failover are handled by the Delta Chat core (2.61+), which sends via the newest relay first and falls back to the next one if a relay is unreachable.
- ⏳ **21-Day Grace Period:** The bot tracks group activity in the background and requires 21 days of observation before reporting "never seen" users.
- 🛡️ **Secure Administration & Rate Limiting:** Claim ownership with `/initadmin`. Admins bypass rate limits and have exclusive control over bot settings. All public web endpoints are rate-limited per client IP (with trusted reverse-proxy X-Forwarded-For extraction), returning HTTP 429 on excess. QR code cache is bounded at 200 entries (FIFO eviction).
- 📱 **QR Code Link:** Generates a SecureJoin QR code in the logs for easy device linking.
- 📋 **Startup Version Check:** Automatically checks and logs versions of Bouncer Bot, DeltaChat Core, RPC Client, `deltabot-cli`, `cmping` and `cmcall` at startup.
- 🦠 **VirusTotal Inspection (`/virus`):** Inspect links or attached files for malware, phishing, and security threats using the VirusTotal API v3. Supports direct URL scans (`/virus <url>`), replies to messages containing links, or replies to messages with attached files. Employs a global FIFO queue and rate limiter (1 check every 15 seconds) to strictly adhere to VirusTotal free tier limits, with live in-place message updates as scans complete.
- 🎨 **Sticker Creation (`/sticker`, `/stickernobg`):** Convert any image into a standard WebP sticker compatible with Delta Chat, Telegram, and Signal with proportional 512px dimension scaling and automatic EXIF orientation transpose. Triggered either by replying to an image with `/sticker` or `/stickernobg`, or by sending an image with the command in its caption. `/sticker` preserves the original image and background (5s cooldown), while `/stickernobg` (or `/sticker nobg`) uses `rembg` with the lightweight `u2netp` model (only 4.7 MB) and a 15s cooldown with a global concurrency lock. Images are automatically pre-scaled down to 512px *before* AI segmentation to minimize CPU and memory usage, and background removal runs in an isolated subprocess (`sticker_tool.py`) with disabled memory arenas, ensuring 100% of memory is immediately reclaimed by the OS and the main bot daemon remains at ~75–80 MB. Background removal can be disabled via `ENABLE_REMBG=false` or configured with other models via `REMBG_MODEL`. Downloaded models are cached persistently in `./data/u2net` so they are never re-downloaded across restarts or updates.
- ⚡ **High-Performance Architecture & Read Pool:** Optimized SQLite read connection pool (`_ReaderConnectionPool`) supporting concurrent non-blocking reads in WAL mode, eliminating N+1 connection overhead and reducing latency by >12x. Intensive I/O and media processing (VirusTotal inspection, channel post media ingestion, Pillow WebP image optimization, and QR code generation) are fully offloaded to asynchronous background worker threads (`asyncio.to_thread` and dedicated daemon workers), keeping the Delta Chat event loop completely non-blocking.
- 🐳 **Docker Ready:** Easy deployment using Docker Compose.

## Setup

1. **Clone the repository:**

   ```bash
   git clone https://git.gluek.info/gluek/deltachat_bouncer
   cd deltachat_bouncer
   ```

2. **Initialize Account:**
   Run the initialization command once to set up the bot's email and password:

   ```bash
   docker compose run --rm bot python bot.py init bot-email@example.com your_password
   ```

3. **Start the Bot:**

   ```bash
   docker compose up -d
   docker compose logs -f
   ```

   *Note: If it's a new account, a QR code will be printed to the logs for linking your Delta Chat device.*

4. **Claim Admin Ownership:**
   Send `/initadmin` to the bot in a private message to become the administrator.

## Commands

- `/bounce [username]` — Show user activity, or check inactive members in current group (Shows warning candidates and observation progress if `/autokick` is on, otherwise 21 days; displays role badges and age indicators).
- `/search [email1] ...` — Search for group members by one or more emails (case-insensitive substring match) or by replying to a message containing email addresses.
- `/away [message]` — Set away status or view current away status without clearing it.
- `/back` — Clear away status and notify users who messaged you while you were away.
- `/relays` — Find group members using public Russian mail providers.
- `/top` — Show the 10 most active members in the last 24 hours.
- `/invite` — Generate an invite link and QR code for this group.
- `/chats` — Show the catalog of registered group chats available to join.
- `/chat<ID> [message]` — Request an invite link to the group (Private chat only).
- `/dchannels` — Show the catalog of registered Delta Chat channels with invite links and web preview URLs.
- `/dchannel<ID>` — Request the invite link and web preview URL for a channel.
- `/cmping <server1> ...` — Ping mail relays to/from specified target servers (15s cooldown, domain-validated).
- `/cmcall <server1> [server2]` — Test a Delta Chat call from server1 to server2 (or within one server): signaling, TURN, audio echo and RTP statistics (60s cooldown, one test at a time).
- `/callstats` — Your recent echo calls to this bot; admins also get 24h/7d totals and the last 10 calls.
- 📞 **Call the bot** — It answers and echoes your audio, then sends call statistics into the chat.
- `/virus <url>` — Scan a URL, or reply to a message containing a link or attached file with `/virus` to inspect with VirusTotal (15s global rate limit).
- `/sticker` — Convert replied or attached image to a WebP sticker (preserves original image/background; 5s cooldown).
- `/stickernobg` — Convert replied or attached image to a WebP sticker with background removed (15s cooldown, serialized).
- `/slap [username]` — Slap a user with a large trout (or reply to a message; 15s cooldown).
- `/approve<ID>` — Approve a pending join request for a private group (Group chat only).
- `/decline<ID> [reason]` — Decline a pending join request for a private group with an optional reason (Group chat only).
- `/contact<ID>` — Share contact card for the given ID (e.g., `/contact123`).
- `/help` — Show available commands and bot information.
- `/donate` — Support project development ❤️
- `/initadmin` — Claim administrative ownership (private chat only).
- `/autokick [on/off/days/ignore/unignore]` — Configure auto-kick with warnings (default: 90 days, e.g. `/autokick 30`) or manage cryptographic fingerprint ignore list (`/autokick ignore <email/nick>`, `/autokick unignore <fp/email>`) (Admin only).
- `/kick <user_id>` — Remove a member from the current group by contact ID, search query, or message reply (Admin only).
- `/chatadd [description]` — Add the current group chat to the catalog (Admin only). Falls back to group description if not provided.
- `/chatremove` — Remove the current group chat from the catalog (Admin only).
- `/chatdesc<ID> <text>` — Update description of cataloged group chat (Admin only).
- `/dchanneladd <URL>` — Join and add a channel to the catalog as unlisted by default (Admin only).
- `/dchannelremove [ID]` — Remove a channel from the catalog (Admin only). If the channel author removes the bot from the channel, it is also automatically removed from the catalog.
- `/dchanneldesc<ID> <text>` — Update description of cataloged channel (Admin only).
- `/dchannelpub<ID>on` / `/dchannelpub<ID>off` — Toggle channel public catalog visibility (Admin only). Unlisted channels are hidden from the public catalog and landing page, but maintain active web preview URLs and RSS feeds.
- `/url [base_url]` — Show or set the base web URL used for public channel previews and RSS feeds (Admin only).
- `/private <on/off>` — Toggle cataloged chat privacy status (Admin only).
- `/welcome [on/off/on <text>]` — Configure welcome messages for new members (Admin only).
- `/transports` — Show configured mail relays & stats (Admin only).
- `/addtransport` — Add a backup mail relay (Admin only, private 1-on-1 chat only for credential security).
- `/rmtransport <addr>` — Remove a mail relay (Admin only).
- `/cmpingadd <server>` — Add a server to connectivity monitoring rotation (Admin only).
- `/cmpingdel <server>` — Remove a server from monitoring (Admin only).
- `/cmpinglist` — Show all monitored servers, pair count, and rotation info; each server also shows its call-monitoring badge (📞✅ / ⚠️ / 🚨 / ➖ no TURN / ⏭ skipped).
- `/cmpingstatus [server]` — Show full monitoring results sorted from newest to oldest, with an optional server filter.
- `/cmpingfail [server]` — Show currently failed links with an optional server filter.
- `/cmpingevents [id]` — Show CMPing incident log or detailed incident breakdown (aliases: `/cmpingincidents`, `/cmevents`).
- `/cmpinghistory [server]` — Show downtime records and outage durations for monitored servers (alias: `/cmhistory`).
- `/cmcallstatus [server]` — Call monitoring: per-relay call health (✅ ok, ⚠️ degraded, 🚨 failing, ➖ no TURN), TURN server, and the latest call per pair with RTT, loss and signaling time.
- `/cmcallhistory [server]` — Call incidents (failing / degraded episodes) with durations.
- `/cmcallskip [server]` — Exclude a server from call monitoring only, e.g. a mail server without calls (Admin only; no argument lists skipped servers). `/cmcallunskip <server>` puts it back.
- `/cmreport <on/off>` — Toggle monitoring alerts for current chat (Admin only).

Relay selection and failover are handled by the Delta Chat core (2.61+): it sends via the newest relay first and falls back to the next one if a relay is unreachable. `/transports` lists relays in that order. The former `/setprimary` and `/resilient` commands are deprecated and only reply with this explanation.

### Target-Specific Commands in Group Chats

In group chats where multiple bots are present, you can address this bot specifically to prevent other bots from responding. Append the `@boun` or `@stew` suffix to any command, for example:

- `/help@boun` or `/help@stew`
- `/stats@boun` or `/stats@stew`

A plain `/help` sent in a group chat is answered in a private 1:1 chat with the sender, so several bots don't flood the group with help texts. Use `/help@boun` to show the help in the group itself.

## Echo Call Settings

Environment variables (all optional):

| Variable | Default | Meaning |
|---|---|---|
| `CALL_ECHO` | `1` | Set to `0` to stop answering calls. |
| `CALL_ECHO_WHO` | `everybody` | `everybody` lets any non-blocked contact call (also contact requests); `contacts` only accepted contacts. Applied to the core's `who_can_call_me` at start. |
| `CALL_ECHO_DELAY` | `0` | Seconds of playback delay. `0` is a live echo (like Asterisk's `Echo()`); e.g. `1.5` lets you finish a sentence before hearing it. |
| `CALL_ECHO_MAX_SECONDS` | `300` | The bot hangs up after this many seconds. |
| `CALL_ECHO_MAX_CONCURRENT` | `2` | Parallel echo calls; extra callers are declined with a "line busy" message. |
| `CALL_ECHO_LOG_DAYS` | `30` | Days to keep the per-call statistics behind `/callstats`; `0` keeps no call history at all (callers still get their report). |
| `CALL_DEBUG_LOG` | off | Set to `1` to log aiortc/aioice at INFO for troubleshooting. This logs callers' IP addresses (every ICE candidate pair), so turn it off again afterwards. |
| `CALL_ECHO_STUN` | `auto` | STUN for echo calls. `auto` uses the relay's TURN server as STUN too, exactly like the Delta Chat apps, so the bot gets a public (server-reflexive) candidate through Docker's NAT and calls can go peer-to-peer; `off` keeps TURN only; or an explicit `host:port`. |
| `CMCALL_MONITOR_INTERVAL` | `3600` | Seconds between call-monitoring cycles; `0` disables. The first cycle starts a quarter interval (max 15 min) after boot, offset from cmping. |
| `CMCALL_MONITOR_DURATION` | `5` | Seconds of test audio per monitored call. |
| `CMCALL_DEGRADED_LOSS_PCT` | `10` | Packet loss (%) that, in two checks in a row, marks a relay's calls as degraded. |

Call media uses the TURN server the bot's relay announces (Delta Chat core's `ice_servers`), so the container needs no extra ports. Chatmail relays announce only TURN; the Delta Chat apps (libwebrtc) query that server as STUN as well, which aiortc does not do on its own — with `CALL_ECHO_STUN=auto` the bot does the same, so behind the Docker bridge NAT it still learns its public address and most calls connect peer-to-peer (hole punching), with TURN as the fallback for symmetric NAT / CGNAT. `cmcall` test profiles for `/cmcall` are cached in `./data/cmcall_cache` (mounted to `/root/.cache/cmcall`).

To test the echo service end to end from the command line: `cmcall <your relay> --to '<bot invite link>'` calls the bot, probes the echo and prints the bot's report.

## Echo Call Privacy

- **No recording.** Audio only passes through memory: decoded, queued for playback (about 100 ms plus `CALL_ECHO_DELAY`) and sent back. Nothing is written to disk and there is no speech recognition. The bot only measures loudness to tell the caller how much of their voice it heard.
- **Encryption.** Media is DTLS-SRTP end to end between the caller's app and the bot; TURN relays see only encrypted packets. The bot is the other end of the call, so it necessarily decrypts the audio to echo it — trust in an echo bot is trust in whoever runs it.
- **Call message.** The incoming call message carries the caller's SDP offer, including their ICE candidates (local and public IP addresses). The bot deletes it from its account as soon as the call is over instead of keeping it for `delete_device_after` (36 h).
- **Statistics.** `/callstats` keeps, per call: contact ID, chat ID, start time, duration, whether media connected, path type (direct / STUN / TURN), packet loss, RTT, jitter, seconds of detected voice and why the call ended — no audio, no IP addresses. Rows older than `CALL_ECHO_LOG_DAYS` (30) are deleted; `0` keeps none. The admin's `/callstats` lists recent calls by contact ID.
- **Logs.** The bot logs call IDs, contact IDs and durations. aiortc/aioice are kept at WARNING because at INFO they log every ICE candidate pair, i.e. callers' IP addresses; `CALL_DEBUG_LOG=1` re-enables that for troubleshooting only.
- **Call monitoring** (`/cmcall`, `/cmcallstatus`) uses the bot's own test profiles and stores only relay names and measurements.

## Admin Management

Admin functions can be performed directly through chat commands, or managed via the server CLI:

### Set Administrator

```bash
docker compose exec bot python set_admin.py --email your@email.com
```

### Transport (Mail Relay) CLI Initialization

Although we recommend using `/addtransport` in chat, you can also add a backup relay via the command line:

1. Stop the bot: `docker compose stop bot`
2. Add relay: `docker compose run --rm bot python bot.py init transport backup-email@example.com password`
3. Start the bot: `docker compose up -d`

## Storage Optimization & Cleanup

To keep server disk usage to an absolute minimum, the bot is configured to:

1. **Disable Auto-Downloads:** Disables downloading any attachments/media files automatically (`download_limit` is set to `1` byte). The bot only processes message text.
2. **Auto-Delete Old Messages:** Automatically deletes messages older than 36 hours (`delete_device_after` set to `36 hours` / 1.5 days) to prevent the main SQLite database (`dc.db`) from growing while preserving a buffer for `/top` 24-hour activity stats.

### Safe Disk Space Cleanup

If the bot's data directory has already grown due to old media attachments, you can safely delete all cached media files (blobs) on your host system without breaking the SQLite database:

```bash
# Clean up existing downloaded attachments/blobs
rm -rf /home/tgbridge/deltachat_bouncer/data/bouncer/accounts/*/dc.db-blobs/*
```

## Fediverse / ActivityPub Federation

Every channel registered in the Bouncer Bot automatically federates with the Fediverse (ActivityPub / ActivityStreams 2.0).

### Following a Channel from Mastodon / Fediverse

1. In your Mastodon (or Pleroma, Misskey, etc.) search bar, search for the channel handle:
   ```text
   @<channel_token>@dc.gluek.info
   ```
   *(For example: `@twniAE9eNajd@dc.gluek.info`)*

2. Click **Follow**. The bot will automatically accept your follow request.
3. When new messages and media (images, videos, files) are posted to the Delta Chat channel, they will automatically appear in your Mastodon home feed as rich posts.

### Supported Endpoints

- **WebFinger:** `/.well-known/webfinger?resource=acct:<token>@<domain>` (RFC 7033)
- **Actor Profile:** `/c/{token}` (Content Negotiation with `Accept: application/activity+json`, includes avatar icon and background wallpaper header banner)
- **Actor Inbox & Shared Inbox:** `/c/{token}/inbox` and `/inbox` (Receives `Follow`, `Undo`, and `Delete` activities; sends `Accept` and backfills up to 10 recent channel posts to the new follower's inbox)
- **Actor Outbox:** `/c/{token}/outbox` (Returns `OrderedCollection` / `OrderedCollectionPage` with recent channel notes)
- **Actor Followers & Following:** `/c/{token}/followers` (Returns follower count) and `/c/{token}/following`
- **Single Note:** `/c/{token}/posts/{msg_id}` (Direct post representation with ActivityStreams `@context`)
- **NodeInfo & Instance API:** `/.well-known/nodeinfo`, `/nodeinfo/2.0`, and `/api/v1/instance` (Instance metadata and stats for GoToSocial, Mastodon, and crawlers)

All outgoing federation deliveries are cryptographically signed using **HTTP Signatures** (`draft-cavage-http-signatures`) with individual RSA-2048 actor keypairs.

## Architecture

As of v2.15.0, the bot logic is split into focused modules instead of one monolithic file (`bot.py` is now the lifecycle entry point only — `on_init`/`on_start`/`__main__`):

- `config.py` — static configuration: env loading, logging, version, cooldown/constant tables, the `dc_cli` instance.
- `state.py` — mutable runtime state: locks, caches, the live bot handle. Other modules always read/write it as `state.<name>`.
- `dc_helpers.py` — generic Delta Chat RPC helpers (admin/fingerprint checks, `_send`/`_react`, the delayed-command debouncer, message-attachment extraction).
- `formatting.py` / `security.py` — Delta Chat markdown → HTML rendering; web-layer rate limiting and safe host/URL resolution.
- `cmcall_monitor.py` — periodic call monitoring between relays (round-robin cycle, health evaluation, `📞` alerts) and `/cmcallstatus`, `/cmcallhistory`, `/cmcallskip`, `/cmcallunskip`.
- `calls.py` — echo call service (`EchoCallManager`, `IncomingCall`/`CallEnded` raw-event hooks) and the `/cmcall` / `/callstats` commands.
- `moderation.py`, `transports.py`, `cmping.py` + `cmping_commands.py`, `channels.py`, `virustotal.py`, `stickers.py` — one module per subsystem, each owning its background workers and `/command` handlers.
- `commands.py` / `handlers.py` — the remaining general commands (`/help`, `/bounce`, `/search`, …) and the global Delta Chat event handlers (system messages, the catch-all `NewMessage` dispatcher).
- `web/` — the aiohttp channel-preview + ActivityPub server: `web/routes.py`, `web/ap_routes.py`, and `web/templates/` (landing page, channel preview, RSS feed, tombstone/404, shared theme snippets).

`bot.py` re-exports everything under its old name for backward compatibility (`import bot; bot.<name>` still works), but new code should import the owning module directly.

## Support & Development

If you find this bot useful, consider supporting its development:

- **Git:** [gluek/deltachat_bouncer](https://git.gluek.info/gluek/deltachat_bouncer)
- **Donations:** Use the `/donate` command in Delta Chat.
