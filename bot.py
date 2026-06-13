import asyncio
import concurrent.futures
import io
import logging
import os
import re
import signal
import threading
import time
import shutil
import subprocess
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
_chat_cmping_anti_spam: dict[int, float] = {}
_domain_locks: dict[str, threading.Lock] = {}
_domain_locks_lock = threading.Lock()
_cmping_global_lock = threading.Lock()

# CMPing monitoring state
_cmping_monitor_index = 0
_cmping_monitor_running = False
_cmping_last_results: dict[tuple, dict] = {}  # {(src, dst, "fwd"/"bwd"): {"success": bool, "avg": float, "error": str}}
CMPING_MONITOR_INTERVAL = int(os.environ.get("CMPING_MONITOR_INTERVAL", "1800"))  # default 30 min

def _get_domain_lock(domain: str) -> threading.Lock:
    with _domain_locks_lock:
        if domain not in _domain_locks:
            _domain_locks[domain] = threading.Lock()
        return _domain_locks[domain]

BOUNCE_COOLDOWN_SECONDS = 60   # 1 minute for general commands (/bounce, /top, /relays)
SEARCH_COOLDOWN_SECONDS = 10   # 10 seconds for /search command
CMPING_COOLDOWN_SECONDS = 15   # 15 seconds for /cmping command

REGULAR_MAIL_DOMAINS = {
    "yandex.ru", "yandex.com", "ya.ru",
    "mail.ru", "list.ru", "bk.ru", "inbox.ru", "internet.ru",
    "rambler.ru"
}

# Age indicator: each circle = 1 week of bot knowing the user
_AGE_CIRCLES = ["🔴", "🟠", "🟡", "🟢", "🔵", "🟣", "🟤", "⚫", "⚪"]

def _get_contact_age_indicator(contact_id: int) -> str:
    """Return a colored circle emoji based on how long the bot has known this contact.
    🔴 = <1 week, 🟠 = 1-2 weeks, ... ⚪ = 8+ weeks."""
    now = time.time()
    first_seen = database.get_contact_first_seen(contact_id)
    if first_seen is None:
        database.ensure_contact_first_seen(contact_id, now)
        first_seen = now
    weeks = int((now - first_seen) / (7 * 24 * 3600))
    idx = min(weeks, len(_AGE_CIRCLES) - 1)
    return _AGE_CIRCLES[idx]

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

def _react(bot, accid, msg_id, reaction):
    """Add a reaction to a message."""
    try:
        bot.rpc.send_reaction(accid, msg_id, [reaction] if reaction else [])
    except Exception as e:
        logger.warning(f"Failed to send reaction {reaction}: {e}")

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
            
            # Refresh member counts for all catalog chats and channels
            _refresh_catalog_member_counts(bot, accid)
            
        except Exception as e:
            logger.error(f"Background loop error: {e}")
            
        time.sleep(3600) # Check every hour


def _refresh_catalog_member_counts(bot, accid):
    """Refresh member counts for all catalog chats and channels."""
    updated = 0
    
    # Catalog chats
    try:
        catalog_chats = database.get_all_catalog_chats()
        for cat_chat in catalog_chats:
            try:
                chat_id = cat_chat['chat_id']
                contacts = bot.rpc.get_chat_contacts(accid, chat_id)
                now = time.time()
                member_count = sum(1 for c in contacts if c != 1)
                # Seed first_seen for all contacts
                for c in contacts:
                    if c != 1:
                        database.ensure_contact_first_seen(c, now)
                old_count = cat_chat.get('member_count', 0)
                if member_count != old_count:
                    database.update_catalog_chat_member_count(chat_id, member_count)
                    logger.info(f"Refreshed catalog chat {cat_chat['name']!r} member count: {old_count} -> {member_count}")
                    updated += 1
            except Exception as e:
                logger.error(f"Failed to refresh member count for catalog chat {cat_chat.get('name', '?')}: {e}")
    except Exception as e:
        logger.error(f"Failed to load catalog chats for member refresh: {e}")
    
    # Catalog channels
    try:
        catalog_channels = database.get_all_catalog_channels()
        for cat_chan in catalog_channels:
            try:
                chat_id = cat_chan['chat_id']
                contacts = bot.rpc.get_chat_contacts(accid, chat_id)
                now = time.time()
                member_count = sum(1 for c in contacts if c != 1)
                # Seed first_seen for all contacts
                for c in contacts:
                    if c != 1:
                        database.ensure_contact_first_seen(c, now)
                old_count = cat_chan.get('member_count', 0)
                if member_count != old_count:
                    database.update_catalog_channel_member_count(chat_id, member_count)
                    logger.info(f"Refreshed catalog channel {cat_chan['name']!r} member count: {old_count} -> {member_count}")
                    updated += 1
            except Exception as e:
                logger.error(f"Failed to refresh member count for catalog channel {cat_chan.get('name', '?')}: {e}")
    except Exception as e:
        logger.error(f"Failed to load catalog channels for member refresh: {e}")
    
    logger.info(f"Catalog member count refresh complete: {updated} updated")


def _get_bot_domains(bot, accid) -> list[str]:
    """Get the bot's transport domains. Returns a list of domain strings."""
    bot_domains = []
    try:
        transports = bot.rpc.list_transports(accid)
    except Exception:
        transports = []

    for t in transports:
        addr = t.get('addr', '') if isinstance(t, dict) else getattr(t, 'addr', '')
        if addr:
            domain = addr.split('@')[-1] if '@' in addr else addr
            domain = domain.strip().lower()
            if domain and domain not in bot_domains:
                bot_domains.append(domain)

    # Fallback to configured active address domain
    if not bot_domains:
        try:
            addr = bot.rpc.get_config(accid, "configured_addr") or bot.rpc.get_config(accid, "addr")
            if addr:
                domain = addr.split('@')[-1] if '@' in addr else addr
                domain = domain.strip().lower()
                if domain and domain not in bot_domains:
                    bot_domains.append(domain)
        except Exception:
            pass

    return bot_domains


def _cmping_monitor_loop(bot, accid):
    """Background loop: periodically checks server connectivity via cmping."""
    global _cmping_monitor_index, _cmping_monitor_running, _cmping_last_results

    if CMPING_MONITOR_INTERVAL <= 0:
        logger.info("CMPing monitor disabled (CMPING_MONITOR_INTERVAL=0).")
        return

    logger.info(f"CMPing monitor loop started (interval={CMPING_MONITOR_INTERVAL}s).")
    time.sleep(60)  # Initial delay — let bot fully start up

    cmping_path = shutil.which("cmping") or "cmping"

    while True:
        try:
            _cmping_monitor_cycle(bot, accid, cmping_path)
        except Exception as e:
            logger.error(f"CMPing monitor cycle error: {e}")
        finally:
            _cmping_monitor_running = False

        time.sleep(CMPING_MONITOR_INTERVAL)


