import asyncio
import io
import logging
import os
import re
import threading
import time
from datetime import datetime

from deltachat2 import events, MsgData, SystemMessageType
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
INACTIVITY_DAYS_THRESHOLD = 14
INACTIVITY_SECONDS_THRESHOLD = INACTIVITY_DAYS_THRESHOLD * 24 * 3600

# Anti-spam: {chat_id: timestamp}
_chat_anti_spam: dict[int, float] = {}
_chat_relays_anti_spam: dict[int, float] = {}
_chat_search_anti_spam: dict[int, float] = {}
BOUNCE_COOLDOWN_SECONDS = 60   # 1 minute for general commands (/bounce, /top, /relays)
SEARCH_COOLDOWN_SECONDS = 10   # 10 seconds for /search command

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
            logger.debug(f"Detected bot's own fingerprints from enc_info: {[f[-8:] for f in self_fps]}")
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

    actual_attempts = 0
    for attempt in range(max_attempts):
        actual_attempts = attempt + 1
        try:
            bot.rpc.send_msg(accid, chat_id, msg_data)
            
            # Track success
            addr = "unknown"
            try:
                addr = bot.rpc.get_config(accid, "configured_addr") or bot.rpc.get_config(accid, "addr") or "unknown"
                if addr != "unknown":
                    database.increment_transport_sent(addr)
            except Exception:
                pass
                
            return # Success!
        except Exception as e:
            error_str = str(e).lower()
            if "not a member of the chat" in error_str:
                logger.warning(f"Cannot send message to chat {chat_id}: bot is not a member of the chat.")
                return
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

    logger.error(f"Final failure sending msg to chat {chat_id} after {actual_attempts} attempts.")

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
    
    if not inactive_users:
        if lurkers_skipped > 0:
            return f"ℹ️ **Inactivity Check**\nI've been monitoring this group for {monitored_days} days. {lurkers_skipped} members haven't spoken yet, but I need {INACTIVITY_DAYS_THRESHOLD} days of observation before reporting them as inactive."
        return ""
        
    # Build the report
    report = "⚠️ **Inactivity Report**\n"
    report += f"Monitoring this group for {monitored_days} days.\n\n"
    report += f"• Total members: {total_members}\n"
    report += f"• Active recently: {active_count}\n"
    
    if inactive_users:
        report += f"• ⚠️ Inactive (>{INACTIVITY_DAYS_THRESHOLD}d): {len(inactive_users)}\n\n"
        report += "\n".join(inactive_users)
    else:
        report += f"• Inactive (>{INACTIVITY_DAYS_THRESHOLD}d): 0\n"

    if lurkers_skipped > 0:
        report += f"\n\n_Note: {lurkers_skipped} more members haven't spoken yet, but they are still in the {INACTIVITY_DAYS_THRESHOLD}-day grace period._"

    return report

def _background_monitor_loop(bot, accid):
    logger.info("Background monitor task started.")
    time.sleep(10) # Wait for bot to connect and sync
    while True:
        try:
            try:
                chats = bot.rpc.get_chatlist_entries(accid, None, None, None)
                logger.info(f"Background check: tracking {len(chats)} chats")
            except Exception as e:
                logger.error(f"get_chatlist_entries failed: {e}")
                chats = []
            
            GROUP_TYPES = {"Group", "Mailinglist", "OutBroadcast", "InBroadcast"}

            for chat_id in chats:
                try:
                    if not isinstance(chat_id, int):
                        continue

                    chat = bot.rpc.get_basic_chat_info(accid, chat_id)
                    chat_type = chat.get('chat_type', 'Single') if isinstance(chat, dict) else getattr(chat, 'chat_type', 'Single')

                    if str(chat_type) in GROUP_TYPES:
                        # Ensure monitored_since is set for new groups
                        if database.get_chat_monitored_since(chat_id) is None:
                            logger.info(f"Started monitoring new group: {chat_id}")
                            database.set_chat_monitored_since(chat_id, time.time())
                except Exception as e:
                    logger.error(f"Error checking chat {chat_id} in background monitor: {e}")
        except Exception as e:
            logger.error(f"Background loop error: {e}")
            
        time.sleep(3600) # Check every hour for new chats


resilient_lock = threading.Lock()

def _setup_resilient_mode(bot):
    original_send_msg = bot.rpc.send_msg

    def patched_send_msg(account_id, chat_id, msg_data):
        try:
            is_resilient = database.get_config("resilient") == "1"
        except Exception:
            is_resilient = False

        if not is_resilient:
            return original_send_msg(account_id, chat_id, msg_data)

        try:
            transports = bot.rpc.list_transports(account_id)
        except Exception:
            transports = []

        if len(transports) <= 1:
            return original_send_msg(account_id, chat_id, msg_data)

        initial_addr = None
        try:
            initial_addr = bot.rpc.get_config(account_id, "configured_addr") or bot.rpc.get_config(account_id, "addr")
        except Exception:
            pass

        # 1. Send the message normally via the current primary transport (non-blocking queueing)
        try:
            msg_id = original_send_msg(account_id, chat_id, msg_data)
            bot.logger.info(f"Resilient send: initial msg queued with ID {msg_id} on transport {initial_addr}.")
        except Exception as send_err:
            bot.logger.error(f"Resilient send: failed to queue initial message: {send_err}")
            return None

        # Background worker to handle resending to other transports sequentially
        def bg_resend_worker(m_id, init_addr, t_list):
            bot.logger.info(f"Resilient send: starting background sender for msg {m_id}")
            with resilient_lock:
                bot.logger.info(f"Resilient send bg: waiting for initial delivery of msg {m_id} on {init_addr}...")
                start_time = time.time()
                delivered = False
                while time.time() - start_time < 10:
                    try:
                        msg_snapshot = bot.rpc.get_message(account_id, m_id)
                        state = msg_snapshot.get('state') if isinstance(msg_snapshot, dict) else getattr(msg_snapshot, 'state', None)
                        if state in (26, 28):
                            bot.logger.info(f"Resilient send bg: initial msg {m_id} delivered successfully on {init_addr}.")
                            delivered = True
                            break
                        if state == 24:
                            bot.logger.warning(f"Resilient send bg: initial msg {m_id} failed on {init_addr}.")
                            break
                    except Exception as poll_err:
                        bot.logger.debug(f"Resilient send bg initial poll error: {poll_err}")
                    time.sleep(0.5)

                if not delivered:
                    bot.logger.warning(f"Resilient send bg: initial msg {m_id} did not deliver on {init_addr} within timeout.")

                # 2. Resend on all other transports
                for t in t_list:
                    t_addr = t.get('addr') if isinstance(t, dict) else getattr(t, 'addr', None)
                    if not t_addr or (init_addr and t_addr.lower() == init_addr.lower()):
                        continue

                    bot.logger.info(f"Resilient send bg: switching primary transport to {t_addr}")
                    try:
                        bot.rpc.set_config(account_id, "configured_addr", t_addr)
                        time.sleep(1)
                    except Exception as switch_err:
                        bot.logger.error(f"Resilient send bg: failed to switch transport to {t_addr}: {switch_err}")
                        continue

                    try:
                        bot.logger.info(f"Resilient send bg: resending msg {m_id} on transport {t_addr}...")
                        bot.rpc.resend_messages(account_id, [m_id])

                        # Wait up to 10 seconds for the resent message to be delivered/failed
                        start_time = time.time()
                        delivered = False
                        while time.time() - start_time < 10:
                            try:
                                msg_snapshot = bot.rpc.get_message(account_id, m_id)
                                state = msg_snapshot.get('state') if isinstance(msg_snapshot, dict) else getattr(msg_snapshot, 'state', None)
                                if state in (26, 28):
                                    bot.logger.info(f"Resilient send bg: msg {m_id} delivered successfully on {t_addr}.")
                                    delivered = True
                                    break
                                if state == 24:
                                    bot.logger.warning(f"Resilient send bg: msg {m_id} failed on {t_addr}.")
                                    break
                            except Exception as poll_err:
                                bot.logger.debug(f"Resilient send bg poll error: {poll_err}")
                            time.sleep(0.5)

                        if not delivered:
                            bot.logger.warning(f"Resilient send bg: msg {m_id} did not deliver on {t_addr} within timeout.")
                    except Exception as resend_err:
                        bot.logger.error(f"Resilient send bg: failed to resend message on transport {t_addr}: {resend_err}")

                # 3. Restore the initial primary transport configuration
                if init_addr:
                    try:
                        bot.logger.info(f"Resilient send bg: restoring initial primary transport to {init_addr}")
                        bot.rpc.set_config(account_id, "configured_addr", init_addr)
                    except Exception as restore_err:
                        bot.logger.error(f"Resilient send bg: failed to restore transport to {init_addr}: {restore_err}")

        # Start the background thread for resilient sending
        threading.Thread(target=bg_resend_worker, args=(msg_id, initial_addr, transports), daemon=True).start()

        return msg_id

    bot.rpc.send_msg = patched_send_msg

