# Delta Chat Bouncer Bot

A Delta Chat bot designed to monitor large groups and report users who haven't been seen online for over 30 days.

## Features
- **Daily Inactivity Report:** Wakes up once a day to scan all groups the bot is in and reports inactive members.
- **Manual Check:** Admins can trigger an immediate check in a group using the `/bounce` command.
- **Secure Administration:** Uses Delta Chat cryptographic fingerprints (or email) for admin authentication.

## Setup

1. **Clone the repository:**
   ```bash
   git clone <your-repo-url>
   cd deltachat_bouncer
   ```

2. **Configure Delta Chat account:**
   Use the `deltabot-cli` standard process or initialize it. Run the bot for the first time so it generates the `data` folder and connects.

   ```bash
   docker-compose up -d
   docker-compose logs -f
   ```
   *(You'll see a QR code in the logs if it's the first time and you need to link a device, or use `deltabot_cli` to login directly).*

3. **Set Admin:**
   - Add the bot to your contacts in Delta Chat.
   - Send `/initadmin` to the bot in a direct message. 
   - *Alternatively*, run `python set_admin.py` (or execute it within the docker container) to manually set the admin email/fingerprint.

## Commands (Admin Only)
- `/bounce` - Triggers an immediate inactivity check in the current group and sends the report to the chat.