def _cmping_monitor_cycle(bot, accid, cmping_path):
    """Execute one monitoring cycle: pick source, check all pairs, report changes."""
    global _cmping_monitor_index, _cmping_monitor_running, _cmping_last_results

    if _cmping_monitor_running:
        logger.warning("CMPing monitor: previous cycle still running, skipping.")
        return
    _cmping_monitor_running = True

    # Build server list: bot transports + manually monitored
    bot_domains = _get_bot_domains(bot, accid)
    monitor_domains = database.get_all_cmping_monitors()
    all_servers = list(bot_domains)
    for d in monitor_domains:
        if d not in all_servers:
            all_servers.append(d)

    if len(all_servers) < 2:
        logger.debug("CMPing monitor: fewer than 2 servers, skipping cycle.")
        return

    # Pick source via round-robin
    source_idx = _cmping_monitor_index % len(all_servers)
    source = all_servers[source_idx]
    _cmping_monitor_index = (_cmping_monitor_index + 1) % len(all_servers)
    targets = [s for s in all_servers if s != source]

    logger.info(f"CMPing monitor cycle: source={source}, targets={targets}")

    # Check each pair
    state_changes = []  # list of (src, dst, direction, old_result, new_result)

    for dst in targets:
        # Forward: source -> dst
        fwd_result = _run_monitor_single(cmping_path, source, dst)
        # Backward: dst -> source
        bwd_result = _run_monitor_single(cmping_path, dst, source)

        # Check for state changes
        for direction, result, src_d, dst_d in [
            ("fwd", fwd_result, source, dst),
            ("bwd", bwd_result, dst, source),
        ]:
            key = (src_d, dst_d)
            old = _cmping_last_results.get(key)
            old_success = old.get("success") if old else None

            if old_success != result.get("success"):
                state_changes.append((src_d, dst_d, old, result))

            _cmping_last_results[key] = result

    # Send alerts for state changes
    if state_changes:
        _send_monitor_alerts(bot, accid, state_changes, all_servers, source)


