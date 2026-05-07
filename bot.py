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

# Anti-spam: {chat_id: timestamp}
_chat_anti_spam: dict[int, float] = {}
BOUNCE_COOLDOWN_SECONDS = 600  # 10 minutes

# ── Admin helpers ──

def _get_contact_fingerprint(bot, accid, contact_id, contact=None):
    self_fps = set()
    try:
        bot_addrs = []
        # Get primary address
        bot_addr = bot.rpc.get_config(accid, "addr")
        if bot_addr:
            bot_addrs.append(bot_addr.lower().strip())
            
        # Get all transport aliases (crucial if chat was started via secondary relay)
        try:
            transports = bot.rpc.list_transports(accid)
            for t in transports:
                t_addr = t.get('addr', '') if isinstance(t, dict) else getattr(t, 'addr', '')
                if t_addr:
                    bot_addrs.append(t_addr.lower().strip())
        except: pass
        
        if bot_addrs:
            for args in [(accid, contact_id), (contact_id,)]:
                try:
                    enc_info_self = bot.rpc.get_contact_encryption_info(*args)
                    if enc_info_self:
                        blocks = re.split(r'\n\s*\n', enc_info_self.strip())
                        for block in blocks:
                            if any(a in block.lower() for a in bot_addrs):
                                matches = re.findall(r'[0-9a-fA-F]{32,64}', "".join(block.split()).replace(':', ''))
                                self_fps.update(m.upper() for m in matches)
                        break
                except Exception:
                    continue

        if self_fps:
            logger.info(f"Detected bot's own fingerprints from enc_info: {[f[-8:] for f in self_fps]}")
    except Exception as e:
        logger.error(f"Error detecting self-fingerprint: {e}")

    # Filter fingerprints from contact object
    if contact:
        get_val = getattr(contact, 'get', lambda k: getattr(contact, k, None))
        for attr in ['fingerprint', 'key_fingerprint', 'public_key']:
            val = get_val(attr)
            if val:
                matches = re.findall(r'[0-9a-fA-F]{32,64}', str(val).replace(' ', '').replace(':', ''))
                valid_matches = [m.upper() for m in matches if m.upper() not in self_fps]
                if valid_matches:
                    return ",".join(valid_matches)
    try:
        fp = bot.rpc.get_contact_config(accid, contact_id, "fp")
        if fp and fp.upper().replace(' ', '') not in self_fps:
            return fp.upper().replace(' ', '')
    except Exception:
        pass

    for args in [(accid, contact_id), (contact_id,)]:
        try:
            enc_info = bot.rpc.get_contact_encryption_info(*args)
            if enc_info:
                cleaned = "".join(enc_info.split()).replace(':', '')
                matches = re.findall(r'[0-9a-fA-F]{32,64}', cleaned)
                # Filter out bot's own fingerprints
                valid_matches = [m.upper() for m in matches if m.upper() not in self_fps]
                if valid_matches:
                    return ",".join(valid_matches)
        except Exception:
            continue
    return None

def _is_dc_admin(bot, accid, contact_id):
    """Check if the given contact is the bot administrator (by email or fingerprint)."""
    try:
        contact = None
        try:
            contact = bot.rpc.get_contact(accid, contact_id)
        except Exception:
            pass
        
        if not contact:
            return False

        # Safety check: bot itself is never the admin
        if contact_id == 1:
            return False

        # 1. Check fingerprint (strongest)
        admin_fp = database.get_admin_fingerprint()
        if admin_fp:
            c_fp = _get_contact_fingerprint(bot, accid, contact_id, contact=contact)
            logger.info(f"Admin check (FP) for {contact_id}: stored={admin_fp}, contact={c_fp}")
            if c_fp:
                if admin_fp.upper() in c_fp.upper().split(','):
                    return True
            
            logger.warning(f"Admin check: Fingerprint mismatch or missing for {contact_id}")
            return False
        
        # 2. Check email
        sender_email = contact.address
        admin_email = database.get_config("admin_dc_email")
        logger.info(f"Admin check (Email) for {contact_id}: stored={admin_email}, contact={sender_email}")
        if admin_email and sender_email and admin_email.lower().strip() == sender_email.lower().strip():
            return True
            
    except Exception as e:
        logger.error(f"Critical error in admin check: {e}")
    return False

