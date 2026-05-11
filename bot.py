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
import tempfile

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
_chat_relays_anti_spam: dict[int, float] = {}
BOUNCE_COOLDOWN_SECONDS = 600  # 10 minutes

REGULAR_MAIL_DOMAINS = {
    "yandex.ru", "yandex.com", "ya.ru",
    "mail.ru", "list.ru", "bk.ru", "inbox.ru", "internet.ru",
    "rambler.ru"
}

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
            
            logger.info(f"Admin check: Fingerprint mismatch or missing for {contact_id}")
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

def _get_top_posters(bot, accid, chat_id, limit=10, hours=24):
    """Return top posters in the given chat for the last N hours."""
    now = time.time()
    since = now - (hours * 3600)
    
    try:
        # Get all message IDs in the chat
        msg_ids = bot.rpc.get_message_ids(accid, chat_id, False, False)
        
        counts = {} # {contact_id: count}
        # Iterate backwards until we hit the time limit
        for msg_id in reversed(msg_ids):
            try:
                # We need the timestamp and from_id
                msg = bot.rpc.get_message(accid, msg_id)
                if msg.timestamp < since:
                    break
                
                # Ignore system contacts (ID <= 9)
                if msg.from_id > 9:
                    counts[msg.from_id] = counts.get(msg.from_id, 0) + 1
            except Exception:
                continue
        
        # Sort by count
        sorted_posters = sorted(counts.items(), key=lambda x: x[1], reverse=True)
        return sorted_posters[:limit]
    except Exception as e:
        logger.error(f"Error getting top posters for chat {chat_id}: {e}")
        return []

# ── Bouncer Logic ──

def _get_top_posters_report(bot, accid, chat_id):
    """Generate a formatted report of top posters."""
    top_posters = _get_top_posters(bot, accid, chat_id)
    if not top_posters:
        return "ℹ️ No activity recorded in this chat in the last 24 hours."
        
    report = "🏆 **Top 10 Posters (last 24h):**\n"
    medals = ["🥇", "🥈", "🥉", "4.", "5.", "6.", "7.", "8.", "9.", "10."]
    for i, (contact_id, count) in enumerate(top_posters):
        try:
            contact = bot.rpc.get_contact(accid, contact_id)
            name = contact.name or contact.display_name or "Unknown"
            medal = medals[i] if i < len(medals) else f"{i+1}."
            report += f"{medal} **{name}**: {count} msgs\n"
        except:
            continue
    return report