def _run_monitor_single(cmping_path, src, dst):
    """Run a single cmping check (src -> dst) with retry on failure.
    Uses the global lock to avoid conflicts with user /cmping commands.
    Returns dict with 'success', 'avg' (if success), 'error' (if failed), 'checked_at'."""
    import subprocess

    def do_check():
        cmd = [cmping_path, "-c", "1", src, dst]
        try:
            _cmping_global_lock.acquire()
            try:
                stdout, stderr, rc = _run_cmping_subprocess(cmd, timeout=60)
            finally:
                _cmping_global_lock.release()
            return _parse_single_cmping(stdout, stderr, rc)
        except subprocess.TimeoutExpired:
            return {"success": False, "error": "Timeout expired (60s)"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    result = do_check()

    # If failed, retry once to filter transient issues
    if not result.get("success"):
        logger.info(f"CMPing monitor: {src} -> {dst} failed ({result.get('error')}), retrying...")
        time.sleep(2)
        result = do_check()

    result["checked_at"] = time.time()
    return result


def _send_monitor_alerts(bot, accid, state_changes, all_servers, source):
    """Send monitoring alerts to subscribed chats for state changes."""
    from datetime import datetime, timezone

    report_chats = database.get_all_cmping_report_chats()
    if not report_chats:
        return

    # Separate failures and recoveries
    failures = [(s, d, old, new) for s, d, old, new in state_changes if not new.get("success")]
    recoveries = [(s, d, old, new) for s, d, old, new in state_changes if new.get("success") and old is not None]

    if not failures and not recoveries:
        return

    # Build emoji map for involved servers
    def get_index_emoji(idx: int) -> str:
        emojis = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
        if 1 <= idx <= 10:
            return emojis[idx - 1]
        return f"[{idx}]"

    domain_to_emoji = {}
    for idx, d in enumerate(all_servers, 1):
        domain_to_emoji[d] = get_index_emoji(idx)

    gmt_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M GMT")
    bot_name = os.environ.get("DISPLAY_NAME", "Bouncer Bot")
    messages = []

    # Failure alert
    if failures:
        # Collect involved domains
        involved = set()
        for s, d, _, _ in failures:
            involved.add(s)
            involved.add(d)

        legend_lines = [f"{domain_to_emoji.get(d, d)} {d}" for d in all_servers if d in involved]
        result_lines = []
        for s, d, old, new in failures:
            e1 = domain_to_emoji.get(s, s)
            e2 = domain_to_emoji.get(d, d)
            err = new.get("error", "Unknown error")
            result_lines.append(f"{e1}→{e2} ❌ {err}")

        parts = [
            "🔴 **CMPing Monitor Alert:**",
            "\n".join(legend_lines),
            "\n".join(result_lines),
            f"Source: {source} | Generated {gmt_time} by {bot_name}",
        ]
        messages.append("\n\n".join(parts))

    # Recovery alert
    if recoveries:
        involved = set()
        for s, d, _, _ in recoveries:
            involved.add(s)
            involved.add(d)

        legend_lines = [f"{domain_to_emoji.get(d, d)} {d}" for d in all_servers if d in involved]
        result_lines = []
        for s, d, old, new in recoveries:
            e1 = domain_to_emoji.get(s, s)
            e2 = domain_to_emoji.get(d, d)
            avg_str = f"{new['avg']:.1f} ms" if new.get("avg") else "OK"
            old_err = old.get("error", "error") if old else "error"
            result_lines.append(f"{e1}→{e2} ✅ {avg_str} (was: {old_err})")

        parts = [
            "✅ **CMPing Recovery:**",
            "\n".join(legend_lines),
            "\n".join(result_lines),
            f"Source: {source} | Generated {gmt_time} by {bot_name}",
        ]
        messages.append("\n\n".join(parts))

    # Send to all subscribed chats
    for msg_text in messages:
        for chat_id in report_chats:
            try:
                _send(bot, accid, chat_id, msg_text)
            except Exception as e:
                logger.error(f"CMPing monitor: failed to send alert to chat {chat_id}: {e}")


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
        bot_name = os.environ.get("DISPLAY_NAME", "Bouncer Bot")
        bot.rpc.set_config(dc_accid, "displayname", bot_name)
        status_text = os.environ.get(
            "STATUS_TEXT",
            "I monitor group activity, manage chat/channel catalogs, welcome new members, and search contacts. Send /help for commands."
        )
        bot.rpc.set_config(dc_accid, "selfstatus", status_text)
        
        # Set icon if configured or fallback to default icon
        try:
            base_dir = os.path.dirname(os.path.abspath(__file__))
            avatar_env = os.environ.get("AVATAR_PATH")
            avatar_paths = []
            if avatar_env:
                if os.path.isabs(avatar_env):
                    avatar_paths.append(avatar_env)
                else:
                    avatar_paths.append(os.path.join(base_dir, avatar_env))
                    avatar_paths.append(os.path.abspath(avatar_env))
            
            # Default paths fallback
            avatar_paths.extend([
                os.path.join(base_dir, "icon.png"),
                os.path.join(base_dir, "data", "icon.png")
            ])
            
            for path in avatar_paths:
                if os.path.exists(path):
                    bot.rpc.set_config(dc_accid, "selfavatar", path)
                    bot.logger.info(f"Successfully configured bot avatar from {path}")
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

    t2 = threading.Thread(target=_cmping_monitor_loop, args=(bot, accid), daemon=True)
    t2.start()


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
                    
                    # Track first-seen and get age indicator
                    database.ensure_contact_first_seen(contact_id, now)
                    age = _get_contact_age_indicator(contact_id)

                    found_users_map[contact_id] = {
                        "name": name,
                        "addrs_str": addrs_str,
                        "status": status,
                        "age": age,
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
                reply += f"• /contact{contact_id} {info['age']} **{info['name']}** ({info['addrs_str']}) [{info['status']}]\n  ↳ Groups: {groups_str}\n"
        else:
            reply = f"🔍 **Search Results for {queries_str} ({len(found_users_map)}):**\n\n"
            for contact_id, info in found_users_map.items():
                reply += f"• /contact{contact_id} {info['age']} **{info['name']}** ({info['addrs_str']}) [{info['status']}]\n"
        
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
    
    try:
        status_text = bot.rpc.get_config(accid, "selfstatus")
    except Exception:
        status_text = os.environ.get("STATUS_TEXT")
    if not status_text:
        status_text = f"I monitor groups and report inactive users (no activity for {INACTIVITY_DAYS_THRESHOLD} days)."

    help_text = (
        f"👋 Hi {sender_email}!\n\n"
        f"{status_text}\n\n"
        f"**Commands:**\n"
        f"/bounce — Trigger an inactivity check in the current group.\n"
        f"/search [query1] ... — Search members by email/domain (e.g. @testrun.org) or reply to a message.\n"
        f"/relays — Find group members using regular mail providers.\n"
        f"/top    — Show top 10 posters in the last 24 hours.\n"
        f"/invite — Generate an invite link/QR code for this group.\n"
        f"/contact<ID> — Get a contact object for the given ID.\n"
        f"/help   — This message.\n"
        f"/chats  — Show catalog of available chats.\n"
        f"/dchannels — Show catalog of available channels.\n"
        f"/cmping <server1> ... — Ping relays to/from specified servers.\n\n"
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
        help_text += "/chatdesc<ID> <text> — Update chat description\n"
        help_text += "/private <on/off> — Toggle privacy of current chat\n"
        help_text += "/welcome [on/off/...] — Manage welcome message for new members\n"
        help_text += "/dchanneladd <URL> — Join and add a channel to the catalog\n"
        help_text += "/dchannelremove [ID] — Remove channel from catalog\n"
        help_text += "/dchanneldesc<ID> <text> — Update channel description\n"
        help_text += "/cmpingadd <server> — Add server to connectivity monitoring\n"
        help_text += "/cmpingdel <server> — Remove server from monitoring\n"
        help_text += "/cmpinglist — Show monitored servers\n"
        help_text += "/cmpingstatus — Show monitoring results matrix\n"
        help_text += "/cmfaillist — Show currently failed links only\n"
        help_text += "/cmreport <on/off> — Toggle monitoring alerts in this chat"

        
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

@dc_cli.on(events.NewMessage(command="/dchannels"))
def dchannels_command(bot, accid, event):
    msg = event.msg
    catalog_channels = database.get_all_catalog_channels()
    if not catalog_channels:
        _send(bot, accid, msg.chat_id, "ℹ️ The channel catalog is empty.")
        return

    lines = []
    for ch in catalog_channels:
        desc = ch['description'] or ""
        if len(desc) > 100:
            desc = desc[:100] + "..."
        desc_str = f" {desc}" if desc else ""
        lines.append(f"/dchannel{ch['id']} **{ch['name']}**{desc_str}")

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

def bg_channel_join_worker(bot, accid, admin_chat_id, url, chat_name, qr_info=None):
    try:
        old_chats = set(bot.rpc.get_chatlist_entries(accid, None, None, None))
        bot.rpc.secure_join(accid, url)
        
        joined_chat_id = None
        for _ in range(30):
            time.sleep(1)
            new_chats = set(bot.rpc.get_chatlist_entries(accid, None, None, None))
            added_chats = new_chats - old_chats
            if added_chats:
                joined_chat_id = list(added_chats)[0]
                break
        
        if not joined_chat_id and qr_info:
            contact_id = qr_info.get("contact_id")
            if contact_id:
                try:
                    joined_chat_id = bot.rpc.create_chat_by_contact_id(accid, int(contact_id))
                except Exception:
                    pass
            
            if not joined_chat_id:
                try:
                    grpid = qr_info.get("grpid")
                    chats = bot.rpc.get_chatlist_entries(accid, None, None, None)
                    for cid in chats:
                        if not isinstance(cid, int):
                            continue
                        try:
                            c_info = bot.rpc.get_basic_chat_info(accid, cid)
                            c_grpid = c_info.get("grpid") if isinstance(c_info, dict) else getattr(c_info, "grpid", None)
                            if grpid and c_grpid == grpid:
                                joined_chat_id = cid
                                break
                            
                            c_name = c_info.get("name") if isinstance(c_info, dict) else getattr(c_info, "name", None)
                            if c_name == chat_name:
                                joined_chat_id = cid
                                break
                        except Exception:
                            continue
                except Exception:
                    pass

        if joined_chat_id:
            chat = bot.rpc.get_basic_chat_info(accid, joined_chat_id)
            c_name = chat.get('name') if isinstance(chat, dict) else getattr(chat, 'name', chat_name or 'Channel')
            chat_type = chat.get('chat_type') if isinstance(chat, dict) else getattr(chat, 'chat_type', 'Single')
            
            description = ""
            if str(chat_type) == "Single":
                try:
                    contacts = bot.rpc.get_chat_contacts(accid, joined_chat_id)
                    other_contacts = [c for c in contacts if c != 1]
                    if other_contacts:
                        contact = bot.rpc.get_contact(accid, other_contacts[0])
                        description = contact.get('status') if isinstance(contact, dict) else getattr(contact, 'status', '')
                        description = description.strip() if description else ""
                except Exception as e:
                    logger.warning(f"Failed to get contact status: {e}")
            else:
                try:
                    description = bot.rpc.get_chat_description(accid, joined_chat_id) or ""
                    description = description.strip()
                except Exception:
                    pass
                
            database.add_catalog_channel(joined_chat_id, c_name, description, 0, url)
            _send(bot, accid, admin_chat_id, f"✅ Channel **{c_name}** has been joined and added to the catalog.")
        else:
            _send(bot, accid, admin_chat_id, f"❌ Failed to join channel **{chat_name or 'Channel'}**. The securejoin handshake timed out. Please check that the link is correct and try again.")
            
    except Exception as e:
        logger.error(f"Error in bg_channel_join_worker: {e}")
        _send(bot, accid, admin_chat_id, f"❌ An error occurred while adding the channel: {e}")

@dc_cli.on(events.NewMessage(command="/dchanneladd"))
def dchanneladd_command(bot, accid, event):
    msg = event.msg
    if not _is_dc_admin(bot, accid, msg.from_id):
        _send(bot, accid, msg.chat_id, "❌ Only the bot administrator can use this command.")
        return

    url = event.payload.strip() if event.payload else ""
    if not url:
        _send(bot, accid, msg.chat_id, "Usage: /dchanneladd <invite_link_url>")
        return

    existing_channels = database.get_all_catalog_channels()
    for ch in existing_channels:
        if ch.get('invite_link') == url:
            _send(bot, accid, msg.chat_id, f"ℹ️ This channel is already in the catalog (ID: {ch['id']}).")
            return

    try:
        qr_info = bot.rpc.check_qr(accid, url)
        kind = qr_info.get("kind", "")
        kind_lower = kind.lower()
        allowed_kinds = (
            "askverifygroup", "askjoinbroadcast", "withdrawverifygroup", "withdrawjoinbroadcast", "reviveverifygroup", "revivejoinbroadcast",
            "askverifycontact", "withdrawverifycontact", "reviveverifycontact"
        )
        if kind_lower not in allowed_kinds:
            _send(bot, accid, msg.chat_id, "❌ Invalid invite link. Please provide a valid Delta Chat group/channel join link.")
            return
            
        if "contact" in kind_lower or "broadcast" in kind_lower:
            chat_name = qr_info.get("name")
        else:
            chat_name = qr_info.get("grpname")
    except Exception as e:
        _send(bot, accid, msg.chat_id, f"❌ Failed to parse invite link: {e}")
        return

    _send(bot, accid, msg.chat_id, f"⏳ Attempting to join channel **{chat_name or 'Channel'}** in the background... This may take up to 30 seconds.")
    threading.Thread(target=bg_channel_join_worker, args=(bot, accid, msg.chat_id, url, chat_name, qr_info), daemon=True).start()

@dc_cli.on(events.NewMessage(command="/dchannelremove"))
def dchannelremove_command(bot, accid, event):
    msg = event.msg
    if not _is_dc_admin(bot, accid, msg.from_id):
        _send(bot, accid, msg.chat_id, "❌ Only the bot administrator can use this command.")
        return

    payload = event.payload.strip() if event.payload else ""
    if payload:
        try:
            catalog_id = int(payload)
            channel = database.get_catalog_channel_by_id(catalog_id)
            if channel:
                database.remove_catalog_channel(channel['chat_id'])
                _send(bot, accid, msg.chat_id, f"✅ Channel **{channel['name']}** has been removed from the catalog.")
                return
            else:
                _send(bot, accid, msg.chat_id, "❌ Channel not found in the catalog.")
                return
        except ValueError:
            pass

    channel = database.get_catalog_channel_by_chat_id(msg.chat_id)
    if channel:
        database.remove_catalog_channel(msg.chat_id)
        _send(bot, accid, msg.chat_id, f"✅ Channel **{channel['name']}** has been removed from the catalog.")
    else:
        _send(bot, accid, msg.chat_id, "❌ This chat is not registered as a channel in the catalog. Use `/dchannelremove <ID>` to remove by ID.")

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
              "/welcome on <additional text> — enable greeting with additional text\n"
              "/welcome off — disable greeting")

def _parse_single_cmping(stdout, stderr, returncode):
    # Split by lines and remove carriage returns, comment lines
    lines = []
    combined = (stdout or "") + "\n" + (stderr or "")
    for raw_line in combined.splitlines():
        line = raw_line.split('\r')[-1].strip()
        if not line or line.startswith('#'):
            continue
        lines.append(line)
        
    rtt_line = None
    for line in lines:
        if "rtt min/avg/max/mdev" in line:
            rtt_line = line
            break
            
    if returncode == 0 and rtt_line:
        match = re.search(r'rtt min/avg/max/mdev\s*=\s*([0-9.]+)/([0-9.]+)/([0-9.]+)/([0-9.]+)', rtt_line)
        if match:
            try:
                return {
                    "success": True,
                    "min": float(match.group(1)),
                    "avg": float(match.group(2)),
                    "max": float(match.group(3)),
                    "mdev": float(match.group(4))
                }
            except Exception:
                pass
                
    err_lines = [l for l in lines if l.startswith('✗') or 'fail' in l.lower() or 'error' in l.lower() or 'connect' in l.lower()]
    if not err_lines:
        err_lines = [l for l in lines if l]
    clean_errs = []
    for el in err_lines:
        if "cmping" in el.lower() and "usage" in el.lower():
            continue
        if el.startswith('✗'):
            el = el.lstrip('✗').strip()
        clean_errs.append(el)
        
    err_msg = " / ".join(clean_errs[-2:]) or "Unknown failure"
    return {
        "success": False,
        "error": err_msg
    }

def _run_cmping_subprocess(cmd, timeout=60):
    """Run cmping as a subprocess with proper cleanup on timeout.
    Returns (stdout, stderr, returncode)."""
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, start_new_session=True
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        return stdout, stderr, proc.returncode
    except subprocess.TimeoutExpired:
        # Kill entire process group (cmping + deltachat-rpc-server children)
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, OSError):
            proc.kill()
        proc.communicate()  # drain pipes
        raise

def bg_cmping_worker(bot, accid, chat_id, msg_id, bot_domains, specified_servers):
    # Acquire global lock — only one cmping test at a time
    if not _cmping_global_lock.acquire(blocking=False):
        _send(bot, accid, chat_id, "⏳ Another CMPing test is running, your request is queued...")
        _cmping_global_lock.acquire()  # block until available

    try:
        _bg_cmping_worker_inner(bot, accid, chat_id, msg_id, bot_domains, specified_servers)
    finally:
        _cmping_global_lock.release()

def _bg_cmping_worker_inner(bot, accid, chat_id, msg_id, bot_domains, specified_servers):
    cmping_path = shutil.which("cmping") or "cmping"
    
    all_domains = list(bot_domains)
    for s in specified_servers:
        if s not in all_domains:
            all_domains.append(s)
            
    def get_index_emoji(idx: int) -> str:
        emojis = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
        if 1 <= idx <= 10:
            return emojis[idx - 1]
        return f"[{idx}]"

    domain_to_emoji = {}
    for idx, d in enumerate(all_domains, 1):
        domain_to_emoji[d] = get_index_emoji(idx)

    legend_lines = []
    for d in all_domains:
        legend_lines.append(f"{domain_to_emoji[d]} {d}")

    def is_general_error(err_str):
        err_lower = err_str.lower()
        keywords = [
            "dns", "configure profile", "setup receiver", "setup sender", 
            "failed to configure", "imap failed", "smtp failed", "connect",
            "no such file", "invalid", "usage", "failed to setup"
        ]
        return any(kw in err_lower for kw in keywords)

    def run_pair_ping(h1, h2):
        unique_domains = sorted(list(set([h1, h2])))
        locks = [_get_domain_lock(d) for d in unique_domains]
        for lock in locks:
            lock.acquire()
        try:
            forward_res = None
            forward_general = False
            try:
                cmd = [cmping_path, "-c", "3", h1, h2]
                logger.info(f"Running: {' '.join(cmd)}")
                stdout, stderr, rc = _run_cmping_subprocess(cmd, timeout=60)
                res = _parse_single_cmping(stdout, stderr, rc)
                if res.get("success"):
                    forward_res = res
                else:
                    forward_res = res
                    forward_general = is_general_error(res.get("error", ""))
            except subprocess.TimeoutExpired:
                forward_res = {"success": False, "error": "Timeout expired (60s)"}
                forward_general = False
            except Exception as e:
                forward_res = {"success": False, "error": str(e)}
                forward_general = is_general_error(str(e))
                
            if forward_res and not forward_res.get("success") and forward_general:
                return (h1, h2, forward_res, forward_general, None)
                
            backward_res = None
            try:
                cmd = [cmping_path, "-c", "3", h2, h1]
                logger.info(f"Running: {' '.join(cmd)}")
                stdout, stderr, rc = _run_cmping_subprocess(cmd, timeout=60)
                res = _parse_single_cmping(stdout, stderr, rc)
                if res.get("success"):
                    backward_res = res
                else:
                    backward_res = res
            except subprocess.TimeoutExpired:
                backward_res = {"success": False, "error": "Timeout expired (60s)"}
            except Exception as e:
                backward_res = {"success": False, "error": str(e)}
                
            return (h1, h2, forward_res, forward_general, backward_res)
        finally:
            for lock in reversed(locks):
                lock.release()

    results_map = {}
    def run_relay_pings(h1):
        relay_results = []
        for h2 in specified_servers:
            res = run_pair_ping(h1, h2)
            relay_results.append((h2, res))
        return h1, relay_results

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(bot_domains)) as executor:
        futures = {executor.submit(run_relay_pings, host1): host1 for host1 in bot_domains}
        
        for f in concurrent.futures.as_completed(futures):
            host1 = futures[f]
            try:
                h1, relay_results = f.result()
                for host2, res in relay_results:
                    results_map[(h1, host2)] = res
            except Exception as e:
                logger.error(f"Error in relay pings for {host1}: {e}")
                for host2 in specified_servers:
                    results_map[(host1, host2)] = (host1, host2, {"success": False, "error": str(e)}, False, None)

    report_body_lines = []
    all_failed = True

    for host2 in specified_servers:
        group_lines = []
        emoji2 = domain_to_emoji[host2]
        
        for host1 in bot_domains:
            emoji1 = domain_to_emoji[host1]
            res_tuple = results_map.get((host1, host2))
            if not res_tuple:
                continue
                
            _, _, forward_res, forward_general, backward_res = res_tuple
            
            if forward_res and not forward_res.get("success") and forward_general:
                err_msg = forward_res.get("error", "General setup error")
                group_lines.append(f"{emoji1}→{emoji2} ❌ {err_msg}")
                continue
                
            # 1. Forward line
            if forward_res and forward_res.get("success"):
                forward_str = f"{forward_res['avg']:.1f} ms"
                group_lines.append(f"{emoji1}→{emoji2} {forward_str}")
                all_failed = False
            else:
                err_msg = forward_res.get("error", "Unknown error") if forward_res else "Unknown error"
                err_suffix = f" {err_msg}" if err_msg else ""
                group_lines.append(f"{emoji1}→{emoji2} ❌{err_suffix}")
                
            # 2. Backward line
            if backward_res:
                err_msg = backward_res.get("error", "")
                if err_msg != "Skipped (forward failed)":
                    if backward_res.get("success"):
                        backward_str = f"{backward_res['avg']:.1f} ms"
                        group_lines.append(f"{emoji1}←{emoji2} {backward_str}")
                        all_failed = False
                    else:
                        err_suffix = f" {err_msg}" if err_msg else ""
                        group_lines.append(f"{emoji1}←{emoji2} ❌{err_suffix}")
            
        if group_lines:
            report_body_lines.extend(group_lines)
            report_body_lines.append("")

    from datetime import datetime, timezone
    gmt_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M GMT")
    
    report_parts = [
        "🏓 **CMPing Report:**",
        "\n".join(legend_lines)
    ]
    
    body_content = "\n".join(report_body_lines).strip()
    if body_content:
        report_parts.append(body_content)
        
    bot_name = os.environ.get("DISPLAY_NAME", "Bouncer Bot")
    report_parts.append(f"Generated {gmt_time} by {bot_name}")

    if all_failed:
        _react(bot, accid, msg_id, "❌")
    else:
        _react(bot, accid, msg_id, "☑️")
        
    _send(bot, accid, chat_id, "\n\n".join(report_parts).strip())