@dc_cli.on_init
def on_init(bot, args):
    global dc_bot_instance, dc_accid
    bot.logger.info("Initializing Bouncer Bot...")
    dc_bot_instance = bot
    _setup_resilient_mode(bot)
    
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
            
        # Optimize storage (disable auto-download of attachments, auto-delete messages after 36 hours to buffer /top 24h stats)
        try:
            bot.rpc.set_config(dc_accid, "download_limit", "1")
            bot.rpc.set_config(dc_accid, "delete_device_after", "129600") # 36 hours (1.5 days) to buffer /top 24h stats
            bot.logger.info("Configured auto-download limit (1 byte) and message deletion (36 hours) in on_init.")
        except Exception as e:
            bot.logger.warning(f"Could not configure storage optimization in on_init: {e}")

@dc_cli.on_start
def on_start(bot, args):
    global dc_bot_instance, dc_accid
    dc_bot_instance = bot
    
    # Monkey-patch bot._process_messages to allow processing of system/info messages
    from deltachat2 import SpecialContactId
    from deltachat2.transport import JsonRpcError

    def custom_process_messages(accid: int, retry=True) -> None:
        try:
            for msgid in bot.rpc.get_next_msgs(accid):
                msg = bot.rpc.get_message(accid, msgid)
                outgoing = msg.from_id == SpecialContactId.SELF
                logger.info(f"custom_process_messages: msgid={msgid}, from_id={msg.from_id}, is_info={msg.is_info}, text={msg.text!r}")
                # Process the message if it's outgoing, from a contact > LAST_SPECIAL, or a system/info message
                if outgoing or msg.from_id > SpecialContactId.LAST_SPECIAL or msg.is_info:
                    logger.info(f"custom_process_messages: calling _on_new_msg for msgid={msgid}")
                    bot._on_new_msg(accid, msg)
                bot.rpc.set_config(accid, "last_msg_id", str(msgid))
        except JsonRpcError as err:
            logger.exception(err)
            if retry:
                custom_process_messages(accid, False)

    bot._process_messages = custom_process_messages

    accounts = bot.rpc.get_all_account_ids()
    if not accounts:
        logger.error("No accounts found.")
        return
        
    accid = accounts[0]
    dc_accid = accid
    
    logger.info(f"Bouncer bot started with accid {accid}.")
    
    # Ensure storage optimization settings are active
    try:
        bot.rpc.set_config(accid, "download_limit", "1")
        bot.rpc.set_config(accid, "delete_device_after", "129600") # 36 hours (1.5 days) to buffer /top 24h stats
        logger.info("Successfully set auto-download limit to 1 byte and delete_device_after to 36 hours to optimize storage.")
    except Exception as e:
        logger.error(f"Failed to set storage optimization settings in on_start: {e}")
    
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
    
    t = threading.Thread(target=_background_monitor_loop, args=(bot, accid), daemon=True)
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
        diff = now - last_bounce
        if diff < BOUNCE_COOLDOWN_SECONDS:
            remaining_sec = max(1, int(BOUNCE_COOLDOWN_SECONDS - diff))
            if remaining_sec < 60:
                 _send(bot, accid, msg.chat_id, f"⌛️ This group was checked recently. Please wait {remaining_sec}s before running another check.")
            else:
                 remaining_min = int(remaining_sec / 60)
                 _send(bot, accid, msg.chat_id, f"⌛️ This group was checked recently. Please wait {remaining_min}m before running another check.")
            return

    # Update timestamp
    _chat_anti_spam[msg.chat_id] = now

    report = _check_chat_inactivity(bot, accid, msg.chat_id)
    if report:
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
        diff = now - last_check
        if diff < BOUNCE_COOLDOWN_SECONDS:
             remaining_sec = max(1, int(BOUNCE_COOLDOWN_SECONDS - diff))
             if remaining_sec < 60:
                  _send(bot, accid, msg.chat_id, f"⌛️ Please wait {remaining_sec}s before running another check.")
             else:
                  remaining_min = int(remaining_sec / 60)
                  _send(bot, accid, msg.chat_id, f"⌛️ Please wait {remaining_min}m before running another check.")
             return
    
    _chat_anti_spam[msg.chat_id] = now
    _send(bot, accid, msg.chat_id, _get_top_posters_report(bot, accid, msg.chat_id))