def _check_chat_inactivity(bot, accid, chat_id, daily=False) -> str:
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
        
    total_members = 0
    active_count = 0
    inactive_users = []
    lurkers_skipped = 0
    
    for contact_id in contacts:
        if contact_id == DC_CONTACT_ID_SELF:
            continue
            
        try:
            contact = bot.rpc.get_contact(accid, contact_id)
            if contact.address and contact.address.lower() == "deltachat@system.local":
                continue # Ignore system contact

            total_members += 1
            if isinstance(contact, dict):
                last_seen = contact.get("last_seen", 0)
            else:
                last_seen = getattr(contact, "last_seen", 0)
            
            # Use name if available, otherwise address, otherwise "Unknown"
            name = contact.name or contact.display_name or "Unknown"
            address = contact.address or "no_email@example.com"
            
            if last_seen == 0:
                # Only report "never seen" if we have been monitoring for at least 30 days
                if now - monitored_since >= INACTIVITY_SECONDS_THRESHOLD:
                    inactive_users.append(f"• /contact{contact_id} **{name}** ({address}) [never seen]")
                else:
                    lurkers_skipped += 1
            else:
                inactive_duration = now - last_seen
                if inactive_duration > INACTIVITY_SECONDS_THRESHOLD:
                    days_ago = int(inactive_duration / (24 * 3600))
                    date_str = datetime.fromtimestamp(last_seen).strftime("%-d %b %Y")
                    inactive_users.append(f"• /contact{contact_id} **{name}** ({address}) [last seen {date_str}, {days_ago}d ago]")
                else:
                    active_count += 1
        except Exception as e:
            logger.error(f"Error checking contact {contact_id}: {e}")

    monitored_days = int((now - monitored_since) / (24 * 3600))
    
    if not daily and not inactive_users:
        if lurkers_skipped > 0:
            return f"ℹ️ **Inactivity Check**\nI've been monitoring this group for {monitored_days} days. {lurkers_skipped} members haven't spoken yet, but I need {INACTIVITY_DAYS_THRESHOLD} days of observation before reporting them as inactive."
        return ""
        
    # Build the report
    title = "📊 **Daily Status Report**" if daily else "⚠️ **Inactivity Report**"
    report = f"{title}\n"
    report += f"Monitoring this group for {monitored_days} days.\n\n"
    report += f"• Total members: {total_members}\n"
    report += f"• Active recently: {active_count}\n"
    
    if inactive_users:
        report += f"• ⚠️ Inactive (>30d): {len(inactive_users)}\n\n"
        report += "\n".join(inactive_users)
    else:
        report += "• Inactive (>30d): 0\n"

    if lurkers_skipped > 0:
        report += f"\n\n_Note: {lurkers_skipped} more members haven't spoken yet, but they are still in the {INACTIVITY_DAYS_THRESHOLD}-day grace period._"

    # Add Top 10 Posters only for daily reports (manual /bounce handles it separately)
    if daily:
        top_report = _get_top_posters_report(bot, accid, chat_id)
        if "🏆" in top_report:
            report += "\n\n" + top_report

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
                chats = None
                methods_to_try = [
                    ("get_chat_list(accid)", lambda: bot.rpc.get_chat_list(accid)),
                    ("get_chat_list(accid, 0, None, None, None)", lambda: bot.rpc.get_chat_list(accid, 0, None, None, None)),
                    ("get_chat_list_ids(accid, 0, None, 0, 1000)", lambda: bot.rpc.get_chat_list_ids(accid, 0, None, 0, 1000)),
                    ("get_chat_list_ids(accid, 0, None)", lambda: bot.rpc.get_chat_list_ids(accid, 0, None)),
                    ("get_chat_ids(accid, 0, None)", lambda: bot.rpc.get_chat_ids(accid, 0, None)),
                    ("get_chat_ids(accid, 0)", lambda: bot.rpc.get_chat_ids(accid, 0)),
                    ("get_chat_ids(accid)", lambda: bot.rpc.get_chat_ids(accid)),
                    ("get_chats(accid, 0, None)", lambda: bot.rpc.get_chats(accid, 0, None)),
                    ("get_chats(accid, None, None, False)", lambda: bot.rpc.get_chats(accid, None, None, False)),
                    ("get_chats(accid)", lambda: bot.rpc.get_chats(accid)),
                    ("get_all_chats(accid)", lambda: bot.rpc.get_all_chats(accid)),
                ]
                
                for name, method in methods_to_try:
                    try:
                        chats = method()
                        if chats is not None:
                            logger.info(f"Successfully retrieved chat list using {name}")
                            break
                    except Exception as e:
                        # Log error details to help diagnose RPC mismatch
                        logger.info(f"Discovery: Method {name} failed: {e}")
                        continue
                
                if chats is None:
                    logger.error("All methods to get chat list failed. Check core version and RPC compatibility.")
                    chats = [] # Prevent crash in the loop below
                
                for item in chats:
                    try:
                        # Depending on the core version, the RPC might return a list of integers or a list of dicts/objects
                        chat_id = item if isinstance(item, int) else (item.get("id") if isinstance(item, dict) else getattr(item, "id", None))
                        if chat_id is None:
                            continue
                            
                        chat = bot.rpc.get_basic_chat_info(accid, chat_id)
                        
                        # Check if it's a group chat. 
                        # In some versions it's 'is_multiuser', in others we check 'chat_type'
                        is_group = False
                        if isinstance(chat, dict):
                            is_group = chat.get('is_multiuser') or chat.get('chat_type') != "Single"
                        else:
                            is_group = getattr(chat, 'is_multiuser', False) or getattr(chat, 'chat_type', "") != "Single"
                            
                        if is_group:
                            report = _check_chat_inactivity(bot, accid, chat_id, daily=True)
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
        # For manual /bounce, we also include top posters at the end
        top_report = _get_top_posters_report(bot, accid, msg.chat_id)
        if "🏆" in top_report:
            report += "\n\n" + top_report
        _send(bot, accid, msg.chat_id, report)
    else:
        _send(bot, accid, msg.chat_id, "✅ All users are active or this is not a group chat.")