@dc_cli.on(events.NewMessage(command="/cmping"))
def cmping_command(bot, accid, event):
    msg = event.msg
    
    # 1. Check cooldown (debounce) - 15 seconds
    now = time.time()
    last_run = _chat_cmping_anti_spam.get(msg.chat_id, 0)
    diff = now - last_run
    if diff < CMPING_COOLDOWN_SECONDS:
        remaining_sec = max(1, int(CMPING_COOLDOWN_SECONDS - diff))
        _send(bot, accid, msg.chat_id, f"⌛️ Please wait {remaining_sec}s before running /cmping again.")
        return
        
    # 2. Parse command arguments
    payload_str = event.payload.strip() if event.payload else ""
    specified_servers = [s.strip().lower() for s in payload_str.split() if s.strip()]
    
    if not specified_servers:
        _send(bot, accid, msg.chat_id, "Usage: /cmping <server1> <server2> ... (max 5)")
        return

    if len(specified_servers) > 5:
        _send(bot, accid, msg.chat_id, "❌ Maximum 5 servers per request.")
        return

    # Update cooldown
    _chat_cmping_anti_spam[msg.chat_id] = now
    
    # 3. Add reaction ⏳
    _react(bot, accid, msg.id, "⏳")
    
    # 4. Get bot transport domains
    bot_domains = _get_bot_domains(bot, accid)

    if not bot_domains:
        _react(bot, accid, msg.id, "❌")
        _send(bot, accid, msg.chat_id, "❌ Error: Could not determine bot's own transport domain.")
        return

    # 5. Spawn background thread to run cmping
    threading.Thread(
        target=bg_cmping_worker,
        args=(bot, accid, msg.chat_id, msg.id, bot_domains, specified_servers),
        daemon=True
    ).start()

