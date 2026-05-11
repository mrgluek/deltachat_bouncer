# Delta Chat Bouncer Bot

A professional Delta Chat bot designed to maintain group quality by monitoring inactivity. It automatically scans group members and reports users who haven't been seen online for over 30 days.

## Features

- 🕵️ **Daily Status & Inactivity Report:** Automatically scans all groups once a day. Posts group statistics (total members, active count) and a list of inactive members.
- 📬 **Relay Check (`/relays`):** Scan for group members using "regular mail" (Yandex, Mail.ru, Rambler) that may cause delivery issues in large groups.
- ⏳ **30-Day Grace Period:** For new groups, the bot waits 30 days before reporting "never seen" users to avoid false positives.
- 🚀 **Manual Check (`/bounce`):** Anyone can trigger an immediate inactivity check (with a 10-minute cooldown per group).
- 🛡️ **Secure Administration:** Claim ownership with `/initadmin`. Admins bypass rate limits and have exclusive control over bot settings and group scanning.
- 📱 **QR Code Link:** Generates a SecureJoin QR code in the logs for easy device linking.
- 🐳 **Docker Ready:** Easy deployment using Docker Compose.

## Setup

1. **Clone the repository:**
   ```bash
   git clone https://github.com/mrgluek/deltachat_bouncer
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
- `/help` — Show available commands and bot information.
- `/donate` — Support project development ❤️
- `/initadmin` — Claim administrative ownership (private chat only).

## Admin Management

You can manually manage the administrator or add backup transports via the server CLI:

### Set Administrator
```bash
docker compose exec bot python set_admin.py --email your@email.com
```

### Add Backup Relay (Transport)
To ensure the bot stays online even if one mail server is down:
1. Stop the bot: `docker compose stop bot`
2. Add relay: `docker compose run --rm bot python bot.py init transport backup-email@example.com password`
3. Start the bot: `docker compose up -d`

## Support & Development

If you find this bot useful, consider supporting its development:
- **GitHub:** [mrgluek/deltachat_bouncer](https://github.com/mrgluek/deltachat_bouncer)
- **Donations:** Use the `/donate` command in Delta Chat.
