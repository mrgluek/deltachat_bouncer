# Delta Chat Bouncer Bot

Delta Chat bot designed to maintain group quality by monitoring inactivity and save server resouces by not sending mails to stale users. It scans group members and reports users who haven't been seen online for over 14 days.

## Features

- ⚠️ **Inactivity Reports (`/bounce`):** Trigger a manual scan for inactive group members. Reports total members, active count, and a list of inactive users.
- 📖 **Group Chat Catalog (`/chats`):** Users can browse all group chats cataloged by the bot, complete with name, description, and real-time membership count.
- 📢 **Delta Chat Channels Catalog (`/dchannels`):** Users can browse all channels cataloged by the bot, complete with name and description.
- 🔐 **Join Approval Workflows:** Supports public and private groups. Requests to join public groups immediately receive an invite link, while private groups (`🔐`) require approvals from existing members in the group via dynamic `/approve<ID>` commands.
- 👋🏻 **Custom Welcome Messages (`/welcome`):** Configure customizable welcoming greetings for new members joining the group, with stats (total chats in common) and custom rules text.
- 🔗 **Invite Link (`/invite`):** Generate a SecureJoin invite link and QR code image for the current group chat. Available to all users with a 10-minute cooldown (admins are exempt).
- 🔍 **Member Search (`/search [email1] ...`):** Find group members by one or more email terms (case-insensitive substring matching) or by replying to a message containing email addresses. Searches across all active transports (both primary and secondary addresses) and displays all configured addresses for matching contacts.
- 📬 **Relay Check (`/relays`):** Scan for group members using regular mail providers (Yandex, Mail.ru, etc.).
- 🏆 **Activity Ranking (`/top`):** Show the 10 most active members in the last 24 hours.
- 🏓 **ChatMail Ping (`/cmping`):** Ping mail relays (transports) to/from specified target servers using the `cmping` utility. Features real-time reaction-based progress tracking (`⏳`, `☑️`, `❌`) and runs asynchronously.
- 📡 **Server Connectivity Monitoring:** Automatic periodic monitoring of server connectivity using a round-robin algorithm. Checks each server pair with retry logic, reports only on state changes (OK→FAIL / FAIL→OK) to subscribed chats. Configurable interval via `CMPING_MONITOR_INTERVAL` env var (default: 30 min).
- 👤 **Contact Sharing:** Reports include `/contact<ID>` links to quickly get a contact object for any user.
- 🔄 **Automatic Transport Failover:** Supports multiple mail servers. The bot automatically detects message delivery failures via raw core events, switches `configured_addr` to a backup transport in round-robin fashion, resends the message, and remains on the working transport.
- ⏳ **14-Day Grace Period:** The bot tracks group activity in the background and requires 14 days of observation before reporting "never seen" users.
- 🛡️ **Secure Administration:** Claim ownership with `/initadmin`. Admins bypass rate limits and have exclusive control over bot settings.
- 📱 **QR Code Link:** Generates a SecureJoin QR code in the logs for easy device linking.
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

- `/bounce` — Trigger an immediate inactivity check in the current group.
- `/search [email1] ...` — Search for group members by one or more emails (case-insensitive substring match) or by replying to a message containing email addresses. Searches across all active transports/secondary addresses.
- `/relays` — Find group members using regular mail providers.
- `/top` — Show the 10 most active members in the last 24 hours.
- `/invite` — Generate an invite link and QR code for this group.
- `/chats` — Show the catalog of registered group chats available to join.
- `/chat<ID> [message]` — Request an invite link to the group (Private chat only).
- `/dchannels` — Show the catalog of registered Delta Chat channels.
- `/dchannel<ID>` — Request the invite link to a channel.
- `/cmping <server1> ...` — Ping mail relays to/from specified target servers (15s cooldown).
- `/approve<ID>` — Approve a pending join request for a private group (Group chat only).
- `/decline<ID> [reason]` — Decline a pending join request for a private group with an optional reason (Group chat only).
- `/contact<ID>` — Get a contact object for the given ID (e.g., `/contact123`).
- `/help` — Show available commands and bot information (Threshold: 14 days).
- `/donate` — Support project development ❤️
- `/initadmin` — Claim administrative ownership (private chat only).
- `/chatadd [description]` — Add the current group chat to the catalog (Admin only). Falls back to group description if not provided.
- `/chatremove` — Remove the current group chat from the catalog (Admin only).
- `/chatdesc<ID> <text>` — Update description of cataloged group chat (Admin only).
- `/dchanneladd <URL>` — Join and add a channel to the catalog (Admin only).
- `/dchannelremove [ID]` — Remove a channel from the catalog (Admin only).
- `/dchanneldesc<ID> <text>` — Update description of cataloged channel (Admin only).
- `/private <on/off>` — Toggle cataloged chat privacy status (Admin only).
- `/welcome [on/off/on <text>]` — Configure welcome messages for new members (Admin only).
- `/transports` — Show configured mail relays & stats (Admin only).
- `/addtransport` — Add a backup mail relay (Admin only).
- `/rmtransport <addr>` — Remove a mail relay (Admin only).
- `/setprimary <addr>` — Switch the primary mail relay (Admin only).
- `/resilient` — Toggle resilient sending mode across all relays (Admin only).
- `/cmpingadd <server>` — Add a server to connectivity monitoring rotation (Admin only).
- `/cmpingdel <server>` — Remove a server from monitoring (Admin only).
- `/cmpinglist` — Show all monitored servers, pair count, and rotation info (Admin only).
- `/cmpingstatus` — Show full monitoring results matrix (Admin only).
- `/cmpingfail [server]` — Show currently failed links with an optional server filter (Admin only).


- `/cmreport <on/off>` — Toggle monitoring alerts for current chat (Admin only).


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

## Support & Development

If you find this bot useful, consider supporting its development:

- **Git:** [gluek/deltachat_bouncer](https://git.gluek.info/gluek/deltachat_bouncer)
- **Donations:** Use the `/donate` command in Delta Chat.