@dc_cli.on(events.NewMessage(command="/cmpingadd"))
def cmpingadd_command(bot, accid, event):
    msg = event.msg
    if not _is_dc_admin(bot, accid, msg.from_id):
        _send(bot, accid, msg.chat_id, "❌ Only the bot administrator can use /cmpingadd.")
        return

    text = msg.text.strip()
    parts = text.split(None, 1)
    if len(parts) < 2 or not parts[1].strip():
        _send(bot, accid, msg.chat_id, "Usage: /cmpingadd <server>\nExample: /cmpingadd talklink.fun")
        return

    domain = parts[1].strip().lower()
    # Basic validation
    if '.' not in domain or len(domain) < 3:
        _send(bot, accid, msg.chat_id, f"❌ Invalid domain: {domain}")
        return

    # Check if already a bot transport
    bot_domains = _get_bot_domains(bot, accid)
    if domain in bot_domains:
        _send(bot, accid, msg.chat_id, f"ℹ️ {domain} is already a bot transport, no need to add it.")
        return

    # Check if already monitored
    if database.is_cmping_monitor(domain):
        _send(bot, accid, msg.chat_id, f"ℹ️ {domain} is already in monitoring.")
        return

    database.add_cmping_monitor(domain)
    total_monitors = len(database.get_all_cmping_monitors())
    total = len(bot_domains) + total_monitors
    _send(bot, accid, msg.chat_id,
          f"✅ {domain} added to monitoring.\nTotal: {len(bot_domains)} transport + {total_monitors} monitored = {total} servers.")