@dc_cli.on(events.NewMessage(command="/search"))
def search_command(bot, accid, event):
    msg = event.msg
    
    # 1. Check if this is a global search (admin in private chat) or a local group search
    try:
        chat = bot.rpc.get_basic_chat_info(accid, msg.chat_id)
        chat_type = chat.get('chat_type', 'Single') if isinstance(chat, dict) else getattr(chat, 'chat_type', 'Single')
    except Exception as e:
        logger.error(f"Failed to get chat info for {msg.chat_id}: {e}")
        chat_type = 'Single'

    is_admin = _is_dc_admin(bot, accid, msg.from_id)
    
    global_search = False
    if str(chat_type) not in {"Group", "Mailinglist", "OutBroadcast", "InBroadcast"}:
        if not is_admin:
            _send(bot, accid, msg.chat_id, "ℹ️ Global search is only available for the bot administrator.")
            return
        global_search = True

    self_addr = ""
    try:
        self_addr = (bot.rpc.get_config(accid, "configured_addr") or bot.rpc.get_config(accid, "addr") or "").lower().strip()
    except Exception:
        pass

    # 2. Check cooldown (admins/global searches are exempt)
    now = time.time()
    if not global_search and not is_admin:
        last_search = _chat_search_anti_spam.get(msg.chat_id, 0)
        diff = now - last_search
        if diff < SEARCH_COOLDOWN_SECONDS:
            remaining_sec = max(1, int(SEARCH_COOLDOWN_SECONDS - diff))
            if remaining_sec < 60:
                _send(bot, accid, msg.chat_id, f"⌛️ A search was performed recently in this group. Please wait {remaining_sec}s before running another search.")
            else:
                remaining_min = int(remaining_sec / 60)
                _send(bot, accid, msg.chat_id, f"⌛️ A search was performed recently in this group. Please wait {remaining_min}m before running another search.")
            return

    # 3. Parse and validate queries
    payload_str = event.payload.strip() if event.payload else ""
    queries = [q.lower() for q in payload_str.split() if q]

    # Check if this is a reply to another message
    has_reply = False
    quoted_emails = []
    if hasattr(msg, "quote") and msg.quote and isinstance(msg.quote, dict):
        quoted_text = msg.quote.get("text", "")
        if quoted_text:
            has_reply = True
            # Extract email addresses and domain-like patterns (e.g. @domain.com or full emails) from the quoted text
            found_emails = re.findall(r'(?:[a-zA-Z0-9_.+-]+)?@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+', quoted_text)
            for email in found_emails:
                email_lower = email.lower().strip()
                if email_lower not in queries:
                    quoted_emails.append(email_lower)
            
    queries.extend(quoted_emails)

    if not queries:
        if has_reply:
            _send(bot, accid, msg.chat_id, "ℹ️ No email addresses or domains found in the quoted message to search for.")
        else:
            _send(bot, accid, msg.chat_id, "Usage: /search <query1> <query2> ... (supports domains and partial matches, e.g. /search @testrun.org) or reply to a message containing email addresses or domains.")
        return

    # Update cooldown timestamp for local searches
    if not global_search:
        _chat_search_anti_spam[msg.chat_id] = now

    # Gather list of chats to search in
    group_chats = []
    GROUP_TYPES = {"Group", "Mailinglist", "OutBroadcast", "InBroadcast"}
    
    if global_search:
        try:
            all_chats = bot.rpc.get_chatlist_entries(accid, None, None, None)
        except Exception as e:
            logger.error(f"Failed to get chatlist entries: {e}")
            all_chats = []
            
        for chat_id in all_chats:
            if not isinstance(chat_id, int):
                continue
            try:
                c_info = bot.rpc.get_basic_chat_info(accid, chat_id)
                c_type = c_info.get('chat_type', 'Single') if isinstance(c_info, dict) else getattr(c_info, 'chat_type', 'Single')
                if str(c_type) in GROUP_TYPES:
                    chat_name = c_info.get('name') if isinstance(c_info, dict) else getattr(c_info, 'name', f"Group {chat_id}")
                    group_chats.append((chat_id, chat_name or f"Group {chat_id}"))
            except Exception as e:
                logger.error(f"Failed to get basic chat info for chat {chat_id} in global search: {e}")
    else:
        # Local group search
        try:
            c_info = bot.rpc.get_basic_chat_info(accid, msg.chat_id)
            chat_name = c_info.get('name') if isinstance(c_info, dict) else getattr(c_info, 'name', f"Group {msg.chat_id}")
            group_chats.append((msg.chat_id, chat_name or f"Group {msg.chat_id}"))
        except Exception as e:
            logger.error(f"Failed to get basic chat info for local chat {msg.chat_id}: {e}")
            group_chats.append((msg.chat_id, f"Group {msg.chat_id}"))

    found_users_map = {}
    
    for chat_id, chat_name in group_chats:
        try:
            contacts = bot.rpc.get_chat_contacts(accid, chat_id)
        except Exception as e:
            logger.error(f"Failed to get chat contacts for chat {chat_id}: {e}")
            continue

        for contact_id in contacts:
            if contact_id == DC_CONTACT_ID_SELF:
                continue
            
            # If already processed, we just record the group name
            if contact_id in found_users_map:
                if chat_name not in found_users_map[contact_id]["groups"]:
                    found_users_map[contact_id]["groups"].append(chat_name)
                continue

            try:
                contact = bot.rpc.get_contact(accid, contact_id)
                if contact.address and contact.address.lower() == "deltachat@system.local":
                    continue # Ignore system contact

                # Get name and primary address
                name = contact.name or contact.display_name or "Unknown"
                primary_addr = contact.address or ""
                
                # Get all addresses associated with this contact from various sources
                all_addresses = {primary_addr.lower().strip()} if primary_addr else set()
                try:
                    enc_info = bot.rpc.get_contact_encryption_info(accid, contact_id)
                    if enc_info:
                        emails = re.findall(r'[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+', enc_info)
                        for e in emails:
                            addr_lower = e.lower().strip()
                            if addr_lower != self_addr and addr_lower != "deltachat@system.local":
                                all_addresses.add(addr_lower)
                except Exception:
                    pass

                # Substring match (case-insensitive) for any query in any of the addresses
                matched = False
                for query in queries:
                    for addr in all_addresses:
                        if query in addr:
                            matched = True
                            break
                    if matched:
                        break
                
                if matched:
                    if isinstance(contact, dict):
                        last_seen = contact.get("last_seen", 0)
                    else:
                        last_seen = getattr(contact, "last_seen", 0)

                    if last_seen == 0:
                        status = "never seen"
                    else:
                        date_str = datetime.fromtimestamp(last_seen).strftime("%-d %b %Y")
                        days_ago = int((now - last_seen) / (24 * 3600))
                        status = f"last seen {date_str}, {days_ago}d ago"

                    # Format the list of addresses (primary first)
                    display_addrs = [primary_addr] if primary_addr else []
                    for addr in sorted(all_addresses):
                        if primary_addr and addr != primary_addr.lower().strip() and addr not in display_addrs:
                            display_addrs.append(addr)
                        elif not primary_addr and addr not in display_addrs:
                            display_addrs.append(addr)
                    addrs_str = ", ".join(display_addrs)

                    found_users_map[contact_id] = {
                        "name": name,
                        "addrs_str": addrs_str,
                        "status": status,
                        "groups": [chat_name]
                    }
            except Exception as e:
                logger.error(f"Error checking contact {contact_id} in search: {e}")

    queries_str = ", ".join(f"'{q}'" for q in queries)
    
    if found_users_map:
        if global_search:
            reply = f"🔍 **Global Search Results for {queries_str} ({len(found_users_map)}):**\n\n"
            for contact_id, info in found_users_map.items():
                groups_str = ", ".join(info["groups"])
                reply += f"• /contact{contact_id} **{info['name']}** ({info['addrs_str']}) [{info['status']}]\n  ↳ Groups: {groups_str}\n"
        else:
            reply = f"🔍 **Search Results for {queries_str} ({len(found_users_map)}):**\n\n"
            for contact_id, info in found_users_map.items():
                reply += f"• /contact{contact_id} **{info['name']}** ({info['addrs_str']}) [{info['status']}]\n"
        
        _send(bot, accid, msg.chat_id, reply)
    else:
        if global_search:
            _send(bot, accid, msg.chat_id, f"🔍 No members matching {queries_str} found in any group chats.")
        else:
            _send(bot, accid, msg.chat_id, f"🔍 No members matching {queries_str} found in this group.")

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
        f"/search [query1] ... — Search members by email/domain (e.g. @testrun.org) or reply to a message.\n"
        f"/relays — Find group members using regular mail providers.\n"
        f"/top    — Show top 10 posters in the last 24 hours.\n"
        f"/invite — Generate an invite link/QR code for this group.\n"
        f"/contact<ID> — Get a contact object for the given ID.\n"
        f"/help   — This message.\n"
        f"/chats  — Show catalog of available chats.\n\n"
        f"/donate — Support development ❤️\n\n"
        f"💡 _Commands have a 10-minute cooldown per group (except for admins)._\n\n"
        f"🤖 **Source:** Run your own bot: https://git.gluek.info/gluek/deltachat_bouncer"
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
        help_text += "/transports — Show configured mail relays & stats\n"
        help_text += "/addtransport — Add a backup mail relay\n"
        help_text += "/rmtransport <addr> — Remove a mail relay\n"
        help_text += "/setprimary <addr> — Switch the primary mail relay\n"
        help_text += "/resilient — Toggle resilient sending mode (all relays)\n"
        help_text += "/chatadd [desc] — Add current chat to catalog\n"
        help_text += "/chatremove — Remove current chat from catalog\n"
        help_text += "/private <on/off> — Toggle privacy of current chat\n"
        help_text += "/welcome [on/off/...] — Manage welcome message for new members"
        
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

