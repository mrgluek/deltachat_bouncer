"""Generic Delta Chat RPC helpers shared across command/handler modules.

Admin/fingerprint checks, message sending with transport-stat tracking,
reactions, the delayed-command debouncer, and small chat-lookup utilities.
"""
import logging
import os
import re
import threading
import time

from deltachat2 import MsgData

import database
import state
from config import logger


def _get_domain_lock(domain: str) -> threading.Lock:
    with state._domain_locks_lock:
        if domain not in state._domain_locks:
            state._domain_locks[domain] = threading.Lock()
        return state._domain_locks[domain]


def _prune_anti_spam_dicts():
    """Prune anti-spam entries older than 3600 seconds to prevent memory growth."""
    now = time.time()
    cutoff = now - 3600
    for spam_dict in (
        state._chat_bounce_anti_spam,
        state._chat_top_anti_spam,
        state._chat_invite_anti_spam,
        state._chat_relays_anti_spam,
        state._chat_search_anti_spam,
        state._chat_cmping_anti_spam,
        state._chat_slap_anti_spam,
        state._chat_sticker_anti_spam,
        state._chat_stickernobg_anti_spam,
    ):
        expired = [cid for cid, ts in list(spam_dict.items()) if ts < cutoff]
        for cid in expired:
            spam_dict.pop(cid, None)


def _prune_domain_locks(active_domains: set[str]):
    """Remove locks for domains that are no longer monitored."""
    with state._domain_locks_lock:
        stale = [d for d in list(state._domain_locks.keys()) if d not in active_domains]
        for d in stale:
            state._domain_locks.pop(d, None)


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


# Age indicator: each circle/square = 1 week of bot knowing the user
_AGE_CIRCLES = ["🔴", "🟠", "🟡", "🟢", "🔵", "🟣", "🟤", "⚫", "⚪"]
_AGE_SQUARES = ["🟥", "🟧", "🟨", "🟩", "🟦", "🟪", "🟫", "⬛", "⬜"]


def _get_contact_age_indicator(contact_id: int, is_multi_chat: bool = False) -> str:
    """Return a colored circle emoji (or square if member of multiple community chats)
    based on how long the bot has known this contact.
    🔴/🟥 = <1 week, 🟠/🟧 = 1-2 weeks, ... ⚪/⬜ = 8+ weeks."""
    now = time.time()
    first_seen = database.get_contact_first_seen(contact_id)
    if first_seen is None:
        database.ensure_contact_first_seen(contact_id, now)
        first_seen = now
    weeks = int((now - first_seen) / (7 * 24 * 3600))
    palette = _AGE_SQUARES if is_multi_chat else _AGE_CIRCLES
    idx = min(weeks, len(palette) - 1)
    return palette[idx]


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
        except Exception:
            pass

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
            if logger.isEnabledFor(logging.DEBUG):
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


def _is_dc_admin(bot, accid, contact_id, contact=None):
    """Check if the given contact is the bot administrator (by email or fingerprint)."""
    try:
        if not contact:
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
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(f"Admin check (FP) for {contact_id}: stored={admin_fp}, contact={c_fp}")
            if c_fp:
                if admin_fp.upper() in c_fp.upper().split(','):
                    return True

            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(f"Admin check: Fingerprint mismatch or missing for {contact_id}")
            return False

        # 2. Check email
        sender_email = contact.address
        admin_email = database.get_config("admin_dc_email")
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(f"Admin check (Email) for {contact_id}: stored={admin_email}, contact={sender_email}")
        if admin_email and sender_email and admin_email.lower().strip() == sender_email.lower().strip():
            return True

    except Exception as e:
        logger.error(f"Critical error in admin check: {e}")
    return False


def _is_contact_autokick_ignored(bot, accid, contact_id, contact=None) -> bool:
    """Check if the contact's cryptographic fingerprint is in the autokick ignore list."""
    try:
        c_fp = _get_contact_fingerprint(bot, accid, contact_id, contact=contact)
        if c_fp:
            for fp in c_fp.upper().split(','):
                clean = fp.strip()
                if clean and database.is_fingerprint_autokick_ignored(clean):
                    return True
    except Exception as e:
        logger.error(f"Error checking autokick ignore for contact {contact_id}: {e}")
    return False


def _get_user_badges(bot, accid, contact_id: int, contact=None) -> str:
    """Return badges for special user roles/statuses:
    👑 = bot admin
    ⭐ = autokick ignored
    💤 = away
    """
    badges = []
    try:
        if _is_dc_admin(bot, accid, contact_id, contact=contact):
            badges.append("👑")
    except Exception:
        pass
    try:
        if _is_contact_autokick_ignored(bot, accid, contact_id, contact=contact):
            badges.append("⭐")
    except Exception:
        pass
    try:
        if database.get_away_status(contact_id):
            badges.append("💤")
    except Exception:
        pass
    return " ".join(badges)


def _get_chat_autokick_warn_threshold(autokick_days: int) -> int:
    """Calculate the inactivity threshold in days when warnings start.
    If autokick > 7 days: warnings start at autokick - 7 days.
    If autokick <= 7 days: warnings start at autokick - 1 days (minimum 1 day).
    """
    if autokick_days > 7:
        return autokick_days - 7
    return max(1, autokick_days - 1)


def _send(bot, accid, chat_id, text, reply_to_id=None):
    msg_data = MsgData(text=text)
    if reply_to_id:
        msg_data.quoted_message_id = reply_to_id

    try:
        msg_id = bot.rpc.send_msg(accid, chat_id, msg_data)

        # Track success
        try:
            addr = bot.rpc.get_config(accid, "configured_addr") or bot.rpc.get_config(accid, "addr") or "unknown"
            if addr and isinstance(addr, str) and addr != "unknown":
                database.increment_transport_sent(addr)
        except Exception:
            pass

        return msg_id
    except Exception as e:
        error_str = str(e).lower()
        if "not a member of the chat" in error_str:
            logger.warning(f"Cannot send message to chat {chat_id}: bot is not a member of the chat.")
            return None
        logger.error(f"Failed to send message to chat {chat_id}: {e}")
        return None