@dc_cli.on(events.NewMessage(command="/cmpingdel"))
def cmpingdel_command(bot, accid, event):
    msg = event.msg
    if not _is_dc_admin(bot, accid, msg.from_id):
        _send(bot, accid, msg.chat_id, "❌ Only the bot administrator can use /cmpingdel.")
        return

    text = msg.text.strip()
    parts = text.split(None, 1)
    if len(parts) < 2 or not parts[1].strip():
        _send(bot, accid, msg.chat_id, "Usage: /cmpingdel <server>\nExample: /cmpingdel talklink.fun")
        return

    domain = parts[1].strip().lower()
    removed = database.remove_cmping_monitor(domain)
    if removed:
        # Clean up last results for this domain
        keys_to_remove = [k for k in _cmping_last_results if domain in k]
        for k in keys_to_remove:
            del _cmping_last_results[k]
        _send(bot, accid, msg.chat_id, f"✅ {domain} removed from monitoring.")
    else:
        _send(bot, accid, msg.chat_id, f"❌ {domain} is not in monitoring list.")

@dc_cli.on(events.NewMessage(command="/cmpinglist"))
def cmpinglist_command(bot, accid, event):
    msg = event.msg
    if not _is_dc_admin(bot, accid, msg.from_id):
        _send(bot, accid, msg.chat_id, "❌ Only the bot administrator can use /cmpinglist.")
        return

    bot_domains = _get_bot_domains(bot, accid)
    monitor_domains = database.get_all_cmping_monitors()

    all_servers = list(bot_domains)
    for d in monitor_domains:
        if d not in all_servers:
            all_servers.append(d)

    lines = ["📡 **CMPing Monitor Servers:**\n"]

    if bot_domains:
        lines.append("**Transport servers (auto):**")
        for i, d in enumerate(bot_domains, 1):
            lines.append(f"  {i}. {d}")
    else:
        lines.append("_No transport servers detected._")

    if monitor_domains:
        lines.append("\n**Monitored servers (manual):**")
        offset = len(bot_domains)
        for i, d in enumerate(monitor_domains, offset + 1):
            lines.append(f"  {i}. {d}")
    else:
        lines.append("\n_No manually added servers._")

    n = len(all_servers)
    pairs = n * (n - 1) // 2
    lines.append(f"\nTotal: {n} servers, {pairs} unique pairs")

    if n >= 2:
        source_idx = _cmping_monitor_index % n
        next_source = all_servers[source_idx]
        interval_min = CMPING_MONITOR_INTERVAL // 60
        lines.append(f"Next check source: {next_source}")
        lines.append(f"Interval: {interval_min} min | Full rotation: ~{n * interval_min} min")

    _send(bot, accid, msg.chat_id, "\n".join(lines))

@dc_cli.on(events.NewMessage(command="/cmreport"))
def cmreport_command(bot, accid, event):
    msg = event.msg
    if not _is_dc_admin(bot, accid, msg.from_id):
        _send(bot, accid, msg.chat_id, "❌ Only the bot administrator can use /cmreport.")
        return

    text = msg.text.strip()
    parts = text.split(None, 1)
    arg = parts[1].strip().lower() if len(parts) > 1 else ""

    if arg == "on":
        database.add_cmping_report_chat(msg.chat_id)
        _send(bot, accid, msg.chat_id, "✅ CMPing monitoring alerts **enabled** for this chat.")
    elif arg == "off":
        removed = database.remove_cmping_report_chat(msg.chat_id)
        if removed:
            _send(bot, accid, msg.chat_id, "✅ CMPing monitoring alerts **disabled** for this chat.")
        else:
            _send(bot, accid, msg.chat_id, "ℹ️ CMPing monitoring alerts were not enabled for this chat.")
    else:
        # Show status
        from datetime import datetime, timezone
        is_enabled = database.is_cmping_report_chat(msg.chat_id)
        if is_enabled:
            enabled_at = database.get_cmping_report_chat_enabled_at(msg.chat_id)
            if enabled_at:
                dt = datetime.fromtimestamp(enabled_at, tz=timezone.utc).strftime("%Y-%m-%d %H:%M GMT")
                _send(bot, accid, msg.chat_id, f"📊 CMPing monitoring alerts: ✅ enabled (since {dt})\n\nUse `/cmreport off` to disable.")
            else:
                _send(bot, accid, msg.chat_id, "📊 CMPing monitoring alerts: ✅ enabled\n\nUse `/cmreport off` to disable.")
        else:
            _send(bot, accid, msg.chat_id, "📊 CMPing monitoring alerts: ❌ disabled\n\nUse `/cmreport on` to enable.")