@dc_cli.on(events.NewMessage(command="/invite"))
def invite_command(bot, accid, event):
    msg = event.msg
    
    # 1. Check if this is a group chat
    try:
        chat = bot.rpc.get_basic_chat_info(accid, msg.chat_id)
        chat_type = chat.get('chat_type', 'Single') if isinstance(chat, dict) else getattr(chat, 'chat_type', 'Single')
    except Exception as e:
        logger.error(f"Failed to get chat info for {msg.chat_id}: {e}")
        return

    if str(chat_type) not in {"Group", "Mailinglist", "OutBroadcast", "InBroadcast"}:
        _send(bot, accid, msg.chat_id, "❌ Invite links can only be generated for group chats.")
        return

    # 2. Check cooldown (admins are exempt)
    is_admin = _is_dc_admin(bot, accid, msg.from_id)
    now = time.time()
    
    if not is_admin:
        last_check = _chat_anti_spam.get(msg.chat_id, 0)
        diff = now - last_check
        if diff < BOUNCE_COOLDOWN_SECONDS:
            remaining_sec = max(1, int(BOUNCE_COOLDOWN_SECONDS - diff))
            _send(bot, accid, msg.chat_id, f"⌛️ Please wait {remaining_sec}s before running another check.")
            return

    # Update cooldown timestamp
    _chat_anti_spam[msg.chat_id] = now

    try:
        # Generate securejoin QR link
        qrdata = bot.rpc.get_chat_securejoin_qr_code(accid, msg.chat_id)
        
        # Clean/Format the invite link
        invite_link = qrdata
        if qrdata.startswith("OPEN-CHAT:"):
            invite_link = "https://i.delta.chat/#" + qrdata[10:]
        elif qrdata.startswith("OPEN:"):
            invite_link = "https://i.delta.chat/#" + qrdata[5:]
        elif qrdata.startswith("dcqr://"):
            invite_link = "https://i.delta.chat/#" + qrdata[7:]

        # Check if chat is private in our catalog
        catalog_chat = database.get_catalog_chat_by_chat_id(msg.chat_id)
        is_private = catalog_chat and catalog_chat['is_private']
        
        if is_private:
            database.update_catalog_chat_invite_link(msg.chat_id, qrdata)

        chat_name = chat.get('name') if isinstance(chat, dict) else getattr(chat, 'name', 'Group')
        
        if is_private:
            invite_text = (
                f"👥 **Single-use invite link to group: {chat_name}**\n\n"
                f"Scan the QR code or click the link below to join:\n"
                f"{invite_link}\n\n"
                f"⚠️ This invite link is single-use and will be invalidated after one connection."
            )
        else:
            invite_text = (
                f"👥 **Invite to group: {chat_name}**\n\n"
                f"Scan the QR code or click the link below to join:\n"
                f"{invite_link}"
            )
        
        # Try to generate QR code image
        temp_path = None
        try:
            qr = qrcode.QRCode(version=1, box_size=10, border=4)
            qr.add_data(qrdata)
            qr.make(fit=True)
            img = qr.make_image(fill_color="black", back_color="white")
            
            # Create a temporary file
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
                img.save(f, format="PNG")
                temp_path = f.name
            
            # Send file + text
            bot.rpc.send_msg(accid, msg.chat_id, MsgData(file=temp_path, text=invite_text))
            
            # Track success
            try:
                addr = bot.rpc.get_config(accid, "configured_addr") or bot.rpc.get_config(accid, "addr") or "unknown"
                if addr != "unknown":
                    database.increment_transport_sent(addr)
            except Exception:
                pass
        except Exception as qr_err:
            logger.warning(f"Could not generate QR image, falling back to text message: {qr_err}")
            _send(bot, accid, msg.chat_id, invite_text)
        finally:
            if temp_path and os.path.exists(temp_path):
                try:
                    os.unlink(temp_path)
                except Exception as e:
                    logger.warning(f"Failed to delete temp QR image file {temp_path}: {e}")
                    
    except Exception as e:
        logger.error(f"Failed to generate invite: {e}")
        _send(bot, accid, msg.chat_id, f"❌ Failed to generate invite link: {e}")