def _send(bot, accid, chat_id, text):
    msg_data = MsgData(text=text)
    
    # Try to determine how many attempts we should make based on number of transports
    try:
        transports = bot.rpc.list_transports(accid)
        max_attempts = max(2, len(transports))
    except Exception:
        transports = []
        max_attempts = 2

    for attempt in range(max_attempts):
        try:
            bot.rpc.send_msg(accid, chat_id, msg_data)
            return # Success!
        except Exception as e:
            error_str = str(e).lower()
            logger.warning(f"Attempt {attempt + 1} failed to send message: {e}")
            
            # List of strings that suggest a transport/network level failure
            transport_errors = ["network", "timeout", "connection", "unreachable", "smtp", "status 0", "socket", "refused", "auth"]
            
            if attempt < max_attempts - 1 and any(err in error_str for err in transport_errors):
                try:
                    # Determine current primary address
                    current_addr = bot.rpc.get_config(accid, "addr")
                    
                    if not transports:
                        transports = bot.rpc.list_transports(accid)
                    
                    if len(transports) > 1:
                        # Find a backup relay to switch to
                        for t in transports:
                            t_addr = t.get('addr') if isinstance(t, dict) else getattr(t, 'addr', None)
                            if t_addr and t_addr != current_addr:
                                logger.info(f"Switching transport from {current_addr} to backup: {t_addr}")
                                try:
                                    bot.rpc.set_config(accid, "addr", t_addr)
                                    # If the transport has a password stored, update it too
                                    t_pw = t.get('password') if isinstance(t, dict) else getattr(t, 'password', None)
                                    if t_pw:
                                        bot.rpc.set_config(accid, "mail_pw", t_pw)
                                    
                                    time.sleep(2) # Give core a moment to reconfigure
                                    break 
                                except Exception as set_e:
                                    logger.error(f"Failed to switch transport: {set_e}")
                                    continue
                except Exception as rotate_e:
                    logger.error(f"Error during transport rotation: {rotate_e}")
            else:
                # If it's not a transport error or we're out of attempts, just stop
                break

    logger.error(f"Final failure sending msg to chat {chat_id} after {max_attempts} attempts.")

# ── Bouncer Logic ──

def _check_chat_inactivity(bot, accid, chat_id) -> str:
    # 1. Ensure we have a monitoring start date for this chat
    monitored_since = database.get_chat_monitored_since(chat_id)
    now = time.time()
    if monitored_since is None:
        monitored_since = now
        database.set_chat_monitored_since(chat_id, monitored_since)

    try:
        contacts = bot.rpc.get_chat_contacts(accid, chat_id)
    except Exception as e:
        logger.error(f"Failed to get chat contacts for {chat_id}: {e}")
        return ""

    if len(contacts) <= 2:
        return "" # Ignore 1-on-1 chats and empty groups
        
    inactive_users = []
    lurkers_skipped = 0
    
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
                # Only report "never seen" if we have been monitoring for at least 30 days
                # because for a new bot, EVERYONE who hasn't spoken yet is "never seen".
                if now - monitored_since >= INACTIVITY_SECONDS_THRESHOLD:
                    inactive_users.append(f"- {name} ({address}) - never seen since monitoring started")
                else:
                    lurkers_skipped += 1
            else:
                inactive_duration = now - last_seen
                if inactive_duration > INACTIVITY_SECONDS_THRESHOLD:
                    days_ago = int(inactive_duration / (24 * 3600))
                    date_str = datetime.fromtimestamp(last_seen).strftime("%-d %b %Y")
                    inactive_users.append(f"- {name} ({address}) - last seen {date_str}, {days_ago} days ago")
        except Exception as e:
            logger.error(f"Error checking contact {contact_id}: {e}")

    if not inactive_users:
        if lurkers_skipped > 0:
            monitored_days = int((now - monitored_since) / (24 * 3600))
            return f"ℹ️ **Inactivity Check**\nI've been monitoring this group for {monitored_days} days. {lurkers_skipped} members haven't spoken yet, but I need {INACTIVITY_DAYS_THRESHOLD} days of observation before reporting them as inactive."
        return ""
        
    report = f"⚠️ **Inactivity Report**\nThe following users have not been seen for more than {INACTIVITY_DAYS_THRESHOLD} days:\n\n"
    report += "\n".join(inactive_users)
    
    if lurkers_skipped > 0:
        monitored_days = int((now - monitored_since) / (24 * 3600))
        report += f"\n\n_Note: {lurkers_skipped} more members haven't spoken yet, but they are still in the {INACTIVITY_DAYS_THRESHOLD}-day grace period (monitored for {monitored_days} days)._"
        
    return report