@dc_cli.on(events.NewMessage(command="/cmpingstatus"))
def cmpingstatus_command(bot, accid, event):
    msg = event.msg
    if not _is_dc_admin(bot, accid, msg.from_id):
        _send(bot, accid, msg.chat_id, "❌ Only the bot administrator can use /cmpingstatus.")
        return

    from datetime import datetime, timezone

    bot_domains = _get_bot_domains(bot, accid)
    monitor_domains = database.get_all_cmping_monitors()
    all_servers = list(bot_domains)
    for d in monitor_domains:
        if d not in all_servers:
            all_servers.append(d)

    if len(all_servers) < 2:
        _send(bot, accid, msg.chat_id, "ℹ️ Fewer than 2 servers configured for monitoring.")
        return

    now = time.time()
    lines = ["📊 **CMPing Monitor Status:**\n"]

    has_any = False
    for src in all_servers:
        for dst in all_servers:
            if src == dst:
                continue
            key = (src, dst)
            result = _cmping_last_results.get(key)
            if result is None:
                continue
            has_any = True
            checked_at = result.get("checked_at", 0)
            age_min = int((now - checked_at) / 60) if checked_at else 0

            if result.get("success"):
                avg = result.get("avg", 0)
                lines.append(f"✅ {src} → {dst}: {avg:.1f} ms ({age_min} min ago)")
            else:
                err = result.get("error", "Unknown")
                lines.append(f"❌ {src} → {dst}: {err} ({age_min} min ago)")

    if not has_any:
        lines.append("_No checks performed yet. Monitoring starts after bot startup._")

    # Add rotation info
    n = len(all_servers)
    source_idx = _cmping_monitor_index % n
    next_source = all_servers[source_idx]
    interval_min = CMPING_MONITOR_INTERVAL // 60
    lines.append(f"\nSource rotation: {_cmping_monitor_index % n + 1}/{n} (next: {next_source})")
    lines.append(f"Interval: {interval_min} min")

    _send(bot, accid, msg.chat_id, "\n".join(lines))

@dc_cli.on(events.NewMessage(command="/cmfaillist"))
def cmfaillist_command(bot, accid, event):
    msg = event.msg
    if not _is_dc_admin(bot, accid, msg.from_id):
        _send(bot, accid, msg.chat_id, "❌ Only the bot administrator can use /cmfaillist.")
        return

    bot_domains = _get_bot_domains(bot, accid)
    monitor_domains = database.get_all_cmping_monitors()
    all_servers = list(bot_domains)
    for d in monitor_domains:
        if d not in all_servers:
            all_servers.append(d)

    if len(all_servers) < 2:
        _send(bot, accid, msg.chat_id, "ℹ️ Fewer than 2 servers configured for monitoring.")
        return

    now = time.time()
    lines = ["🔴 **Current CMPing Monitor Failures:**\n"]

    failed_count = 0
    for src in all_servers:
        for dst in all_servers:
            if src == dst:
                continue
            key = (src, dst)
            result = _cmping_last_results.get(key)
            if result is None:
                continue
            if not result.get("success"):
                failed_count += 1
                checked_at = result.get("checked_at", 0)
                age_min = int((now - checked_at) / 60) if checked_at else 0
                err = result.get("error", "Unknown")
                lines.append(f"❌ {src} → {dst}: {err} ({age_min} min ago)")

    if failed_count == 0:
        _send(bot, accid, msg.chat_id, "✅ **All monitored links are healthy.**\nNo failures detected at the moment.")
    else:
        _send(bot, accid, msg.chat_id, "\n".join(lines))

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
                    new_member_id = None
                    
                    # Try msg attributes first (may not exist in all deltachat2 versions)
                    for attr_name in ('info_contact_id', 'infoContactId'):
                        val = getattr(msg, attr_name, None)
                        if val:
                            new_member_id = val
                            break
                    if not new_member_id and isinstance(msg, dict):
                        new_member_id = msg.get('info_contact_id') or msg.get('infoContactId')
                    
                    logger.info(f"Welcome greeting: join event in chat {dc_chat_id}, msg.text={msg.text!r}, info_contact_id={new_member_id!r}")
                    
                    # Parse the system message text to extract who was added
                    # DC system messages look like:
                    #   "Member Name added by Admin Name."
                    #   "Member added by Admin."  
                    #   "You added Member Name."
                    #   "Member Name (addr@example.com) added by ..."
                    if not new_member_id and msg.text:
                        text = msg.text.strip().rstrip('.')
                        added_name = None
                        
                        # Pattern: "You added <name>"
                        m = re.match(r'^[Yy]ou added (.+)$', text)
                        if m:
                            added_name = m.group(1).strip()
                        
                        # Pattern: "<name> added by <someone>"
                        if not added_name:
                            m = re.match(r'^(.+?)\s+added\s+by\s+.+$', text, re.IGNORECASE)
                            if m:
                                added_name = m.group(1).strip()
                        
                        # Pattern: "<name> added" (no "by")
                        if not added_name:
                            m = re.match(r'^(.+?)\s+added$', text, re.IGNORECASE)
                            if m:
                                added_name = m.group(1).strip()
                        
                        # Pattern: "Member <name> joined" or "<name> joined"
                        if not added_name:
                            m = re.match(r'^(?:Member\s+)?(.+?)\s+joined$', text, re.IGNORECASE)
                            if m:
                                added_name = m.group(1).strip()
                        
                        logger.info(f"Welcome greeting: parsed added_name={added_name!r} from text={msg.text!r}")
                        
                        if added_name:
                            # Try lookup by email address first
                            try:
                                new_member_id = bot.rpc.lookup_contact_id_by_addr(accid, added_name)
                            except Exception:
                                new_member_id = None
                            
                            # Try finding in chat contacts by name/address
                            if not new_member_id:
                                new_member_id = find_contact_in_chat(bot, accid, dc_chat_id, added_name)
                            
                            logger.info(f"Welcome greeting: lookup for {added_name!r} -> contact_id={new_member_id!r}")
                    
                    # Last resort: if we detected a join but couldn't identify who,
                    # check from_id (the person who triggered the event)
                    if not new_member_id and msg.from_id and msg.from_id != 1:
                        # from_id in a join info message is usually the person who added,
                        # not the person who was added — so skip this for self-join scenarios
                        logger.info(f"Welcome greeting: could not resolve new member, from_id={msg.from_id}")
                    
                    logger.info(f"Welcome greeting: resolved new_member_id={new_member_id!r}")
                    if new_member_id and new_member_id != 1:
                        try:
                            contact = bot.rpc.get_contact(accid, new_member_id)
                            member_name = contact.name or contact.display_name or contact.address or "New member"
                            user_chat_count = get_user_chat_count(bot, accid, new_member_id)
                            
                            database.ensure_contact_first_seen(new_member_id, time.time())
                            age = _get_contact_age_indicator(new_member_id)
                            
                            chat_name = catalog_chat['name']
                            welcome_suffix = f" {catalog_chat['welcome_text']}" if catalog_chat.get('welcome_text') else ""
                            
                            welcome_msg = f"👋🏻 {age} **{member_name}** (💬 {user_chat_count}), welcome to {chat_name} group!{welcome_suffix}"
                            _send(bot, accid, dc_chat_id, welcome_msg)
                            logger.info(f"Welcome greeting sent for {member_name} in chat {dc_chat_id}")
                        except Exception as welcome_err:
                            logger.error(f"Failed to send welcome greeting: {welcome_err}")
                
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