@dc_cli.on(events.NewMessage(command="/transports"))
def transports_command(bot, accid, event):
    """Show configured transports (mail relays) and their status."""
    msg = event.msg
    if not _is_dc_admin(bot, accid, msg.from_id):
        _send(bot, accid, msg.chat_id, "❌ Only the bot administrator can use /transports.")
        return

    try:
        transports = bot.rpc.list_transports(accid)
    except Exception as e:
        _send(bot, accid, msg.chat_id, f"❌ Failed to list transports: {e}")
        return

    if not transports:
        _send(bot, accid, msg.chat_id, "No transports configured.")
        return

    # Get connectivity status
    connectivity_label = "❓ Unknown"
    try:
        connectivity = bot.rpc.get_connectivity(accid)
        if connectivity >= 4000:
            connectivity_label = "🟢 Connected"
        elif connectivity >= 3000:
            connectivity_label = "🔄 Working"
        elif connectivity >= 2000:
            connectivity_label = "🟡 Connecting"
        else:
            connectivity_label = "🔴 Not connected"
    except Exception:
        pass

    # Get connectivity HTML to parse per-transport status
    connectivity_html = ""
    try:
        connectivity_html = bot.rpc.get_connectivity_html(accid)
    except Exception:
        pass

    # Get resilient sending mode status
    resilient_on = False
    try:
        resilient_on = database.get_config("resilient") == "1"
    except Exception:
        pass

    # Get per-transport statistics
    stats_map = {}
    for s in database.get_all_transport_stats():
        stats_map[s['addr']] = s

    active_addr = bot.rpc.get_config(accid, "configured_addr") or bot.rpc.get_config(accid, "addr")
    transport_addrs = []
    for t in transports:
        addr = t.get('addr', '') if isinstance(t, dict) else getattr(t, 'addr', '')
        transport_addrs.append(addr)

    reply = f"🔌 **Mail Relays (Transports)**\n\nStatus: {connectivity_label}\n\n"

    import re
    for addr in transport_addrs:
        # Determine status label from HTML
        status_label = "❓ Unknown"
        if connectivity_html:
            domain = addr.split('@')[-1] if '@' in addr else addr
            pattern = rf'class="([^"]+)\s+dot".*?<b>{re.escape(domain)}:</b>\s*([^<]+)'
            match = re.search(pattern, connectivity_html, re.IGNORECASE)
            if match:
                color = match.group(1).lower()
                status_text = match.group(2).strip().lower()
                if "yellow" in color or "connecting" in status_text:
                    status_label = "🟡 Connecting"
                elif "green" in color:
                    status_label = "🔄 Working"
                elif "red" in color or "lost" in status_text or "error" in status_text:
                    status_label = "🔴 Not connected"

        is_used = resilient_on or (addr == active_addr)
        used_str = " ✔︎ Used for sending:" if is_used else ":"
        reply += f"**{status_label}**{used_str} `{addr}`\n"

        stats = stats_map.get(addr)
        if stats:
            reply += f"  📤 Sent: {stats['msgs_sent']}  📥 Received: {stats['msgs_received']}\n"
            if stats.get('last_sent_at'):
                import datetime
                last_sent = datetime.datetime.fromtimestamp(stats['last_sent_at']).strftime('%Y-%m-%d %H:%M')
                reply += f"  Last sent: {last_sent}\n"
            if stats.get('last_received_at'):
                import datetime
                last_recv = datetime.datetime.fromtimestamp(stats['last_received_at']).strftime('%Y-%m-%d %H:%M')
                reply += f"  Last received: {last_recv}\n"
        else:
            reply += f"  📤 Sent: 0  📥 Received: 0\n"
        reply += "\n"

    reply += f"Total transports: {len(transport_addrs)}"
    _send(bot, accid, msg.chat_id, reply)

@dc_cli.on(events.NewMessage(command="/addtransport"))
def addtransport_command(bot, accid, event):
    """Add a backup mail relay (transport). Admin only."""
    msg = event.msg
    if not _is_dc_admin(bot, accid, msg.from_id):
        _send(bot, accid, msg.chat_id, "❌ Only the bot administrator can use /addtransport.")
        return

    payload = event.payload.strip() if event.payload else ""
    if not payload:
        _send(bot, accid, msg.chat_id, 
            "Usage:\n"
            "/addtransport DCACCOUNT:server.example\n"
            "/addtransport user@example.com password123"
        )
        return

    try:
        if payload.startswith("DCACCOUNT:"):
            bot.rpc.add_transport_from_qr(accid, payload)
            _send(bot, accid, msg.chat_id, "✅ Backup transport added via chatmail URI.")
        else:
            parts = payload.split(None, 1)
            if len(parts) < 2:
                _send(bot, accid, msg.chat_id, 
                    "❌ For email accounts, provide both address and password:\n"
                    "/addtransport user@example.com password123"
                )
                return
            addr, password = parts[0], parts[1]
            bot.rpc.add_or_update_transport(accid, {"addr": addr, "password": password})
            _send(bot, accid, msg.chat_id, f"✅ Backup transport `{addr}` added.")
    except Exception as e:
        _send(bot, accid, msg.chat_id, f"❌ Failed to add transport: {e}")

@dc_cli.on(events.NewMessage(command="/setprimary"))
def setprimary_command(bot, accid, event):
    """Set a specific transport as primary. Admin only."""
    msg = event.msg
    if not _is_dc_admin(bot, accid, msg.from_id):
        _send(bot, accid, msg.chat_id, "❌ Only the bot administrator can use /setprimary.")
        return

    addr = event.payload.strip() if event.payload else ""
    if not addr:
        _send(bot, accid, msg.chat_id, "Usage: /setprimary user@example.com")
        return

    try:
        bot.rpc.set_config(accid, "configured_addr", addr)
        _send(bot, accid, msg.chat_id, f"✅ Primary address (`configured_addr`) is now `{addr}`.")
    except Exception as e:
        _send(bot, accid, msg.chat_id, f"❌ Failed to set primary address: {e}")

@dc_cli.on(events.NewMessage(command="/resilient"))
def resilient_command(bot, accid, event):
    msg = event.msg
    if not _is_dc_admin(bot, accid, msg.from_id):
        _send(bot, accid, msg.chat_id, "❌ Only the bot administrator can use /resilient.")
        return

    arg = event.payload.strip().lower() if event.payload else ""

    try:
        current = database.get_config("resilient") == "1"
        if not arg:
            status = "enabled" if current else "disabled"
            _send(bot, accid, msg.chat_id, f"ℹ️ Resilient sending mode is currently {status}.")
            return

        if arg in ("on", "1", "true"):
            database.set_config("resilient", "1")
            _send(bot, accid, msg.chat_id, "✅ Resilient sending mode enabled. Each outgoing message will be sent via all connected transports.")
        elif arg in ("off", "0", "false"):
            database.set_config("resilient", "0")
            _send(bot, accid, msg.chat_id, "❌ Resilient sending mode disabled.")
        else:
            _send(bot, accid, msg.chat_id, "❌ Invalid argument. Use '/resilient on', '/resilient off', or '/resilient' to get status.")
    except Exception as e:
        _send(bot, accid, msg.chat_id, f"❌ Failed to update resilient mode: {e}")

