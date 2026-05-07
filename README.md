# Delta Chat Bouncer Bot

A Delta Chat bot designed to monitor large groups and report users who haven't been seen online for over 30 days.

## Features
- **Daily Inactivity Report:** Wakes up once a day to scan all groups the bot is in and reports inactive members.
- **Manual Check:** Admins can trigger an immediate check in a group using the `/bounce` command.
- **Secure Administration:** Uses Delta Chat cryptographic fingerprints (or email) for admin authentication.

## Setup

1. **Clone the repository:**
   ```bash
   git clone https://github.com/mrgluek/deltachat_bouncer
   cd deltachat_bouncer
   ```

2. **Configure Delta Chat account:**
   Initialize the Delta Chat account for the bot:

   ```bash
   docker compose run --rm bot python bot.py init bot-email@example.com your_password
   ```

   Then start the bot:

   ```bash
   docker compose up -d
   docker compose logs -f
   ```
   *(You'll see a QR code in the logs if it's the first time and you need to link a device).*

3. **Set Admin:**
   - Add the bot to your contacts in Delta Chat.
   - Send `/initadmin` to the bot in a direct message. 
   - *Alternatively*, run `python set_admin.py` (or execute it within the docker container) to manually set the admin email/fingerprint.

## Commands (Admin Only)
- `/bounce` - Triggers an immediate inactivity check in the current group and sends the report to the chat.