@dc_cli.on(events.NewMessage(command="/top"))
def top_command(bot, accid, event):
    msg = event.msg
    # 10-minute cooldown similar to other commands
    last_check = _chat_anti_spam.get(msg.chat_id, 0) # Reuse bounce cooldown for simplicity
    now = time.time()
    
    if not _is_dc_admin(bot, accid, msg.from_id):
        if now - last_check < BOUNCE_COOLDOWN_SECONDS:
             _send(bot, accid, msg.chat_id, "⌛️ Please wait a moment before running another check.")
             return
    
    _chat_anti_spam[msg.chat_id] = now
    _send(bot, accid, msg.chat_id, _get_top_posters_report(bot, accid, msg.chat_id))

@dc_cli.on(events.NewMessage(command="/help"))
def help_command(bot, accid, event):
    msg = event.msg
    contact = bot.rpc.get_contact(accid, msg.from_id)
    sender_email = contact.address
    
    help_text = (
        f"👋 Hi {sender_email}!\n\n"
        f"I monitor groups and report inactive users (no activity for {INACTIVITY_DAYS_THRESHOLD} days).\n\n"
        f"**Commands:**\n"
        f"/bounce — Trigger an inactivity check in the current group.\n"
        f"/relays — Find group members using regular mail providers.\n"
        f"/top    — Show top 10 posters in the last 24 hours.\n"
        f"/contact<ID> — Get a contact object for the given ID.\n"
        f"/help   — This message.\n\n"
        f"/donate — Support development ❤️\n\n"
        f"💡 _Commands have a 10-minute cooldown per group (except for admins)._\n\n"
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

@dc_cli.on(events.NewMessage(command="/relays"))
def relays_command(bot, accid, event):
    """Check group members for regular mail providers, including secondary transports."""
    msg = event.msg
    
    # Allow everyone to use /relays, but with a cooldown (admins are exempt)
    is_admin = _is_dc_admin(bot, accid, msg.from_id)
    now = time.time()
    
    if not is_admin:
        last_check = _chat_relays_anti_spam.get(msg.chat_id, 0)
        if now - last_check < BOUNCE_COOLDOWN_SECONDS:
            remaining = int((BOUNCE_COOLDOWN_SECONDS - (now - last_check)) / 60)
            if remaining < 1:
                 _send(bot, accid, msg.chat_id, "⌛️ Please wait a moment before running another check.")
            else:
                 _send(bot, accid, msg.chat_id, f"⌛️ Relay check was done recently. Please wait {remaining}m before running another check.")
            return

    # Update timestamp
    _chat_relays_anti_spam[msg.chat_id] = now

    try:
        contacts = bot.rpc.get_chat_contacts(accid, msg.chat_id)
        found_users = []
        for contact_id in contacts:
            if contact_id == DC_CONTACT_ID_SELF:
                continue
            
            contact = bot.rpc.get_contact(accid, contact_id)
            if isinstance(contact, dict):
                primary_addr = contact.get("address", "no_address")
                name = contact.get("name") or contact.get("display_name") or "Unknown"
                last_seen = contact.get("last_seen", 0)
            else:
                primary_addr = getattr(contact, "address", "no_address")
                name = getattr(contact, "name", None) or getattr(contact, "display_name", None) or "Unknown"
                last_seen = getattr(contact, "last_seen", 0)
            
            if last_seen == 0:
                last_seen_str = "never seen"
            else:
                date_str = datetime.fromtimestamp(last_seen).strftime("%-d %b %Y")
                last_seen_str = date_str

            # Get all addresses associated with this contact from various sources
            all_addresses = {primary_addr.lower().strip()}
            try:
                # Encryption info typically lists all associated addresses (transports)
                enc_info = bot.rpc.get_contact_encryption_info(accid, contact_id)
                if enc_info:
                    # Extract emails using a standard pattern
                    emails = re.findall(r'[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+', enc_info)
                    for e in emails:
                        all_addresses.add(e.lower().strip())
            except Exception:
                pass

            matching_addresses = []
            for addr in all_addresses:
                if '@' in addr:
                    domain = addr.split('@')[-1]
                    if domain in REGULAR_MAIL_DOMAINS:
                        matching_addresses.append(addr)
            
            if matching_addresses:
                found_users.append({
                    "name": name,
                    "primary": primary_addr,
                    "last_seen": last_seen_str,
                    "contact_id": contact_id,
                    "matches": sorted(list(set(matching_addresses)))
                })

        if not found_users:
            _send(bot, accid, msg.chat_id, "✅ No group members using public Russian mail providers found.")
            return

        reply = f"⚠️ **Members with public Russian mail providers ({len(found_users)}):**\n\n"
        reply += "These users may experience delivery issues in large groups:\n\n"
        for user in found_users:
            matches_str = ", ".join(user["matches"])
            reply += f"• /contact{user['contact_id']} **{user['name']}** ({user['primary']}) [{user['last_seen']}] — {matches_str}\n"
        
        reply += "\nConsider asking them to use chatmail relays or private servers."
        _send(bot, accid, msg.chat_id, reply)

    except Exception as e:
        logger.error(f"Error in /relays command: {e}")
        _send(bot, accid, msg.chat_id, f"❌ Failed to check members: {e}")

@dc_cli.on(events.NewMessage)
def handle_all_messages(bot, accid, event):
    """Handle dynamic commands like /contact123"""
    msg = event.msg
    text = (msg.text or "").strip()
    if text.startswith("/contact"):
        logger.info(f"DEBUG: Processing contact request: {text}")
        try:
            id_str = text[8:].strip()
            if id_str.isdigit():
                contact_id = int(id_str)
                # Verify contact exists and is in the same chat
                try:
                    chat_contacts = bot.rpc.get_chat_contacts(accid, msg.chat_id)
                    # Convert to set of ints for robust comparison
                    chat_contact_ids = set()
                    for c in chat_contacts:
                        try:
                            chat_contact_ids.add(int(c))
                        except (ValueError, TypeError):
                            continue
                    
                    if contact_id in chat_contact_ids:
                        logger.info(f"DEBUG: Sharing contact {contact_id} in chat {msg.chat_id}")
                        temp_path = None
                        try:
                            # Generate vCard content using core RPC
                            vcard_content = bot.rpc.make_vcard(accid, [contact_id])
                            
                            # Create a temporary file to hold the vCard
                            with tempfile.NamedTemporaryFile(suffix=".vcf", mode="w", delete=False) as f:
                                f.write(vcard_content)
                                temp_path = f.name
                            
                            # Send the vCard as a message
                            bot.rpc.send_msg(accid, msg.chat_id, MsgData(file=temp_path, viewtype="Vcard"))
                            logger.info(f"DEBUG: Successfully shared contact {contact_id}")
                        finally:
                            if temp_path and os.path.exists(temp_path):
                                try:
                                    os.unlink(temp_path)
                                except Exception as e:
                                    logger.warning(f"DEBUG: Failed to delete temp vCard file {temp_path}: {e}")
                    else:
                        logger.warning(f"DEBUG: User {msg.from_id} tried to access contact {contact_id} not in chat {msg.chat_id}")
                except Exception as e:
                    logger.error(f"DEBUG: Error in contact security check: {e}")
        except Exception as e:
            logger.error(f"DEBUG: Error parsing contact ID: {e}")

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