def _background_checker_loop(bot, accid):
    logger.info("Background checker loop started.")
    time.sleep(10) # Wait for bot to connect and sync
    while True:
        try:
            # Run once a day. We check if 24h passed since last run.
            last_run = float(database.get_config("last_daily_run") or 0)
            now = time.time()
            if now - last_run >= 24 * 3600:
                logger.info("Running daily inactivity check...")
                
                # Try multiple methods and signatures to find the one that works with this core version
                chats = []
                methods_to_try = [
                    lambda: bot.rpc.get_chats(accid, None, None, False),
                    lambda: bot.rpc.get_chats(accid),
                    lambda: bot.rpc.get_chat_ids(accid, 0),
                    lambda: bot.rpc.get_chat_ids(accid),
                    lambda: bot.rpc.get_all_chats(accid),
                ]
                
                for method in methods_to_try:
                    try:
                        chats = method()
                        if chats is not None:
                            break
                    except Exception:
                        continue
                else:
                    logger.error("All methods to get chat list failed with 'Method not found' or other error.")
                
                for chat_id in chats:
                    try:
                        chat = bot.rpc.get_basic_chat_info(accid, chat_id)
                        
                        # Check if it's a group chat. 
                        # In some versions it's 'is_multiuser', in others we check 'chat_type'
                        is_group = False
                        if isinstance(chat, dict):
                            is_group = chat.get('is_multiuser') or chat.get('chat_type') != "Single"
                        else:
                            is_group = getattr(chat, 'is_multiuser', False) or getattr(chat, 'chat_type', "") != "Single"
                            
                        if is_group:
                            report = _check_chat_inactivity(bot, accid, chat_id)
                            if report:
                                _send(bot, accid, chat_id, report)
                                time.sleep(1) # Add a small delay to avoid spamming the core
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
    
    # Show configured admin and transports
    admin_email = database.get_config("admin_dc_email")
    admin_fp = database.get_admin_fingerprint()
    if admin_email:
        fp_suffix = f" ({admin_fp[-8:].upper()})" if admin_fp else ""
        print(f"Bot Administrator: {admin_email}{fp_suffix}")
    
    try:
        transports = bot.rpc.list_transports(accid)
        print("\n" + "=" * 50)
        print("Configured Bot Transports (Relays):")
        for t in transports:
            a = t.get('addr', '') if isinstance(t, dict) else getattr(t, 'addr', '')
            print(f" - {a}")
        
        qrdata = bot.rpc.get_chat_securejoin_qr_code(accid, None)
        print("\nTo add this bot, scan the QR code or copy the link:\n")
        
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
    
    # Allow everyone to use /bounce, but with a cooldown (admins are exempt)
    is_admin = _is_dc_admin(bot, accid, msg.from_id)
    now = time.time()
    
    if not is_admin:
        last_bounce = _chat_anti_spam.get(msg.chat_id, 0)
        if now - last_bounce < BOUNCE_COOLDOWN_SECONDS:
            remaining = int((BOUNCE_COOLDOWN_SECONDS - (now - last_bounce)) / 60)
            if remaining < 1:
                 _send(bot, accid, msg.chat_id, "⌛️ Please wait a moment before running another check.")
            else:
                 _send(bot, accid, msg.chat_id, f"⌛️ This group was checked recently. Please wait {remaining}m before running another check.")
            return

    # Update timestamp
    _chat_anti_spam[msg.chat_id] = now

    report = _check_chat_inactivity(bot, accid, msg.chat_id)
    if report:
        _send(bot, accid, msg.chat_id, report)
    else:
        _send(bot, accid, msg.chat_id, "✅ All users are active or this is not a group chat.")