def find_contact_in_chat(bot, accid, chat_id, name_or_address):
    if not name_or_address:
        return None
    name_or_address_lower = name_or_address.strip().lower()
    
    # Try looking up by address directly first
    try:
        cid = bot.rpc.lookup_contact_id_by_addr(accid, name_or_address)
        if cid and cid != 1:
            return cid
    except Exception:
        pass
        
    # Search chat contacts by comparing attributes
    try:
        contacts = bot.rpc.get_chat_contacts(accid, chat_id)
        for contact_id in contacts:
            if contact_id == 1:
                continue
            try:
                contact = bot.rpc.get_contact(accid, contact_id)
                c_name = getattr(contact, 'name', None) or ""
                c_disp = getattr(contact, 'display_name', None) or ""
                c_addr = getattr(contact, 'address', None) or ""
                
                if (c_name.strip().lower() == name_or_address_lower or 
                    c_disp.strip().lower() == name_or_address_lower or 
                    c_addr.strip().lower() == name_or_address_lower):
                    return contact_id
            except Exception:
                continue
    except Exception as e:
        logger.error(f"Error searching chat contacts for {name_or_address}: {e}")
        
    return None

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
    
    # Handle /chatdesc<ID> command
    if text.startswith("/chatdesc"):
        m = re.match(r'^/chatdesc(\d+)(?:\s+(.*))?$', text, re.IGNORECASE)
        if m:
            if not _is_dc_admin(bot, accid, msg.from_id):
                _send(bot, accid, msg.chat_id, "❌ Only the bot administrator can use this command.")
                return
                
            catalog_id = int(m.group(1))
            new_desc = m.group(2).strip() if m.group(2) else ""
            
            catalog_chat = database.get_catalog_chat_by_id(catalog_id)
            if not catalog_chat:
                _send(bot, accid, msg.chat_id, "❌ Chat with this number was not found in the catalog.")
                return
                
            database.update_catalog_chat_description(catalog_id, new_desc)
            _send(bot, accid, msg.chat_id, f"✅ Description for chat **{catalog_chat['name']}** has been updated.")
            return

    # 1. Handle /chat<ID> command (but NOT /chats)
    elif text.startswith("/chat") and not (text == "/chats" or text.startswith("/chats ")):
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
                    database.ensure_contact_first_seen(msg.from_id, time.time())
                    age = _get_contact_age_indicator(msg.from_id)
                    
                    request_id = database.add_pending_request(
                        catalog_id, chat_id, msg.from_id, requester_name, message_payload
                    )
                    
                    if message_payload:
                        req_msg = (
                            f"{age} **{requester_name}** (💬 {user_chat_count}) wants to join this chat with following message: {message_payload}\n"
                            f"To approve: reply with /approve{request_id}\n"
                            f"To decline: reply with /decline{request_id} (optional comment after command)"
                        )
                    else:
                        req_msg = (
                            f"{age} **{requester_name}** (💬 {user_chat_count}) wants to join this chat.\n"
                            f"To approve: reply with /approve{request_id}\n"
                            f"To decline: reply with /decline{request_id} (optional comment after command)"
                        )
                    
                    _send(bot, accid, chat_id, req_msg)
                    _send(bot, accid, msg.chat_id, "⏳ Your request to join has been sent to group members. Please wait for approval.")
                except Exception as e:
                    logger.error(f"Failed to create join request: {e}")
                    return

    # Handle /dchanneldesc<ID> command
    elif text.startswith("/dchanneldesc"):
        m = re.match(r'^/dchanneldesc(\d+)(?:\s+(.*))?$', text, re.IGNORECASE)
        if m:
            if not _is_dc_admin(bot, accid, msg.from_id):
                _send(bot, accid, msg.chat_id, "❌ Only the bot administrator can use this command.")
                return
                
            catalog_id = int(m.group(1))
            new_desc = m.group(2).strip() if m.group(2) else ""
            
            channel = database.get_catalog_channel_by_id(catalog_id)
            if not channel:
                _send(bot, accid, msg.chat_id, "❌ Channel with this number was not found in the catalog.")
                return
                
            database.update_catalog_channel_description(catalog_id, new_desc)
            _send(bot, accid, msg.chat_id, f"✅ Description for channel **{channel['name']}** has been updated.")
            return

    # Handle /dchannel<ID> command (but NOT /dchannels)
    elif text.startswith("/dchannel") and not (text == "/dchannels" or text.startswith("/dchannels ")):
        m = re.match(r'^/dchannel(\d+)', text, re.IGNORECASE)
        if m:
            catalog_id = int(m.group(1))
            channel = database.get_catalog_channel_by_id(catalog_id)
            if not channel:
                _send(bot, accid, msg.chat_id, "❌ Channel with this number was not found in the catalog.")
                return
                
            invite_link = channel.get('invite_link')
            if invite_link:
                if invite_link.startswith("OPEN-CHAT:"):
                    invite_link = "https://i.delta.chat/#" + invite_link[10:]
                elif invite_link.startswith("OPEN:"):
                    invite_link = "https://i.delta.chat/#" + invite_link[5:]
                elif invite_link.startswith("dcqr://"):
                    invite_link = "https://i.delta.chat/#" + invite_link[7:]
                
                _send(bot, accid, msg.chat_id, f"🔗 Invite link to channel **{channel['name']}**:\n{invite_link}")
            else:
                _send(bot, accid, msg.chat_id, "❌ No invite link registered for this channel.")
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
