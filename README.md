# Delta Chat Bouncer Bot

Delta Chat bot designed to maintain group quality by monitoring inactivity and save server resouces by not sending mails to stale users. It scans group members and reports users who haven't been seen online for over 14 days.

## Features

- ⚠️ **Inactivity Reports (`/bounce`):** Trigger a manual scan for inactive group members. Reports total members, active count, and a list of inactive users.
- 📬 **Relay Check (`/relays`):** Scan for group members using regular mail providers (Yandex, Mail.ru, etc.).
- 🏆 **Activity Ranking (`/top`):** Show the 10 most active members in the last 24 hours.
- 👤 **Contact Sharing:** Reports include `/contact<ID>` links to quickly get a contact object for any user.
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
- `/relays` — Find group members using regular mail providers.
- `/top` — Show the 10 most active members in the last 24 hours.
- `/contact<ID>` — Get a contact object for the given ID (e.g., `/contact123`).
- `/help` — Show available commands and bot information (Threshold: 14 days).
- `/donate` — Support project development ❤️
- `/initadmin` — Claim administrative ownership (private chat only).
- `/transports` — Show configured mail relays & stats (Admin only).
- `/addtransport` — Add a backup mail relay (Admin only).
- `/rmtransport <addr>` — Remove a mail relay (Admin only).
- `/setprimary <addr>` — Switch the primary mail relay (Admin only).

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

## Support & Development

If you find this bot useful, consider supporting its development:

- **Git:** [gluek/deltachat_bouncer](https://git.gluek.info/gluek/deltachat_bouncer)
- **Donations:** Use the `/donate` command in Delta Chat.