@dc_cli.on(events.NewMessage(command="/help"))
def help_command(bot, accid, event):
    msg = event.msg
    contact = bot.rpc.get_contact(accid, msg.from_id)
    sender_email = contact.address
    
    help_text = (
        f"👋 Hi {sender_email}!\n\n"
        f"I monitor groups and report inactive users (no activity for {INACTIVITY_DAYS_THRESHOLD} days).\n\n"
        f"**Commands:**\n"
        f"/bounce — Trigger an immediate inactivity check in this group.\n"
        f"/help — This message.\n"
        f"/donate — Support development ❤️\n\n"
        f"💡 _Anyone can use /bounce, but no more than once every 10 minutes per group._\n\n"
        f"🤖 **Source:** Run your own bot: https://github.com/mrgluek/deltachat_bouncer"
    )
    
    admin_email = database.get_config("admin_dc_email")
    admin_fp = database.get_admin_fingerprint()
    is_actually_admin = _is_dc_admin(bot, accid, msg.from_id)
    
    if not admin_email:
        help_text += "\n\n/initadmin — Claim bot ownership"
    elif is_actually_admin:
        fp_suffix = f" ({admin_fp[-8:].upper()})" if admin_fp else ""
        help_text += f"\n\n👑 **Admin:** `{admin_email}`{fp_suffix}"
        help_text += "\n\n**Admin Commands:**\n"
        help_text += "/transports — Show mail relays and stats\n"
        help_text += "/rmtransport <email> — Remove a mail relay"
        
    _send(bot, accid, msg.chat_id, help_text)

@dc_cli.on(events.NewMessage(command="/donate"))
def donate_command(bot, accid, event):
    msg = event.msg
    _send(bot, accid, msg.chat_id,
          "❤️ **Support Bot Development**\n\n"
          "If you find this bot useful, you can support its development:\n\n"
          "☕️ Ko-fi: https://ko-fi.com/gluek (🌍 world cards, paypal)\n"
          "🚀 Tribute: https://web.tribute.tg/d/IWb (🇷🇺 russian cards, SBP)\n\n"
          "Thank you! 🙏")

@dc_cli.on(events.NewMessage(command="/transports"))
def transports_command(bot, accid, event):
    msg = event.msg
    if not _is_dc_admin(bot, accid, msg.from_id):
        _send(bot, accid, msg.chat_id, "❌ This command is only for the bot administrator.")
        return
        
    try:
        transports = bot.rpc.list_transports(accid)
        reply = "🤖 **Configured Transports:**\n\n"
        for t in transports:
            a = t.get('addr', '') if isinstance(t, dict) else getattr(t, 'addr', '')
            reply += f"• {a}\n"
        reply += "\nUse `/rmtransport <email>` to remove a relay."
        _send(bot, accid, msg.chat_id, reply)
    except Exception as e:
        _send(bot, accid, msg.chat_id, f"❌ Failed to list transports: {e}")

@dc_cli.on(events.NewMessage(command="/rmtransport"))
def rmtransport_command(bot, accid, event):
    msg = event.msg
    if not _is_dc_admin(bot, accid, msg.from_id):
        _send(bot, accid, msg.chat_id, "❌ This command is only for the bot administrator.")
        return
        
    addr = (event.payload or "").strip()
    if not addr:
        _send(bot, accid, msg.chat_id, "Usage: /rmtransport user@example.com")
        return

    try:
        transports = bot.rpc.list_transports(accid)
        if len(transports) <= 1:
            _send(bot, accid, msg.chat_id, "❌ Cannot remove the last transport.")
            return
        
        bot.rpc.delete_transport(accid, addr)
        _send(bot, accid, msg.chat_id, f"✅ Transport `{addr}` removed.")
    except Exception as e:
        _send(bot, accid, msg.chat_id, f"❌ Failed to remove transport: {e}")

if __name__ == "__main__":
    import sys
    
    # Handle 'init transport' CLI command
    if len(sys.argv) > 2 and sys.argv[1] == "init" and sys.argv[2] == "transport":
        if len(sys.argv) < 5:
            print("Usage: python bot.py init transport <email> <password>")
            sys.exit(1)
            
        addr, password = sys.argv[3], sys.argv[4]
        
        # We need to manually initialize RPC to add transport without starting the bot
        from deltachat2 import Rpc, IOTransport
        from appdirs import user_config_dir
        
        config_dir = user_config_dir("bouncer")
        accounts_dir = os.path.join(config_dir, "accounts")
        
        try:
            with IOTransport(accounts_dir=accounts_dir) as trans:
                rpc = Rpc(trans)
                accids = rpc.get_all_account_ids()
                if not accids:
                    print("Error: No accounts configured. Run 'python bot.py init addr password' first.")
                    sys.exit(1)
                    
                rpc.add_or_update_transport(accids[0], {"addr": addr, "password": password})
                print(f"Success: Backup transport {addr} added.")
        except Exception as e:
            print(f"Error adding transport: {e}")
            sys.exit(1)
        sys.exit(0)

    if len(sys.argv) == 1:
        sys.argv.append("serve")
    dc_cli.start()