@dc_cli.on(events.NewMessage(command="/rmtransport"))
def rmtransport_command(bot, accid, event):
    """Remove a mail relay (transport). Admin only."""
    msg = event.msg
    if not _is_dc_admin(bot, accid, msg.from_id):
        _send(bot, accid, msg.chat_id, "❌ Only the bot administrator can use /rmtransport.")
        return

    addr = event.payload.strip() if event.payload else ""
    if not addr:
        _send(bot, accid, msg.chat_id, "Usage: /rmtransport user@example.com")
        return

    try:
        transports = bot.rpc.list_transports(accid)
        transport_addrs = []
        for t in transports:
            a = t.get('addr', '') if isinstance(t, dict) else getattr(t, 'addr', '')
            transport_addrs.append(a)
        if len(transport_addrs) <= 1:
            _send(bot, accid, msg.chat_id, "❌ Cannot remove the last transport.")
            return
        if addr not in transport_addrs:
            _send(bot, accid, msg.chat_id, f"❌ Transport `{addr}` not found.")
            return
    except Exception as e:
        _send(bot, accid, msg.chat_id, f"❌ Failed to check transports: {e}")
        return

    try:
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
        diff = now - last_check
        if diff < BOUNCE_COOLDOWN_SECONDS:
            remaining_sec = max(1, int(BOUNCE_COOLDOWN_SECONDS - diff))
            if remaining_sec < 60:
                 _send(bot, accid, msg.chat_id, f"⌛️ Relay check was done recently. Please wait {remaining_sec}s before running another check.")
            else:
                 remaining_min = int(remaining_sec / 60)
                 _send(bot, accid, msg.chat_id, f"⌛️ Relay check was done recently. Please wait {remaining_min}m before running another check.")
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

@dc_cli.on(events.NewMessage(command="/chats"))
def chats_command(bot, accid, event):
    msg = event.msg
    catalog_chats = database.get_all_catalog_chats()
    if not catalog_chats:
        _send(bot, accid, msg.chat_id, "ℹ️ The chat catalog is empty.")
        return

    lines = []
    for ch in catalog_chats:
        private_str = "🔐 " if ch['is_private'] else ""
        desc = ch['description'] or ""
        if len(desc) > 100:
            desc = desc[:100] + "..."
        desc_str = f" {desc}" if desc else ""
        lines.append(f"/chat{ch['id']} {private_str}**{ch['name']}**{desc_str} 👥 {ch['member_count']}")

    reply = "\n".join(lines)
    _send(bot, accid, msg.chat_id, reply)

@dc_cli.on(events.NewMessage(command="/chatadd"))
def chatadd_command(bot, accid, event):
    msg = event.msg
    if not _is_dc_admin(bot, accid, msg.from_id):
        _send(bot, accid, msg.chat_id, "❌ Only the bot administrator can use this command.")
        return

    try:
        chat = bot.rpc.get_basic_chat_info(accid, msg.chat_id)
        chat_type = chat.get('chat_type', 'Single') if isinstance(chat, dict) else getattr(chat, 'chat_type', 'Single')
    except Exception as e:
        logger.error(f"Failed to get chat info: {e}")
        return

    GROUP_TYPES = {"Group", "Mailinglist", "OutBroadcast", "InBroadcast"}
    if str(chat_type) not in GROUP_TYPES:
        _send(bot, accid, msg.chat_id, "❌ Only group chats can be added to the catalog.")
        return

    description = event.payload.strip() if event.payload else ""
    if not description:
        try:
            description = bot.rpc.get_chat_description(accid, msg.chat_id) or ""
            description = description.strip()
        except Exception as e:
            logger.warning(f"Failed to get chat description from core: {e}")
            description = ""

    chat_name = chat.get('name') if isinstance(chat, dict) else getattr(chat, 'name', 'Group')

    try:
        contacts = bot.rpc.get_chat_contacts(accid, msg.chat_id)
        member_count = sum(1 for c in contacts if c != 1)
    except Exception:
        member_count = 0

    database.add_catalog_chat(msg.chat_id, chat_name, description, member_count)
    _send(bot, accid, msg.chat_id, f"✅ Chat **{chat_name}** has been added to the catalog.")

@dc_cli.on(events.NewMessage(command="/chatremove"))
def chatremove_command(bot, accid, event):
    msg = event.msg
    if not _is_dc_admin(bot, accid, msg.from_id):
        _send(bot, accid, msg.chat_id, "❌ Only the bot administrator can use this command.")
        return

    database.remove_catalog_chat(msg.chat_id)
    _send(bot, accid, msg.chat_id, "✅ Chat has been removed from the catalog.")

@dc_cli.on(events.NewMessage(command="/private"))
def private_command(bot, accid, event):
    msg = event.msg
    if not _is_dc_admin(bot, accid, msg.from_id):
        _send(bot, accid, msg.chat_id, "❌ Only the bot administrator can use this command.")
        return

    arg = event.payload.strip().lower() if event.payload else ""
    if arg not in ("on", "off"):
        _send(bot, accid, msg.chat_id, "Usage: /private on or /private off")
        return

    catalog_chat = database.get_catalog_chat_by_chat_id(msg.chat_id)
    if not catalog_chat:
        _send(bot, accid, msg.chat_id, "❌ Please add this chat to the catalog first using /chatadd.")
        return

    is_private = 1 if arg == "on" else 0
    database.update_catalog_chat_privacy(msg.chat_id, is_private)

    status_str = "private (by request)" if is_private else "public"
    _send(bot, accid, msg.chat_id, f"✅ Chat is now {status_str}.")