def _react(bot, accid, msg_id, reaction):
    """Add a reaction to a message."""
    try:
        bot.rpc.send_reaction(accid, msg_id, [reaction] if reaction else [])
    except Exception as e:
        logger.warning(f"Failed to send reaction {reaction}: {e}")


def _queue_delayed_command(bot, accid, msg, command_key: str, remaining_sec: float, handler_fn, *args, **kwargs) -> bool:
    """React with ⏳ and queue the command to execute once remaining_sec expires.
    When execution finishes, changes reaction to ☑️ on all queued messages.
    Suppresses duplicate executions if already pending for this chat.
    """
    _react(bot, accid, msg.id, "⏳")
    key = f"{command_key}:{msg.chat_id}"

    with state._pending_delayed_lock:
        if key in state._pending_delayed_commands:
            timer, msg_ids = state._pending_delayed_commands[key]
            if msg.id not in msg_ids:
                msg_ids.append(msg.id)
            return True

        msg_ids = [msg.id]

        def _runner():
            with state._pending_delayed_lock:
                entry = state._pending_delayed_commands.pop(key, None)
                final_msg_ids = entry[1] if entry else msg_ids
            try:
                handler_fn(*args, **kwargs)
                for mid in final_msg_ids:
                    _react(bot, accid, mid, "☑️")
            except Exception as e:
                logger.error(f"Error running delayed command {command_key} for chat {msg.chat_id}: {e}")
                for mid in final_msg_ids:
                    _react(bot, accid, mid, "❌")

        timer = threading.Timer(max(0.01, float(remaining_sec)), _runner)
        timer.daemon = True
        state._pending_delayed_commands[key] = (timer, msg_ids)
        timer.start()
        return True


def clear_pending_delayed_commands():
    """Cancel and clear all pending delayed command timers."""
    with state._pending_delayed_lock:
        for timer, _ in state._pending_delayed_commands.values():
            try:
                timer.cancel()
            except Exception:
                pass
        state._pending_delayed_commands.clear()


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


def _extract_first_url(text: str) -> str | None:
    """Extract the first http/https URL from text, stripping trailing punctuation."""
    if not text:
        return None
    match = re.search(r'(https?://[^\s<>"]+)', text)
    if not match:
        return None
    url = match.group(1).rstrip(".,!?:;)'\"")
    return url if url else None


def _get_msg_file_info(bot, accid, msg) -> dict | None:
    """Extract local file path and filename from a Delta Chat message object, downloading if needed."""
    if not msg:
        return None

    raw_id = getattr(msg, "id", None) if not isinstance(msg, dict) else msg.get("id")
    msg_id = raw_id if isinstance(raw_id, int) else None

    file_path = getattr(msg, "file", None) if not isinstance(msg, dict) else msg.get("file")
    if not isinstance(file_path, str):
        file_path = None

    file_name = getattr(msg, "filename", None) if not isinstance(msg, dict) else msg.get("filename")
    if not isinstance(file_name, str):
        file_name = None

    if file_path and os.path.exists(file_path):
        name = file_name or os.path.basename(file_path)
        return {"path": file_path, "filename": name}

    raw_ds = getattr(msg, "download_state", None) if not isinstance(msg, dict) else msg.get("download_state")
    d_state = str(raw_ds).lower() if raw_ds is not None else ""
    raw_vt = getattr(msg, "view_type", None) if not isinstance(msg, dict) else msg.get("view_type")
    view_type = raw_vt.lower() if isinstance(raw_vt, str) else ""
    raw_fb = getattr(msg, "file_bytes", None) if not isinstance(msg, dict) else msg.get("file_bytes")
    file_bytes = raw_fb if isinstance(raw_fb, int) and not isinstance(raw_fb, bool) else 0

    has_attachment = (
        bool(file_path)
        or bool(file_name)
        or file_bytes > 0
        or any(t in view_type for t in ("file", "image", "audio", "video", "voice", "gif", "sticker"))
        or any(s in d_state for s in ("available", "inprogress", "10", "100", "1000"))
    )

    if has_attachment and msg_id and bot and hasattr(bot, "rpc"):
        try:
            if hasattr(bot.rpc, "download_full_message"):
                bot.rpc.download_full_message(accid, msg_id)
            elif hasattr(bot.rpc, "download_msg"):
                bot.rpc.download_msg(accid, msg_id)
        except Exception as e:
            logger.warning(f"Failed to trigger download for msg {msg_id}: {e}")

        start = time.time()
        while time.time() - start < 30.0:
            time.sleep(0.5)
            try:
                cur = bot.rpc.get_message(accid, msg_id)
                cur_path = getattr(cur, "file", None) if not isinstance(cur, dict) else cur.get("file")
                if isinstance(cur_path, str) and os.path.exists(cur_path):
                    cur_name = getattr(cur, "filename", None) if not isinstance(cur, dict) else cur.get("filename")
                    return {"path": cur_path, "filename": cur_name if isinstance(cur_name, str) else os.path.basename(cur_path)}
                raw_cur_ds = getattr(cur, "download_state", None) if not isinstance(cur, dict) else cur.get("download_state")
                cur_ds = str(raw_cur_ds).lower() if raw_cur_ds is not None else ""
                if "failure" in cur_ds or cur_ds == "20":
                    logger.warning(f"Download failed for msg {msg_id}")
                    break
            except Exception:
                break

    return None
