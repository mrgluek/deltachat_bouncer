# Delta Chat Bouncer Bot

Delta Chat bot designed to maintain group quality by monitoring inactivity and save server resouces by not sending mails to stale users. It scans group members and reports users who haven't been seen online for over 21 days.

## Features

- ⚠️ **Inactivity Reports (`/bounce`):** In groups with `/autokick` enabled, displays members approaching the auto-kick threshold (< 7 days or < 1 day remaining) as well as observation progress for silent members still in their grace period. In other groups, reports members inactive for over 21 days. Displays user role badges (`👑` Admin, `⭐` Autokick Ignored, `💤` Away) and member age indicators (colored circles `🔴`..`⚪` or squares `🟥`..`⬜` for multi-group members). Operates on an independent 60s cooldown.
- 🧹 **Automatic Inactivity Kick with Warnings (`/autokick`):** Automatically purge stale, inactive members from group chats in the background. Features a two-stage warning system: sends a private 1-on-1 direct message warning to inactive candidates and broadcasts a daily summary to the group (once every 24h). Members are kicked only after receiving a warning and passing a 24-hour grace period. Supports cryptographic fingerprint exemptions (`/autokick ignore`) and `/away` vacation status exemptions.
- 🛡️ **Auto-kick Fingerprint Ignore List (`/autokick ignore`):** Exempt specific members or service bots from auto-kick by resolving and storing their cryptographic key fingerprint.
- 👞 **Manual Member Kick (`/kick <userid>`):** Remove a specific member or multiple members from a group chat by contact ID (e.g. `/kick 123` or `/kick /contact123`), search query, or by replying to their message.
- 💤 **Away Status & Auto-Reply (`/away`, `/back`):** Set a temporary away message (e.g. `/away on vacation until Monday`). When other members mention you by name in group chats, the bot informs them privately of your away status. Bare `/away` displays your current status or usage instructions. Running `/back` clears away status and notifies users who messaged you.
- 🔔 **@Mentions With Notifications:** Delta Chat has no real mentions, so a group message that starts with `@Nick` (e.g. `@Gluek посмотри?`) is sent again by the bot as a reply to Nick's last message in the chat, under the author's name (shown as `~Alice`, like TG Bridge). A reply to your own message is what makes Delta Chat notify you, also in a muted chat. The nick is matched against members' names (with or without spaces), display names, addresses and address local parts, case-insensitively; a unique prefix of 3+ letters also works (`@Gle` for Gleb). Ambiguous or unknown nicks, self-mentions and messages that already reply to that person are ignored. Text only (attachments are not copied); one per sender every 10 seconds per group. `MENTION_REPLY=0` turns it off.
  - **Not in the chat? Ask Username Bot:** if nobody in the group has that nick and [Username Bot](https://github.com/mrgluek/deltachat_username) is a member, the bot looks the nick up on `deltachat.id` and sends **only the author, in private**, the person's invite link on `i.delta.chat` (opens right in the Delta Chat app, no browser) and the short link (e.g. `https://deltachat.id/doesnm`, handy to share), so they can write to that person or send them the group's invite. The bot never writes to the mentioned person itself: that would carry group messages outside the group and let anyone make the bot message strangers. Nothing is posted in the group. Username Bot is recognized by address (`USERNAME_BOT_ADDRS`, default `usernamebot`); answers are cached for an hour; unknown nicks are ignored silently. An empty `USERNAME_LOOKUP_URL` turns the lookup off.
- 📖 **Group Chat Catalog (`/chats`):** Users can browse all group chats cataloged by the bot, complete with name, description, and real-time membership count.
- 📢 **Delta Chat Channels Catalog (`/dchannels`):** Users can browse all channels cataloged by the bot, complete with name and description. Channel renames and new channel pictures are tracked automatically (catalog, web preview, RSS and Fediverse actor update on their own).
- 🌐 **Channel Web Preview & RSS Feeds (`/c/{token}`):** Public web previews for registered channels with live message history, attachments, accessible QR code join modals (with Tab focus trapping, Escape key support, focus restoration, and ARIA dialog attributes), inline ✓ Copy Link feedback (no blocking alerts), OpenGraph/Twitter Card metadata, zero-repaint background rendering, Light / Dark / System theme switcher with instant zero-transition toggling and `localStorage` persistence, and standard RSS 2.0 feeds (`/c/{token}/rss.xml`) featuring per-post permalinks (`#post-{msg_id}`), exact media enclosure byte sizes, and CDATA breakout escaping. Soft-delete tombstone handling ensures graceful messaging when channels are removed from the catalog.
- 🪐 **Fediverse / ActivityPub Federation (`@<token>@domain`):** Every registered channel acts as a full Fediverse actor (`type: "Service"` / 🤖 Bot). Users on Mastodon, Pleroma, Misskey, etc., can search `@<token>@<domain>` via WebFinger (`RFC 7033`), follow channels, and receive new messages and media attachments in their home feeds via authenticated HTTP Signatures (`RFC 9421` & `draft-cavage-http-signatures`) with strict KeyId/Actor origin binding, signing key ownership verification (key `owner`/`publicKey` cross-check), 300s anti-replay protection, foreign inbox delivery protection, and bounded backfill concurrency.
- 🔐 **Join Approval Workflows:** Supports public and private groups. Requests to join public groups immediately receive an invite link, while private groups (`🔐`) require approvals from existing members in the group via dynamic `/approve<ID>` commands.
- 📖 **Group FAQ (`/faq`, `/save`, `/remove`):** Drop-in replacement for the standalone FAQ Bot. Reply to any message (text, image, file…) with `/save <topic>` to make it the answer to that topic; afterwards anyone who sends exactly the topic name (case-insensitive) gets the answer. If the topic is sent as a reply to someone ("rules" in reply to a newcomer), the answer goes to that person. Answers can contain `{faq}` (the list of topics) and `{name}` (the name of whoever asked). `/faq` lists the topics, `/faq <topic>` shows one. Unlike the FAQ Bot, only a topic's author or the bot admin can overwrite or `/remove` it, the same topic is answered at most once per 30 s per chat, and answers are stored as copies (text in the database, files in `./data/faq/`) so they survive the 36-hour message cleanup. **Coexistence:** while a bot whose name contains "FAQ" (with the Delta Chat bot flag) is a member of the group, Bouncer stays silent on FAQ commands and topics but keeps mirroring `/save` and `/remove` in the background, so when the FAQ Bot is removed the topics saved in the meantime are already there (topics saved before Bouncer joined have to be saved again). `/faq@boun` is always answered.
- 👋🏻 **Custom Welcome Messages (`/welcome`):** Configure customizable welcoming greetings for new members joining the group with custom rules text and visual age badges.
- 🔗 **Invite Link (`/invite`):** Generate a SecureJoin invite link and QR code image for the current group chat. Available to all users with an independent 1-minute cooldown (admins are exempt). For private group chats, the generated link is single-use and will be automatically deleted from the chat once a new member joins.
- ➕ **New Group (`/newgroup <name>`):** Create a new group with the bot in it and get its join link. Available to all users with a 1-minute cooldown per sender (admins are exempt). In a group where Ntfy Bot is a member, plain `/newgroup` is left to Ntfy Bot so one command does not create two groups (`/newgroup@bouncer` still works). Ntfy Bot is recognized by address, set by `NTFY_BOT_ADDRS` (comma-separated full addresses or local parts, default `ntfydcbot`).
- 🔍 **Member Search (`/search [email1] ...`):** Find group members by one or more email terms (case-insensitive substring matching) or by replying to a message containing email addresses. Displays user role badges (`👑`, `⭐`, `💤`), multi-chat indicators (`🟥`..`⬜`), and searches across all active transports (both primary and secondary addresses).
- 📬 **Relay Check (`/relays`):** Scan for group members using public Russian mail providers (Yandex, Mail.ru, etc.).
- 🏆 **Activity Ranking (`/top`):** Show the 10 most active members in the last 24 hours (independent 60s cooldown).
- 🏓 **ChatMail Ping (`/cmping`):** Ping mail relays (transports) to/from specified target servers using the `cmping` utility: one run per server pair pings there and back with the same profiles (`--both`) and reports through `--json`; a relay that cannot set up or connect a profile is named in the report. Features real-time reaction-based progress tracking (`⏳`, `☑️`, `❌`) and runs asynchronously.
- 📞 **Echo Calls:** Call the bot from any Delta Chat app to test calls: it answers, plays your personal 5-note fingerprint tune (the same one `/join` plays in a meeting) and echoes your voice back. After you hang up it sends a `📞 Echo call report` with the call duration, the media path (direct / peer-to-peer via STUN / TURN relay), how much of your voice it heard, packet loss, jitter and round-trip time, plus which TURN server your app got a relay address from (e.g. `turn.delta.chat` or your relay's) — or that it got none, i.e. its TURN server is blocked on your network. The WebRTC side is [`cmcall`](https://github.com/mrgluek/cmcall)'s `EchoPeer` (aiortc) running on a dedicated event-loop thread, so bot work never stalls call audio. Calls are capped (`CALL_ECHO_MAX_SECONDS`, default 300) and limited in parallel (`CALL_ECHO_MAX_CONCURRENT`, default 2; further callers get a "line busy" message). See [Echo Call Settings](#echo-call-settings).
- 🎙️ **Voice Meetings (prototype, off by default):** `/meet` opens a room, `/join_<id>` makes the bot call you into it, and while the room is open you can simply call the bot to get back in. Delta Chat calls are 1:1, so the bot is the hub: it mixes everyone's audio (mix-minus, noise gate, respects the app's mute) and announces everyone with their own short tune, derived from their key fingerprint, when they join or leave. Background radio (`/radio`) or a shared audio/video file (`/play`) can play quietly underneath. Voice only; the admin switches it on with `/meets on`. See [Voice Meetings](#voice-meetings).
- 📲 **Call Test Between Relays (`/cmcall`):** Runs the `cmcall` utility: a test profile on the first relay calls a profile on the second one, which echoes. Reports signaling delivery in both directions, the TURN servers used (media is forced through the relays' TURN), ICE connect time, echoed-audio round trip and RTP loss/jitter.
- 📞 **Call Monitoring Between Relays:** The call-side twin of the cmping monitor, over the same server list. Every hour (`CMCALL_MONITOR_INTERVAL`) one source relay calls every other relay with `cmcall` (TURN-only media, 5 s of audio); one call per pair covers both mail directions and both TURN servers, and the direction alternates each cycle. A lone relay calls itself. Hard failures are retried once and then raise a `📞🚨 Calls failing` alert in the `/cmreport` chats, edited in place when it resolves; packet loss ≥ `CMCALL_DEGRADED_LOSS_PCT` (10 %) in two checks in a row raises `📞⚠️ Calls degraded`. Relays without a TURN server are shown as "no TURN" and never alert. Quality is measured from the bot's host, so it never counts as an outage.
- 📡 **Server Connectivity Monitoring:** Automatic periodic monitoring of server connectivity using a round-robin algorithm. Employs an **incident-based alerting system** with in-place dynamic message editing (`🚨 Ongoing` → `⚠️ Ongoing (Partial Recovery)` → `✅ Resolved`) to prevent notification noise, along with accurate root-cause fault isolation. Configurable interval via `CMPING_MONITOR_INTERVAL` env var (default: 30 min).
- 👤 **Contact Sharing:** Reports include `/contact<ID>` links to quickly share a contact card for any user.
- 🔄 **Multiple Mail Relays:** Supports multiple mail servers. Relay selection and failover are handled by the Delta Chat core (2.61+), which sends via the newest relay first and falls back to the next one if a relay is unreachable.
- ⏳ **21-Day Grace Period:** The bot tracks group activity in the background and requires 21 days of observation before reporting "never seen" users.
- 🛡️ **Secure Administration & Rate Limiting:** Claim ownership with `/initadmin`. Admins bypass rate limits and have exclusive control over bot settings. All public web endpoints are rate-limited per client IP (with trusted reverse-proxy X-Forwarded-For extraction), returning HTTP 429 on excess. QR code cache is bounded at 200 entries (FIFO eviction).
- 📱 **QR Code Link:** Generates a SecureJoin QR code in the logs for easy device linking.
- 🧪 **TURN Self-Test Page (`/test-turn`):** A browser page on the bot's web server that checks, from the visitor's own network, whether calls can reach `turn.delta.chat` (UDP, and TCP for comparison) and the TURN servers of the bot's relays. Runs entirely in the browser; nothing is sent back. See [Checking TURN Reachability](#checking-turn-reachability).
- 📋 **Startup Version Check:** Automatically checks and logs versions of Bouncer Bot, DeltaChat Core, RPC Client, `deltabot-cli`, `cmping` and `cmcall` at startup.
- 🦠 **VirusTotal Inspection (`/virus`):** Inspect links or attached files for malware, phishing, and security threats using the VirusTotal API v3. Supports direct URL scans (`/virus <url>`), replies to messages containing links, or replies to messages with attached files. Employs a global FIFO queue and rate limiter (1 check every 15 seconds) to strictly adhere to VirusTotal free tier limits, with live in-place message updates as scans complete.
- 🌐 **IP / Network / AS Lookup (`/ip`):** `/ip 84.65.49.143`, `/ip 84.64.0.0/13`, `/ip AS5378` or `/ip chatmail.uk` (a host name or link is resolved with the bot's DNS and its first public address looked up, IPv4 first; the other addresses are listed, and a warning is added when the name points to a CDN such as Cloudflare - then it is the CDN's address, not the real server). Reply `/ip` to a message to check the first public IP address, block or AS number in it, or else the host of its first link (private and reserved addresses are skipped). An IP shows location, organization / AS, host name and its announced BGP prefix (from [ipinfo.io](https://ipinfo.io)), plus open ports with service names, software, Shodan tags (`tor`, `vpn`, `proxy`, `cloud`, `self-signed` ...), hints for VPN / proxy / Tor and router-management ports, and known CVEs (from [Shodan InternetDB](https://internetdb.shodan.io), free, no key - a snapshot of Shodan's periodic scans that can be weeks old and miss ports; IPv6 is rarely scanned). Many networks block Shodan's scanners, so InternetDB can have no record even for a host with ports 80 and 443 open for years: when Shodan has nothing and `CENSYS_API_TOKEN` is set in `.env` (a Personal Access Token from a free [Censys](https://platform.censys.io) account), the bot asks the Censys host lookup API instead and shows its open ports, software and web page titles (e.g. a control panel login page) with a link to the Censys page. A free account has 100 credits per 30 days, one per lookup, shared with the web UI: the bot makes at most `CENSYS_MONTHLY_LIMIT` (default 90) lookups in any 30 days, caches each answer for a day, and pauses Censys for a day after an out-of-credits or auth error; a block shows which AS announces it (or the bigger announced prefix it belongs to), the country and the registry allocation; an AS shows its holder, country and announced prefixes, largest first (from [RIPEstat](https://stat.ripe.net), free and global). Works without any key; an optional `IPINFO_TOKEN` in `.env` is tried first (a paid ipinfo token adds VPN / proxy / Tor / hosting flags). An IPv4 address also shows whether it is in the **Russian mobile internet whitelist** (`📶 RU mobile whitelist: ✅ in 2.63.0.0/17` / `❌ not listed`), a block whether it is listed as a whole, partly (how many addresses and which subnets) or not at all, and an AS how many of its announced IPv4 prefixes are listed - see the `/dns` item below for the source. For everyone, with one global queue: one lookup every 15 seconds, at most 5 waiting; answers are cached for an hour.
- 🧭 **DNS Lookup & Mail / Chatmail Check (`/dns`):** dig-like syntax (`/dig` works too), arguments in any order: `/dns chatmail.uk` gives a full report - addresses with reverse DNS, zone (name servers, DNSSEC, CAA) and for a mail domain MX hosts with forward-confirmed reverse DNS, SPF (one record, `all` policy, whether it covers the MX addresses), DKIM (common selectors, key type and size), DMARC, MTA-STS (the policy is fetched over HTTPS and checked against the MX hosts), TLS-RPT and client SRV records, plus the domain's **certificates from Certificate Transparency logs** (from [Cert Spotter](https://sslmate.com/certspotter/), not expired): how many and from which CAs, until when the domain itself is covered (⚠️ under 10 days - renewal may be failing), certificates from a CA the current CAA does not allow, Cloudflare Universal SSL (Cloudflare issues wildcard certificates for zones on its DNS and adds its CAs to the CAA answer) and the subdomain names found in certificates. Works without a key; with `CERTSPOTTER_API_KEY` in `.env` (free Small plan: 10 queries with subdomains and 100 for a single name per hour) the limits are guaranteed. When the hourly budget for queries with subdomains is spent, only the domain's own certificates are shown; CT answers are cached for an hour. A domain laid out like a chatmail relay (MX = the domain, DKIM selector `opendkim`) also gets the list of records `cmdeploy dns` expects, as required / recommended with what is missing, and whether a valid Let's Encrypt certificate covers the domain, `mta-sts.` and `www.`. `/dns chatmail.uk MX` shows one record type with TTL and CNAME chain, `/dns 213.165.89.146` the reverse name (PTR). `@1.1.1.1` asks a specific public resolver, `@auth` the zone's authoritative name server (bypasses caches - e.g. right after changing records). By default it asks the public resolvers 1.1.1.1 and 8.8.8.8 (`DNS_RESOLVERS` in `.env`; the system resolver on Ubuntu, systemd-resolved, drops DS records). The Addresses section also checks the domain against the **Russian mobile internet whitelists**: the IPv4 addresses of the domain and of its MX hosts (`📶 RU mobile whitelist: 203.0.113.5 ✅ (in 203.0.113.0/24), 198.51.100.7 ❌`) and the domain name itself (`📶 RU mobile whitelist (domains): ✅ gosuslugi.ru is listed` / `⚠️ mail.ru is not listed, only 265 of its subdomains: …` / `❌ … is not listed`; the domain list names exact hosts, the SNI the filters let through). When Russian operators restrict mobile internet only whitelisted resources stay reachable, so a relay on a listed address keeps working for its users. The lists are community-maintained, collected from user reports: IP subnets from [ancored/ru-mobile-whitelist](https://github.com/ancored/ru-mobile-whitelist) (`ru-mobile-whitelist-ip.txt`, rebuilt daily from several public lists - hxehex, roscomvpn, d010b, overskye, escapingworm - minus the subnets reported as dropped; IPv4 only) and exact host names from [hxehex/russia-mobile-internet-whitelist](https://github.com/hxehex/russia-mobile-internet-whitelist) (`whitelist.txt`) - operators and regions differ, so being listed is a good sign, not a guarantee. Each list is downloaded in the background on first use and refreshed every 6 hours: a lookup waits at most 15 seconds for the first copy (else it says the list is unavailable), a stale copy answers while the refresh runs, a download is stopped after 2 minutes, and a failed one keeps the old copy and is retried after 10 minutes. The admin switches each check with `/ruwhitelist [ip|domains] [on|off]`; without that, `RU_WHITELIST_URL` / `RU_WHITELIST_DOMAINS_URL` in `.env` decide (unset: on with the default list, another URL: a list in the same format, empty: off). Reports run three at a time, the rest wait in a queue (up to 50); answers are cached for 5 minutes.
- 🚦 **Paced Sending (relay send limit):** chatmail relays accept 60 messages a minute per sender (burst 10) and answer `450 Too much mail` above that, after which everything is late for minutes. Every message, edit, reaction and deletion the bot sends goes through one queue (`outbox.py`, the same module as in Uptime Bot and ntfy Bot): at most 50 a minute with bursts of 5, by priority - cmping / cmcall monitoring alerts first, then replies and everything a user started (mentions, welcome messages, lookups), then background posts (auto-kick warnings, invite cleanup). A queued update of the same alert message is merged into the waiting one, so it goes out once with the latest text; a `⏳` reaction still waiting is replaced by the final one. A backlog of 20+ messages is logged once a minute.
- 🏠 **Quick Host Lookup (`/host`):** like the `host` tool - `/host chatmail.uk` prints the aliases (CNAME), addresses (A, AAAA) and mail servers (MX, by preference) in `host`'s own wording, `/host 213.165.89.146` the reverse name (PTR); a missing name shows as `Host … not found: 3(NXDOMAIN)`. `@1.1.1.1` / `@auth` work as in `/dns`, and it shares `/dns`'s queue and 5-minute cache. For other record types or the mail checks use `/dns`.
- 📇 **WHOIS / RDAP (`/whois`):** `/whois chatmail.uk` shows registrar, creation / update / expiry dates (⚠️ under 30 days left, ❌ expired), status, name servers, DNSSEC, the registrar's abuse contact and the registrant when the registry publishes it; a free name is reported as not registered. Works for any TLD: RDAP where the TLD has it (.com, .net, .org, .info, .cc, .xyz, .uk, most new gTLDs - servers from IANA's bootstrap list), classic WHOIS on port 43 otherwise (.ru, .su, .рф, .io, .me, .de, .eu ... - server from whois.iana.org), both shown the same way. A subdomain is looked up as its registered domain using the Public Suffix List (`mail.chatmail.uk` -> `chatmail.uk`, `x.example.msk.ru` -> `example.msk.ru`); a TLD or public suffix (`/whois ru`) shows who runs it. `/whois 1.1.1.1`, `/whois 84.64.0.0/13` and `/whois AS5378` ask the regional registry (RIPE, ARIN, APNIC, LACNIC, AFRINIC) for the block, holder, country and abuse contact. Reply `/whois` to a message to check its first address, AS or link. Registries block clients that ask too often, so one server is asked at most every 5 seconds; three lookups run at a time, the rest queue (up to 50); answers are cached for an hour.
- 🎨 **Sticker Creation (`/sticker`, `/stickernobg`):** Convert any image into a standard WebP sticker compatible with Delta Chat, Telegram, and Signal with proportional 512px dimension scaling and automatic EXIF orientation transpose. Triggered either by replying to an image with `/sticker` or `/stickernobg`, or by sending an image with the command in its caption. `/sticker` preserves the original image and background (5s cooldown), while `/stickernobg` (or `/sticker nobg`) uses `rembg` with the lightweight `u2netp` model (only 4.7 MB) and a 15s cooldown with a global concurrency lock. Images are automatically pre-scaled down to 512px *before* AI segmentation to minimize CPU and memory usage, and background removal runs in an isolated subprocess (`sticker_tool.py`) with disabled memory arenas, ensuring 100% of memory is immediately reclaimed by the OS and the main bot daemon remains at ~75–80 MB. Background removal can be disabled via `ENABLE_REMBG=false` or configured with other models via `REMBG_MODEL`. The subprocess runs at idle CPU priority (Linux `SCHED_IDLE`), so it only uses CPU nothing else needs and never makes voice meetings or echo calls stutter; `REMBG_PRIORITY=normal` turns that off. Downloaded models are cached persistently in `./data/u2net` so they are never re-downloaded across restarts or updates.
- ⚡ **High-Performance Architecture & Read Pool:** Optimized SQLite read connection pool (`_ReaderConnectionPool`) supporting concurrent non-blocking reads in WAL mode, eliminating N+1 connection overhead and reducing latency by >12x. Intensive I/O and media processing (VirusTotal inspection, channel post media ingestion, Pillow WebP image optimization, and QR code generation) are fully offloaded to asynchronous background worker threads (`asyncio.to_thread` and dedicated daemon workers), keeping the Delta Chat event loop completely non-blocking.
- 🐳 **Docker Ready:** Easy deployment using Docker Compose.

## Setup

1. **Clone the repository:**

   ```bash
   git clone https://git.gluek.info/gluek/deltachat_bouncer
   cd deltachat_bouncer
   ```

2. **Configure Account and Admin (Optional):**
   Skip this step and the bot creates its own account at first start: the Delta Chat core (2.62+) picks the fastest chatmail relay from its built-in list and later adds further relays in the background. The new address is logged (`Account created: …`).

   To choose the account yourself, initialize it once with an email and password, or with `DCACCOUNT:server.tld` for an account on a specific chatmail relay:

   ```bash
   docker compose run --rm bot python bot.py init bot-email@example.com your_password
   docker compose run --rm bot python bot.py init DCACCOUNT:server.tld
   ```

   The admin can be set on the server (see [Set Administrator](#set-administrator)) or later in the bot chat with `/initadmin` (step 4).

3. **Start the Bot:**

   ```bash
   docker compose up -d
   docker compose logs -f
   ```

   *Note: If it's a new account, a QR code will be printed to the logs for linking your Delta Chat device.*

4. **Claim Admin Ownership:**
   Send `/initadmin` to the bot in a private message to become the administrator.

5. **Updates:**
   Run `./update.sh` (e.g. from cron) to deploy the newest release: the highest `vX.Y.Z` tag, from GitHub or, when GitHub is unreachable, from the Forgejo mirror; `-f` rebuilds the container without changes. Commits on `master` that are not released yet are not deployed. In `.env.local`, `UPDATE_REF=v2.42.0` stays on that release (to roll back; remove it to follow new releases again), `UPDATE_REF=master` follows the branch, and `MONITOR_URL` pings a healthcheck on start, success and failure. Pushing a `vX.Y.Z` tag publishes the GitHub release with that version's `CHANGELOG.md` section.

## Commands

- `/bounce [username]` — Show user activity, or check inactive members in current group (Shows warning candidates and observation progress if `/autokick` is on, otherwise 21 days; displays role badges and age indicators).
- `/search [email1] ...` — Search for group members by one or more emails (case-insensitive substring match) or by replying to a message containing email addresses.
- `/away [message]` — Set away status or view current away status without clearing it.
- `/back` — Clear away status and notify users who messaged you while you were away.
- `/faq [topic]` — List the group's FAQ topics, or show one answer. Sending a topic name as a message also shows its answer.
- `/save <topic>` — Reply to a message to save it as the answer to a topic (`{faq}` = topic list, `{name}` = asker's name). Only the topic's author or the bot admin can overwrite it.
- `/remove <topic>` — Delete a FAQ topic (its author or the bot admin).
- `/relays` — Find group members using public Russian mail providers.
- `/top` — Show the 10 most active members in the last 24 hours.
- `/invite` — Generate an invite link and QR code for this group.
- `/newgroup <name>` — Create a new group with the bot in it and get its join link.
- `/chats` — Show the catalog of registered group chats available to join.
- `/chat<ID> [message]` — Request an invite link to the group (Private chat only).
- `/dchannels` — Show the catalog of registered Delta Chat channels with invite links and web preview URLs.
- `/dchannel<ID>` — Request the invite link and web preview URL for a channel.
- `/chatadd [description]` — Submit the current group to the chat catalog (see [Catalog Submissions](#catalog-submissions)).
- `/dchanneladd <link>` — Submit a channel (or group) invite link to the channel catalog.
- `/chatreport<ID> [reason]` / `/dchannelreport<ID> [reason]` — Report a listed chat or channel as spam or scam.
- `/chatremove`, `/private <on/off>` — In a group that a member submitted: take it out of the catalog, or let everyone join without approval.
- `/cmping <server1> ...` — Ping mail relays to/from specified target servers (15s cooldown, domain-validated).
- `/cmcall <server1> [server2]` — Test a Delta Chat call from server1 to server2 (or within one server): signaling, TURN, audio echo and RTP statistics (60s cooldown, one test at a time).
- `/callstats` — Your recent echo calls to this bot; admins also get 24h/7d totals and the last 10 calls.
- 📞 **Call the bot** — It answers and echoes your audio, then sends call statistics into the chat.
- `/meet` — Create a voice meeting (when the admin enabled them) and get its `/join_<id>` link.
- `/join_<id>` or `/join <id>` — The bot calls you into the meeting; calling the bot while the room is open also puts you back in.
- `/meetclose` / `/meetnew` — Close the meeting you started, or restart it with a new link (in its chat, or in your private chat with the bot).
- `/radio [number|off]` — Meeting radio: list the stations; `/radio 1` plays station 1 in this chat's meeting, `/radio 0` / `/radio off` stops it.
- `/play [off]` — Reply to an audio or video message (or send one with `/play` as caption) to play its sound in this chat's meeting; `/play off` stops it.
- `/ip <IP | prefix | AS | domain>` — IP address, network block, AS or host name lookup; reply `/ip` to a message to check the first address / block / AS (or link) in it (one lookup per 15s for everyone, cached 1h).
- `/dns <domain> [TYPE] [@server|@auth]` (also `/dig`) — DNS report with mail and chatmail relay checks, or one record type / reverse lookup, like dig (`/dns chatmail.uk`, `/dns chatmail.uk MX @auth`, `/dns 213.165.89.146`).
- `/host <domain | IP> [@server|@auth]` — short lookup like the `host` tool: addresses (A, AAAA) and mail servers (MX), with the CNAME it follows, or the reverse name of an IP (`/host chatmail.uk`, `/host 213.165.89.146`).
- `/whois <domain | IP | AS>` — Registration data: registrar, expiry, status, name servers, owner if public; for an IP / AS the holder and abuse contact (RDAP or port-43 WHOIS, any TLD).
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
- `/chatadd [description]` — Add the current group chat to the catalog (Admin: listed at once, any chat type). Falls back to group description if not provided.
- `/chatremove` — Remove the current group chat from the catalog (Admin; any member for a group submitted by a member).
- `/chatdesc<ID> <text>` — Update description of cataloged group chat (Admin only).
- `/dchanneladd <URL>` — Join and add a channel to the catalog as unlisted by default (Admin; users submit for review). If the channel owner's device has not answered the join request yet, the bot says so, keeps watching for 30 minutes and reports when the join completes; the recent posts that a join delivers are then cached for the web preview.
- `/dchannelremove [ID]` — Remove a channel from the catalog (Admin only). If the channel author removes the bot from the channel, it is also automatically removed from the catalog.
- `/dchanneldesc<ID> <text>` — Update description of cataloged channel (Admin only).
- `/dchannelpub<ID>on` / `/dchannelpub<ID>off` — Toggle channel public catalog visibility (Admin only). Unlisted channels are hidden from the public catalog and landing page, but maintain active web preview URLs and RSS feeds.
- `/url [base_url]` — Show or set the base web URL used for public channel previews and RSS feeds (Admin only).
- `/private <on/off>` — Toggle cataloged chat privacy status (Admin; any member for a group submitted by a member).
- `/catalogqueue` — Chat and channel submissions waiting for review (Admin only).
- `/chatapprove<ID>` / `/dchannelapprove<ID>` — Approve a submission, or keep a reported entry listed (clears its reports) (Admin only).
- `/chatreject<ID> [reason]` / `/dchannelreject<ID> [reason]` — Reject a submission: a chat leaves the catalog, the bot leaves a channel and deletes its cached posts and media. The reason is sent to the group / submitter (Admin only).
- `/chatban<ID> [reason]` / `/dchannelban<ID> [reason]` — Reject and ban the submitter from further submissions (Admin only).
- `/dchannelweb<ID>on` / `/dchannelweb<ID>off` — Web preview, RSS and Fediverse mirroring for a channel (Admin only).
- `/catalogsubmit [on/off]` — Switch catalog submissions by users (shows the limits) (Admin only).
- `/ruwhitelist [ip|domains] [on|off]` — Switch the Russian mobile whitelist checks of `/ip` and `/dns`: IP subnets, domains or both; no argument shows their state and list sizes (Admin only).
- `/catalogban <addr>` / `/catalogunban <addr>` — Ban or unban a submitter (Admin only).
- `/welcome [on/off/on <text>]` — Configure welcome messages for new members (Admin only).
- `/transports` — Show mail relays: relay used for sending, send queue, incoming status and storage (Admin only).
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
- `/meets [on|off]` — Switch voice meetings on or off (also `1`/`0`; off by default, off closes all rooms); no argument shows status and open rooms (Admin only).
- `/meetclose <id>` — Close any meeting, e.g. to free its places; ids (or their first 4 characters) are listed by `/meets` (Admin only).
- `/radioadd <url> [name]` / `/radiodel <number>` — Add an http(s) audio stream to the meeting radio (checked before it is added; the name defaults to the stream's `icy-name`) or remove one (Admin only).
- `/cmreport <on/off>` — Toggle monitoring alerts for current chat (Admin only).

Relay selection and failover are handled by the Delta Chat core (2.61+): it keeps sending through the relay that last worked and moves to the next one only if it cannot connect, so the relay used for sending can differ from the account's main address. `/transports` shows which relay that is, the core's send queue, and per relay the incoming status, the last received message and mailbox storage. The former `/setprimary` and `/resilient` commands are deprecated and only reply with this explanation.

### Target-Specific Commands in Group Chats

In group chats where multiple bots are present, you can address this bot specifically to prevent other bots from responding. Append the `@boun` or `@stew` suffix to any command, for example:

- `/help@boun` or `/help@stew`
- `/stats@boun` or `/stats@stew`

A plain `/help` sent in a group chat is answered in a private 1:1 chat with the sender, so several bots don't flood the group with help texts. Use `/help@boun` to show the help in the group itself.

## Catalog Submissions

Any user can submit a group (`/chatadd` inside it) or a channel (`/dchanneladd <invite link>`) to the catalog. The admin path is unchanged; submissions by users are checked like this:

- **Who may submit:** contacts the bot has known for at least `CATALOG_MIN_AGE_DAYS` (3) days. Chatmail accounts are free and disposable, so contact age is the main thing a spammer cannot fake. At most `CATALOG_MAX_PENDING_PER_USER` (2) submissions waiting and `CATALOG_MAX_PER_WEEK` (5) per week per user, `CATALOG_MAX_PENDING_TOTAL` (50) in the whole queue. Banned submitters are refused.
- **Red flags:** stop words (`CATALOG_STOPWORDS`) in the name, description or (for channels) the recent posts, and links that VirusTotal reports as malicious or suspicious (up to 3 links, when `VIRUSTOTAL_API_KEY` is set). A flag never rejects by itself; it is shown to the admin.
- **Auto-approval:** a submitter known for `CATALOG_AUTO_APPROVE_DAYS` (30) days with no red flags is listed at once; the admin still gets the card with a one-command removal. A channel whose join has not finished yet is always reviewed (there is nothing to check). Everything else waits for the admin, who gets a card with the submitter (age, chats), owner check, flags, review preview link and the `/…approve`, `/…reject`, `/…ban` commands. `/catalogqueue` lists what waits.
- **Groups** submitted by a member are private by default: join requests from the catalog go to the members (`/approve<ID>` in the group). Any member can switch that with `/private off` or take the group out with `/chatremove`. A pending group hands out no invites.
- **Channels:** the bot joins right away so that the admin can review the posts. If the link was made by the submitter (the invite link carries the owner's key), the channel gets the web preview, RSS and Fediverse as usual. A channel submitted by someone else is listed in `/dchannels` with its invite link only: the link is public anyway, mirroring the posts to the web is not. The admin can turn mirroring on with `/dchannelweb<ID>on`. A rejected channel is left and its cache deleted.
- **Reports:** `/chatreport<ID>` / `/dchannelreport<ID>` from contacts known for the minimum age, one per contact and entry. After `CATALOG_REPORT_THRESHOLD` (3) reports the entry is hidden (not deleted, so a few accounts cannot remove someone's channel) and back in the review queue.
- **Emergency switch:** `/catalogsubmit off` returns to admin-only (`CATALOG_SUBMISSIONS=0` in `.env` does the same at start).

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
| `CALL_TURN_HOSTS` | — | Extra TURN host names (comma separated) to recognize in callers' relay candidates. `turn.delta.chat`, the bot's relays and the monitored relays are always known. |
| `TURN_TEST_PAGE` | `1` | Serve the `/test-turn` page; `0` turns it off (404) and drops its link from echo reports. |
| `CMCALL_MONITOR_INTERVAL` | `3600` | Seconds between call-monitoring cycles; `0` disables. The first cycle starts a quarter interval (max 15 min) after boot, offset from cmping. |
| `CMCALL_MONITOR_DURATION` | `5` | Seconds of test audio per monitored call. |
| `CMCALL_DEGRADED_LOSS_PCT` | `10` | Packet loss (%) that, in two checks in a row, marks a relay's calls as degraded. |

Call media uses the TURN server the bot's relay announces (Delta Chat core's `ice_servers`), so the container needs no extra ports. Chatmail relays announce only TURN; the Delta Chat apps (libwebrtc) query that server as STUN as well, which aiortc does not do on its own — with `CALL_ECHO_STUN=auto` the bot does the same, so behind the Docker bridge NAT it still learns its public address and most calls connect peer-to-peer (hole punching), with TURN as the fallback for symmetric NAT / CGNAT. `cmcall` test profiles for `/cmcall` are cached in `./data/cmcall_cache` (mounted to `/root/.cache/cmcall`).

### Checking TURN Reachability

Delta Chat apps take their TURN servers from their relays (IMAP METADATA); when none of a profile's relays announces one, Delta Chat core falls back to **`turn.delta.chat`** (UDP 3478, public credentials). Users on a relay with its own TURN (e.g. chatmail relays) do not depend on it. Two ways to see whether a TURN server is blocked for your users:

- **Echo call reports.** An app that reached its TURN server offers a *relay* ICE candidate whose address is the TURN server's. The bot reads the relay candidates of the call offer and those trickled in later, matches their addresses against the TURN host names it knows by forward DNS (`turn.delta.chat`, its own relays, the monitored relays, `CALL_TURN_HOSTS`; resolved in parallel, cached for an hour) and reports `Your app's TURN server: turn.delta.chat ✅`. No relay candidate at all means the app's TURN server did not answer: the report says so and links the test page. The admin's `/callstats` sums it up for 7 days — e.g. `Callers' TURN (7d): chatmail.uk 6/9, turn.delta.chat 2/9 · no TURN address: 1/9 (gmail.com 1)` — and lists the TURN server per recent call. Reverse DNS is not used: a TURN server's PTR record is usually its hosting provider's name.
- **`/test-turn`** (`https://<BASE_URL>/test-turn`, e.g. dc.gluek.info/test-turn): a self-test for anyone, in Russian or English by browser language. It allocates a relay address on `turn.delta.chat` over UDP — what the apps use — and over TCP for comparison, and sends a STUN request to port 3478 of each TURN server the bot's relays announce (reachability only; their credentials are not exposed). The verdict tells apart "works", "UDP blocked but TCP gets through" (calls via `turn.delta.chat` fail) and "unreachable". Everything runs in the browser with `RTCPeerConnection`; results are not sent anywhere and the page never shows the visitor's IP address. Ask users to switch off VPNs/proxies for the test (browsers send TURN-over-TCP through a configured proxy) and to run it on the network they call from.

To test the echo service end to end from the command line: `cmcall <your relay> --to '<bot invite link>'` calls the bot, probes the echo and prints the bot's report.

## Echo Call Privacy

- **No recording.** Audio only passes through memory: decoded, queued for playback (about 100 ms plus `CALL_ECHO_DELAY`) and sent back. Nothing is written to disk and there is no speech recognition. The bot only measures loudness to tell the caller how much of their voice it heard.
- **Encryption.** Media is DTLS-SRTP end to end between the caller's app and the bot; TURN relays see only encrypted packets. The bot is the other end of the call, so it necessarily decrypts the audio to echo it — trust in an echo bot is trust in whoever runs it.
- **Call message.** The incoming call message carries the caller's SDP offer, including their ICE candidates (local and public IP addresses). The bot deletes it from its account as soon as the call is over instead of keeping it for `delete_device_after` (36 h).
- **Statistics.** `/callstats` keeps, per call: contact ID, chat ID, start time, duration, whether media connected, path type (direct / STUN / TURN), packet loss, RTT, jitter, seconds of detected voice, why the call ended, the host names of the TURN servers the caller's app reached and the domain of the caller's address — no audio, no IP addresses (only TURN servers' addresses are read from the caller's relay candidates, never the caller's own). Rows older than `CALL_ECHO_LOG_DAYS` (30) are deleted; `0` keeps none. The admin's `/callstats` lists recent calls by contact ID.
- **Logs.** The bot logs call IDs, contact IDs and durations. aiortc/aioice are kept at WARNING because at INFO they log every ICE candidate pair, i.e. callers' IP addresses; `CALL_DEBUG_LOG=1` re-enables that for troubleshooting only.
- **Call monitoring** (`/cmcall`, `/cmcallstatus`) uses the bot's own test profiles and stores only relay names and measurements.

## Voice Meetings

A prototype audio bridge on top of Delta Chat calls, **off by default** — an admin turns it on with `/meets on` (`/meets off` or `0` closes all rooms; `/meets` alone shows status and open rooms).

- `/meet` creates the chat's room with a 12-character unguessable base62 id and replies with `/join_<id>`. **A room belongs to the chat it was started in:** `/meet` in that chat shows the same link again, and the bot never shows it in another chat (a creator with an open room elsewhere is told to close it first).
- **Group rooms are for the group's members only.** The bot checks membership on `/join`, when someone calls back in, and every 30 s during the meeting, so a leaked link is useless outside the group and whoever leaves or is removed from the group drops out. Tapping `/join_<id>` posts it in the group; to keep the group quiet use *Reply Privately* on the bot's message and send the command there.
- **Private rooms** (`/meet` in a private chat with the bot) are open to anyone who has the link. The bot sends a second message made for forwarding — the bot's invite link plus the `/join_<id>` command — so people who don't have the bot yet can add it and send the command.
- **Managing a room:** its creator can `/meetclose` it (everyone is disconnected) or `/meetnew` it (closed and restarted with a new link, posted only in the room's chat — use it when a link leaked), in the room's chat or in a private chat with the bot. The admin can do both for any room and `/meetclose <id>` to free places.
- `/join_<id>` (or `/join <id>`, also from a group — the call and notice go to your private chat with the bot): the bot calls you. If you miss or end the call, call the bot yourself while the room is open and you land in the same room (an incoming call from someone who joined a room goes to the room, otherwise to the echo service).
- Delta Chat calls are 1:1 only, so every participant is in a normal call with the bot, and the bot mixes: every 20 ms it takes one frame per participant, leaves out silence (noise gate with 300 ms hold) and anyone whose app reports the microphone as muted, and sends each person the sum of the others (mix-minus — you never hear yourself). Joining plays the participant's **join tune** to everyone (the newcomer included), leaving plays it backwards. The tune is 5 notes of the C major pentatonic (C4–A5, 140 ms each, soft bell sound) chosen by a SHA-256 hash of the participant's key fingerprint (their address if there is none) — the same in every meeting and after restarts, so regulars become recognizable by ear; 65 610 possible tunes. `/meet` and `/join` show your tune as note names. Tunes are mixed over the conversation rather than interrupting it, and they reveal nothing about the key (16 bits of a salted hash, kept in memory only).
- **Background audio.** Anyone who can use the room (the group's members; for a private room, its creator's chat) can put something under the conversation from the room's chat:
  - `/radio` lists the stations, `/radio <number>` plays one, `/radio 0` / `/radio off` / `/radio stop` stops it. Stations are http(s) streams (Icecast/Shoutcast, MP3/AAC/Ogg/FLAC, HLS) that only the admin adds — `/radioadd <url> [name]` plays the stream for a moment first and refuses it if no audio comes, `/radiodel <number>` removes it; numbers never change. **Only add streams you may rebroadcast** to your meetings.
  - `/play` as a reply to an audio or video message (or as the caption of one) plays its sound once — only the audio track of a video is used. It pauses the radio, which comes back when the file ends; `/play off` stops the file.
  - Everyone hears it, the speaker included; it bypasses the noise gate and mute. It plays at `MEET_MUSIC_VOLUME` and is pulled down to `MEET_MUSIC_DUCK` of that within ~60 ms when someone talks, coming back over ~0.7 s.
  - While nobody is connected a file pauses and a radio station drops its connection after 10 s, reconnecting when someone joins (and with back-off after network errors). Decoding runs in its own thread and costs about 3 % of a core (AAC stream).
  - Streams are opened by ffmpeg with a protocol whitelist (http/https only) and files with the local file protocol only, both limited to audio/video formats, so a crafted file or playlist cannot read other files or fetch other URLs.
- Rooms live in memory (a bot restart ends them) and close `MEET_IDLE_MINUTES` after the last person left, at the latest `MEET_MAX_HOURS` after creation. One open room per user.
- **Capacity** is one budget for all rooms: `MEET_TOTAL_SLOTS` places (8) in at most `MEET_MAX_ROOMS` rooms (2). One room can use all 8; with two rooms each takes 4, and a second room is refused while the first has more than 4 people.

| Variable | Default | Description |
|---|---|---|
| `MEET_TOTAL_SLOTS` | `8` | Places across all rooms. |
| `MEET_MAX_ROOMS` | `2` | Rooms at the same time. |
| `MEET_MAX_PARTICIPANTS` | `8` | Upper limit per room. |
| `MEET_IDLE_MINUTES` | `60` | A room closes this long after its last participant left. |
| `MEET_MAX_HOURS` | `6` | Hard limit on a room's lifetime. |
| `MEET_RING_SECONDS` | `45` | How long the bot rings after `/join`. |
| `MEET_MAX_ROOMS_PER_USER` | `1` | Open rooms per creator. |
| `MEET_MUSIC_VOLUME` | `0.35` | Volume of background radio/files (1.0 = as decoded). |
| `MEET_MUSIC_DUCK` | `0.3` | Background volume factor while someone talks. |

**Load.** Each participant costs an Opus decode + encode plus aiortc's RTP/SRTP handling in Python: about 10 % of one core per participant (2.8 GHz Xeon), the mixing itself almost nothing. A full meeting of 8 used 70–90 % of one core and ~95 MB RSS in `tests/bench_meet.py` — within the container's 2-CPU / 2 GB limit next to the 2 echo calls. Run `python3 tests/bench_meet.py [participants] [seconds]` in the container to measure on your hardware.

**Privacy.** The bot does not record or store anyone's voice: audio exists only in short in-memory queues (≈100 ms) and nothing is written to disk. But the bot decrypts and mixes everyone's audio, so a meeting is **not end-to-end encrypted between participants**, and any participant can record what they hear — every room and join message says so. Call messages (they carry SDP with IP addresses) are deleted when a participant leaves; nothing about meetings is stored in the database except the on/off switch and the radio station list. A file played with `/play` is downloaded into the bot's account like any attachment and deleted with its message by the bot's 36-hour `delete_device_after`; while a radio station plays, its server sees the bot's IP address, not the participants'.

**Limits of the prototype.** When the bot places the call (`/join`) its offer is audio-only without the apps' data channels, so mute is detected by the noise gate only (a muted mic is silent anyway); when you call in, the app's mute state is used directly.

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

1. **Disable Auto-Downloads:** Disables automatic downloads of attachments/media files over 64 KB (`download_limit` is set to 65536 bytes; at 1 byte the core also deferred every plain text message, which delayed replies). Attachments are only downloaded on demand (e.g. `/sticker`, `/virus`, or `/save` of a message with a file; FAQ files are copied to `./data/faq/` and are not affected by the cleanup below).
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
- `calls.py` — echo call service (`EchoCallManager`, `IncomingCall`/`OutgoingCallAccepted`/`CallEnded` raw-event hooks, routing calls into meetings) and the `/cmcall` / `/callstats` commands.
- `meet.py` — voice meetings: `MeetManager` (rooms, capacity budget, calling participants in), the mix-minus mixer and `/meet`, `/join`, `/meets`, `/radio`, `/play`.
- `turn_names.py` — which TURN server a caller's app used: relay-candidate addresses matched against known TURN host names (forward DNS, cached).
- `meet_media.py` — background audio for meetings: decodes a radio stream or a played file (PyAV, whitelisted protocols/formats) into 20 ms mixer frames in its own thread, with pause and reconnect.
- `moderation.py`, `transports.py`, `cmping.py` + `cmping_commands.py`, `channels.py`, `virustotal.py`, `iplookup.py`, `dnscheck.py` (+ `certs.py`, `whitelist.py`), `whoislookup.py`, `stickers.py`, `faq.py` — one module per subsystem, each owning its background workers and `/command` handlers.
- `commands.py` / `handlers.py` — the remaining general commands (`/help`, `/bounce`, `/search`, …) and the global Delta Chat event handlers (system messages, the catch-all `NewMessage` dispatcher).
- `web/` — the aiohttp channel-preview + ActivityPub server: `web/routes.py`, `web/ap_routes.py`, and `web/templates/` (landing page, `/test-turn` page, channel preview, RSS feed, tombstone/404, shared theme snippets).

`bot.py` re-exports everything under its old name for backward compatibility (`import bot; bot.<name>` still works), but new code should import the owning module directly.

## Support & Development

If you find this bot useful, consider supporting its development:

- **Git:** [gluek/deltachat_bouncer](https://git.gluek.info/gluek/deltachat_bouncer)
- **Donations:** Use the `/donate` command in Delta Chat.

## License

MIT, see [LICENSE](LICENSE).