@dc_cli.on(events.NewMessage(command="/welcome"))
def welcome_command(bot, accid, event):
    msg = event.msg
    if not _is_dc_admin(bot, accid, msg.from_id):
        _send(bot, accid, msg.chat_id, "❌ Only the bot administrator can use this command.")
        return

    catalog_chat = database.get_catalog_chat_by_chat_id(msg.chat_id)
    if not catalog_chat:
        _send(bot, accid, msg.chat_id, "❌ Please add this chat to the catalog first using /chatadd.")
        return

    payload = event.payload.strip() if event.payload else ""
    if not payload:
        status = "enabled" if catalog_chat.get('welcome_enabled') else "disabled"
        add_text = catalog_chat.get('welcome_text') or ""
        text_suffix = f"\nAdditional text: \"{add_text}\"" if add_text else ""
        _send(bot, accid, msg.chat_id, f"ℹ️ Welcome greeting for new members is {status}.{text_suffix}")
        return

    payload_lower = payload.lower()
    if payload_lower == "off":
        database.update_catalog_chat_welcome(msg.chat_id, 0, None)
        _send(bot, accid, msg.chat_id, "✅ Welcome greeting for new members has been disabled.")
    elif payload_lower == "on" or payload_lower.startswith("on "):
        welcome_text = None
        if payload_lower.startswith("on "):
            welcome_text = payload[3:].strip()
            
        database.update_catalog_chat_welcome(msg.chat_id, 1, welcome_text)
        
        preview_text = f" {welcome_text}" if welcome_text else ""
        _send(bot, accid, msg.chat_id, 
              f"✅ Welcome greeting for new members has been enabled!\n"
              f"Example: 👋🏻 **Name** (💬 X), welcome to {catalog_chat['name']} group!{preview_text}")
    else:
        _send(bot, accid, msg.chat_id, 
              "Usage:\n"
              "/welcome — show current status\n"
              "/welcome on — enable standard greeting\n"
              "/welcome on <additional text> — enable greeting with additional text\n"
              "/welcome off — disable greeting")

@dc_cli.on(events.NewMessage(is_info=True, is_bot=None, is_outgoing=None))
def handle_dc_info_message(bot, accid, event):
    msg = event.msg
    dc_chat_id = msg.chat_id
    
    smt = msg.system_message_type
    smt_str = str(smt).lower() if smt is not None else ""
    msg_text = (msg.text or "").lower()
    
    logger.info(f"handle_dc_info_message: dc_chat_id={dc_chat_id}, smt={smt!r}, smt_str={smt_str!r}, msg_text={msg_text!r}")
    
    is_member_event = False
    is_join_event = False
    try:
        if smt == SystemMessageType.MEMBER_ADDED_TO_GROUP:
            is_member_event = True
            is_join_event = True
        elif smt == SystemMessageType.MEMBER_REMOVED_FROM_GROUP:
            is_member_event = True
    except Exception:
        pass
        
    if not is_member_event:
        is_member_event = any(kw in smt_str or kw in msg_text for kw in ("memberadded", "memberremoved", "member_added", "member_removed", "left", "added", "removed"))
        is_join_event = any(kw in smt_str or kw in msg_text for kw in ("memberadded", "member_added", "joined", "added"))
        
    if is_member_event:
        catalog_chat = database.get_catalog_chat_by_chat_id(dc_chat_id)
        if catalog_chat:
            try:
                contacts = bot.rpc.get_chat_contacts(accid, dc_chat_id)
                member_count = sum(1 for c in contacts if c != 1)
                database.update_catalog_chat_member_count(dc_chat_id, member_count)
                logger.info(f"Updated member count for catalog chat {dc_chat_id} to {member_count}")
                
                # Check if it was a join event and welcome is enabled
                if is_join_event and catalog_chat.get('welcome_enabled'):
                    new_member_id = getattr(msg, 'info_contact_id', None)
                    if not new_member_id and msg.text:
                        try:
                            from deltachat2._utils import parse_system_add_remove
                            parsed = parse_system_add_remove(msg.text)
                            if parsed and parsed[0] == "added":
                                affected_email = parsed[1]
                                if affected_email:
                                    new_member_id = bot.rpc.lookup_contact_id_by_address(accid, affected_email)
                        except Exception as parse_err:
                            logger.error(f"Failed to parse join event text: {parse_err}")
                            
                    if new_member_id and new_member_id != 1:
                        contact = bot.rpc.get_contact(accid, new_member_id)
                        member_name = contact.name or contact.display_name or contact.address or "New member"
                        user_chat_count = get_user_chat_count(bot, accid, new_member_id)
                        
                        chat_name = catalog_chat['name']
                        welcome_suffix = f" {catalog_chat['welcome_text']}" if catalog_chat.get('welcome_text') else ""
                        
                        welcome_msg = f"👋🏻 **{member_name}** (💬 {user_chat_count}), welcome to {chat_name} group!{welcome_suffix}"
                        _send(bot, accid, dc_chat_id, welcome_msg)
                
                # Check if chat is private and has an active invite link to revoke
                if is_join_event and catalog_chat.get('is_private') and catalog_chat.get('invite_link'):
                    invite_link = catalog_chat['invite_link']
                    try:
                        qr_info = bot.rpc.check_qr(accid, invite_link)
                        if qr_info.get("kind") == "withdrawVerifyGroup":
                            bot.rpc.set_config_from_qr(accid, invite_link)
                            logger.info(f"Withdrew/invalidated invite link for private chat {dc_chat_id}")
                    except Exception as qr_err:
                        logger.error(f"Failed to withdraw invite link: {qr_err}")
                    database.update_catalog_chat_invite_link(dc_chat_id, None)
            except Exception as e:
                logger.error(f"Failed to update catalog chat member count: {e}")

def get_user_chat_count(bot, accid, contact_id):
    monitored_chats = database.get_all_monitored_chats()
    count = 0
    for chat_id in monitored_chats:
        try:
            contacts = bot.rpc.get_chat_contacts(accid, chat_id)
            if contact_id in contacts:
                count += 1
        except Exception:
            continue
    return count

