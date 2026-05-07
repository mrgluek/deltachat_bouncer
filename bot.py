import asyncio
import io
import logging
import os
import re
import threading
import time
from datetime import datetime

from deltachat2 import events, MsgData
from deltabot_cli import BotCli
import qrcode

import database

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("bouncer_bot")

dc_cli = BotCli("bouncer")

dc_bot_instance = None
dc_accid = None

DC_CONTACT_ID_SELF = 1
INACTIVITY_DAYS_THRESHOLD = 30
INACTIVITY_SECONDS_THRESHOLD = INACTIVITY_DAYS_THRESHOLD * 24 * 3600

# ── Admin helpers ──

def _get_contact_fingerprint(bot, accid, contact_id, contact=None):
    if contact:
        get_val = getattr(contact, 'get', lambda k: getattr(contact, k, None))
        for attr in ['fingerprint', 'key_fingerprint', 'public_key']:
            val = get_val(attr)
            if val:
                matches = re.findall(r'[0-9a-fA-F]{32,64}', str(val).replace(' ', '').replace(':', ''))
                if matches:
                    return matches[0].upper()
    try:
        fp = bot.rpc.get_contact_config(accid, contact_id, "fp")
        if fp:
            return fp.upper().replace(' ', '')
    except Exception:
        pass
        
    self_fps = set()
    try:
        enc_info_self = bot.rpc.get_contact_encryption_info(accid, DC_CONTACT_ID_SELF)
        if enc_info_self:
            matches = re.findall(r'[0-9a-fA-F]{32,64}', "".join(enc_info_self.split()).replace(':', ''))
            self_fps.update(m.upper() for m in matches)
    except: pass

    for args in [(accid, contact_id), (contact_id,)]:
        try:
            enc_info = bot.rpc.get_contact_encryption_info(*args)
            if enc_info:
                cleaned = "".join(enc_info.split()).replace(':', '')
                matches = re.findall(r'[0-9a-fA-F]{32,64}', cleaned)
                valid_matches = [m.upper() for m in matches if m.upper() not in self_fps]
                if valid_matches:
                    return ",".join(valid_matches)
        except Exception:
            continue
    return None

def _is_dc_admin(bot, accid, contact_id):
    try:
        admin_fp = database.get_admin_fingerprint()
        admin_email = database.get_config("admin_dc_email")

        if not admin_fp and not admin_email:
            return False

        contact = None
        try:
            contact = bot.rpc.get_contact(accid, contact_id)
        except Exception:
            pass

        if not contact:
            return False

        if admin_fp:
            c_fp = _get_contact_fingerprint(bot, accid, contact_id, contact=contact)
            if c_fp:
                if admin_fp.upper() in c_fp.upper().split(','):
                    return True
                return False

        if admin_email and contact.address:
            if admin_email.strip().lower() == contact.address.strip().lower():
                return True

    except Exception as e:
        logger.error(f"Critical error in admin check: {e}")
    return False

def _send(bot, accid, chat_id, text):
    msg_data = MsgData(text=text)
    try:
        bot.rpc.send_msg(accid, chat_id, msg_data)
    except Exception as e:
        logger.error(f"Failed to send msg to chat {chat_id}: {e}")

# ── Bouncer Logic ──

def _check_chat_inactivity(bot, accid, chat_id) -> str:
    """Check a specific chat for inactive members and return a formatted report string."""
    try:
        contacts = bot.rpc.get_chat_contacts(accid, chat_id)
    except Exception as e:
        logger.error(f"Failed to get chat contacts for {chat_id}: {e}")
        return ""

    if len(contacts) <= 2:
        return "" # Ignore 1-on-1 chats and empty groups
        
    now = time.time()
    inactive_users = []
    
    for contact_id in contacts:
        if contact_id == DC_CONTACT_ID_SELF:
            continue
            
        try:
            contact = bot.rpc.get_contact(accid, contact_id)
            if contact.address and contact.address.lower() == "deltachat@system.local":
                continue # Ignore system contact

            last_seen = getattr(contact, "last_seen", 0)
            
            # Use name if available, otherwise address, otherwise "Unknown"
            name = contact.name or contact.display_name or "Unknown"
            address = contact.address or "no_email@example.com"
            
            if last_seen == 0:
                inactive_users.append(f"- {name} ({address}) - never seen")
            else:
                inactive_duration = now - last_seen
                if inactive_duration > INACTIVITY_SECONDS_THRESHOLD:
                    days_ago = int(inactive_duration / (24 * 3600))
                    date_str = datetime.fromtimestamp(last_seen).strftime("%-d %b %Y")
                    inactive_users.append(f"- {name} ({address}) - last seen {date_str}, {days_ago} days ago")
        except Exception as e:
            logger.error(f"Error checking contact {contact_id}: {e}")

    if not inactive_users:
        return ""
        
    report = f"⚠️ **Inactivity Report**\nThe following users have not been seen for more than {INACTIVITY_DAYS_THRESHOLD} days (or never):\n\n"
    report += "\n".join(inactive_users)
    return report