@dc_cli.on(events.NewMessage)
def handle_all_messages(bot, accid, event):
    """Handle dynamic commands like /contact123"""
    msg = event.msg
    
    # Track receiving stats
    try:
        addr = bot.rpc.get_config(accid, "configured_addr") or bot.rpc.get_config(accid, "addr")
        if addr:
            database.increment_transport_received(addr)
    except Exception:
        pass
        
    text = (msg.text or "").strip()
    
    # 1. Handle /chat<ID> command (but NOT /chats)
    if text.startswith("/chat") and not (text == "/chats" or text.startswith("/chats ")):
        m = re.match(r'^/chat(\d+)(?:\s+(.*))?$', text, re.IGNORECASE)
        if m:
            catalog_id = int(m.group(1))
            message_payload = m.group(2).strip() if m.group(2) else ""
            
            # Lookup catalog chat
            catalog_chat = database.get_catalog_chat_by_id(catalog_id)
            if not catalog_chat:
                _send(bot, accid, msg.chat_id, "❌ Chat with this number was not found in the catalog.")
                return

            chat_id = catalog_chat['chat_id']
            
            # Check if user is already in the chat
            try:
                contacts = bot.rpc.get_chat_contacts(accid, chat_id)
                if msg.from_id in contacts:
                    _send(bot, accid, msg.chat_id, "ℹ️ You are already a member of this chat.")
                    return
            except Exception as e:
                logger.error(f"Failed to check contacts for join request: {e}")

            if not catalog_chat['is_private']:
                # Public chat: immediately generate invite link and send it
                try:
                    qrdata = bot.rpc.get_chat_securejoin_qr_code(accid, chat_id)
                    invite_link = qrdata
                    if qrdata.startswith("OPEN-CHAT:"):
                        invite_link = "https://i.delta.chat/#" + qrdata[10:]
                    elif qrdata.startswith("OPEN:"):
                        invite_link = "https://i.delta.chat/#" + qrdata[5:]
                    elif qrdata.startswith("dcqr://"):
                        invite_link = "https://i.delta.chat/#" + qrdata[7:]
                        
                    _send(bot, accid, msg.chat_id, f"🔗 Invite link to chat **{catalog_chat['name']}**:\n{invite_link}")
                except Exception as e:
                    logger.error(f"Failed to generate invite: {e}")
                    _send(bot, accid, msg.chat_id, "❌ Failed to generate invite link.")
            else:
                # Private chat: request confirmation from participants in the chat
                try:
                    contact = bot.rpc.get_contact(accid, msg.from_id)
                    requester_name = contact.name or contact.display_name or contact.address or "Unknown"
                    user_chat_count = get_user_chat_count(bot, accid, msg.from_id)
                    
                    request_id = database.add_pending_request(
                        catalog_id, chat_id, msg.from_id, requester_name, message_payload
                    )
                    
                    if message_payload:
                        req_msg = (
                            f"**{requester_name}** (💬 {user_chat_count}) wants to join this chat with following message: {message_payload}\n"
                            f"To approve: reply with /approve{request_id}\n"
                            f"To decline: reply with /decline{request_id} (optional comment after command)"
                        )
                    else:
                        req_msg = (
                            f"**{requester_name}** (💬 {user_chat_count}) wants to join this chat.\n"
                            f"To approve: reply with /approve{request_id}\n"
                            f"To decline: reply with /decline{request_id} (optional comment after command)"
                        )
                    
                    _send(bot, accid, chat_id, req_msg)
                    _send(bot, accid, msg.chat_id, "⏳ Your request to join has been sent to group members. Please wait for approval.")
                except Exception as e:
                    logger.error(f"Failed to create join request: {e}")
                    _send(bot, accid, msg.chat_id, "❌ An error occurred while sending the request.")
            return

    # 2. Handle /approve<ID> command
    elif text.startswith("/approve"):
        m = re.match(r'^/approve(\d+)$', text, re.IGNORECASE)
        if m:
            request_id = int(m.group(1))
            req = database.get_pending_request(request_id)
            if not req:
                _send(bot, accid, msg.chat_id, "❌ Request not found.")
                return
            if req['approved'] == 1:
                _send(bot, accid, msg.chat_id, "ℹ️ This request has already been approved.")
                return
            elif req['approved'] == 2:
                _send(bot, accid, msg.chat_id, "ℹ️ This request has already been declined.")
                return
            if req['chat_id'] != msg.chat_id:
                _send(bot, accid, msg.chat_id, "❌ You can only approve this request in the corresponding group chat.")
                return
                
            try:
                # Generate single-use invite link
                qrdata = bot.rpc.get_chat_securejoin_qr_code(accid, req['chat_id'])
                invite_link = qrdata
                if qrdata.startswith("OPEN-CHAT:"):
                    invite_link = "https://i.delta.chat/#" + qrdata[10:]
                elif qrdata.startswith("OPEN:"):
                    invite_link = "https://i.delta.chat/#" + qrdata[5:]
                elif qrdata.startswith("dcqr://"):
                    invite_link = "https://i.delta.chat/#" + qrdata[7:]
                
                # Cache the invite link in catalog_chats as the active single-use link
                database.update_catalog_chat_invite_link(req['chat_id'], qrdata)
                
                # Mark request as approved
                database.approve_pending_request(request_id)
                
                # Get requester's private chat_id
                user_chat_id = bot.rpc.create_chat_by_contact_id(accid, req['requester_contact_id'])
                
                catalog_chat = database.get_catalog_chat_by_chat_id(req['chat_id'])
                chat_name = catalog_chat['name'] if catalog_chat else "Group"
                
                _send(bot, accid, user_chat_id, 
                      f"✅ Your request to join chat **{chat_name}** has been approved!\n\n"
                      f"Single-use invite link:\n{invite_link}\n\n"
                      f"⚠️ This invite link is single-use and will be invalidated after one connection.")
                      
                _send(bot, accid, msg.chat_id, f"✅ Request approved. A single-use invite link has been sent to **{req['requester_name']}**.")
            except Exception as e:
                logger.error(f"Failed to approve request: {e}")
                _send(bot, accid, msg.chat_id, f"❌ Error while approving request: {e}")
            return

    # 3. Handle /decline<ID> [comment] command
    elif text.startswith("/decline"):
        m = re.match(r'^/decline(\d+)(?:\s+(.*))?$', text, re.IGNORECASE)
        if m:
            request_id = int(m.group(1))
            reason = m.group(2).strip() if m.group(2) else ""
            
            req = database.get_pending_request(request_id)
            if not req:
                _send(bot, accid, msg.chat_id, "❌ Request not found.")
                return
            if req['approved'] == 1:
                _send(bot, accid, msg.chat_id, "ℹ️ This request has already been approved.")
                return
            elif req['approved'] == 2:
                _send(bot, accid, msg.chat_id, "ℹ️ This request has already been declined.")
                return
            if req['chat_id'] != msg.chat_id:
                _send(bot, accid, msg.chat_id, "❌ You can only decline this request in the corresponding group chat.")
                return
                
            try:
                # Mark request as declined
                database.decline_pending_request(request_id)
                
                # Get requester's private chat_id
                user_chat_id = bot.rpc.create_chat_by_contact_id(accid, req['requester_contact_id'])
                
                catalog_chat = database.get_catalog_chat_by_chat_id(req['chat_id'])
                chat_name = catalog_chat['name'] if catalog_chat else "Group"
                
                if reason:
                    decline_msg = f"❌ Your request to join chat **{chat_name}** has been declined.\nComment: {reason}"
                else:
                    decline_msg = f"❌ Your request to join chat **{chat_name}** has been declined."
                
                _send(bot, accid, user_chat_id, decline_msg)
                _send(bot, accid, msg.chat_id, f"❌ Request declined. The user has been notified.")
            except Exception as e:
                logger.error(f"Failed to decline request: {e}")
                _send(bot, accid, msg.chat_id, f"❌ Error while declining request: {e}")
            return

    # 3. Original contact sharing logic
    elif text.startswith("/contact"):
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
                    
                    if contact_id in chat_contact_ids or _is_dc_admin(bot, accid, msg.from_id):
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
                            try:
                                addr = bot.rpc.get_config(accid, "configured_addr") or bot.rpc.get_config(accid, "addr") or "unknown"
                                if addr != "unknown":
                                    database.increment_transport_sent(addr)
                            except Exception:
                                pass
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