def _background_checker_loop(bot, accid):
    logger.info("Background checker loop started.")
    while True:
        try:
            # Run once a day. We check if 24h passed since last run.
            last_run = float(database.get_config("last_daily_run") or 0)
            now = time.time()
            if now - last_run >= 24 * 3600:
                logger.info("Running daily inactivity check...")
                chats = bot.rpc.get_chat_ids(accid)
                for chat_id in chats:
                    try:
                        chat = bot.rpc.get_basic_chat_info(accid, chat_id)
                        # We only want to check groups/mailing lists. 
                        # Usually type 2, 3, etc are groups (multiuser). We can rely on _check_chat_inactivity returning empty for non-groups.
                        is_multiuser = getattr(chat, "is_multiuser", None)
                        if is_multiuser is None:
                            # fallback: assume it might be a group, the inner function will skip if <= 2 contacts
                            is_multiuser = True
                            
                        if is_multiuser:
                            report = _check_chat_inactivity(bot, accid, chat_id)
                            if report:
                                _send(bot, accid, chat_id, report)
                    except Exception as e:
                        logger.error(f"Error checking chat {chat_id} in background task: {e}")
                        
                database.set_config("last_daily_run", str(now))
                logger.info("Daily check finished.")
        except Exception as e:
            logger.error(f"Background loop error: {e}")
            
        time.sleep(3600) # Check every hour if we need to run the daily task

# ── Commands ──

@dc_cli.on_init
def on_init(bot, args):
    global dc_bot_instance, dc_accid
    bot.logger.info("Initializing Bouncer Bot...")
    dc_bot_instance = bot
    
    accounts = bot.rpc.get_all_account_ids()
    if accounts:
        dc_accid = accounts[0]
        bot.rpc.set_config(dc_accid, "displayname", "Bouncer Bot")
        bot.rpc.set_config(dc_accid, "selfstatus", "I monitor groups for inactive users. Send /bounce to check now.")
        
        # Set icon if exists
        try:
            base_dir = os.path.dirname(os.path.abspath(__file__))
            for icon_name in ["icon.png", os.path.join("data", "icon.png")]:
                icon_path = os.path.join(base_dir, icon_name)
                if os.path.exists(icon_path):
                    bot.rpc.set_config(dc_accid, "selfavatar", icon_path)
                    break
        except Exception as e:
            bot.logger.warning(f"Could not set avatar: {e}")

@dc_cli.on_start
def on_start(bot, args):
    global dc_bot_instance, dc_accid
    dc_bot_instance = bot
    
    accounts = bot.rpc.get_all_account_ids()
    if not accounts:
        logger.error("No accounts found.")
        return
        
    accid = accounts[0]
    dc_accid = accid
    
    logger.info(f"Bouncer bot started with accid {accid}.")
    
    # Generate and print SecureJoin QR code to logs
    try:
        qrdata = bot.rpc.get_chat_securejoin_qr_code(accid, None)
        print("\n" + "=" * 50)
        print("To add this bot, scan the QR code or copy the link:\n")
        
        qr = qrcode.QRCode(version=1, box_size=1, border=2)
        qr.add_data(qrdata)
        qr.make(fit=True)
        f = io.StringIO()
        qr.print_ascii(out=f)
        print(f.getvalue())
        
        print(qrdata)
        print("\n" + "=" * 50 + "\n")
    except Exception as e:
        logger.error(f"Failed to generate QR code: {e}")
    
    t = threading.Thread(target=_background_checker_loop, args=(bot, accid), daemon=True)
    t.start()


@dc_cli.on(events.NewMessage(command="/initadmin"))
def initadmin_command(bot, accid, event):
    msg = event.msg
    admin_email = database.get_config("admin_dc_email")
    admin_fp = database.get_admin_fingerprint()

    if admin_email or admin_fp:
        _send(bot, accid, msg.chat_id, "❌ Admin is already set. Use `set_admin.py` on the server to change.")
        return

    contact = bot.rpc.get_contact(accid, msg.from_id)
    email = contact.address
    database.set_config("admin_dc_email", email)

    fp = _get_contact_fingerprint(bot, accid, msg.from_id, contact=contact)
    if fp:
        first_fp = fp.split(',')[0]
        database.set_admin_fingerprint(first_fp)
        _send(bot, accid, msg.chat_id,
              f"✅ You are now the admin!\n\nEmail: `{email}`\nFingerprint: `{first_fp[-8:]}`")
    else:
        _send(bot, accid, msg.chat_id,
              f"✅ You are now the admin!\n\nEmail: `{email}`\n⚠️ Fingerprint not available yet.")


@dc_cli.on(events.NewMessage(command="/bounce"))
def bounce_command(bot, accid, event):
    msg = event.msg
    
    if not _is_dc_admin(bot, accid, msg.from_id):
        _send(bot, accid, msg.chat_id, "❌ This command is restricted to the bot administrator.")
        return

    report = _check_chat_inactivity(bot, accid, msg.chat_id)
    if report:
        _send(bot, accid, msg.chat_id, report)
    else:
        _send(bot, accid, msg.chat_id, "✅ All users are active or this is not a group chat.")

if __name__ == "__main__":
    import sys
    if len(sys.argv) == 1:
        sys.argv.append("serve")
    dc_cli.start()
