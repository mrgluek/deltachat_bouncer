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
import dc_fingerprint
import state
from config import logger
from outbox import outbox, PRIO_ALERT, PRIO_REPLY, PRIO_LOW  # noqa: F401  (priorities for callers)


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
        state._newgroup_anti_spam,
        state._chat_relays_anti_spam,
        state._chat_search_anti_spam,
        state._chat_cmping_anti_spam,
        state._chat_cmcall_anti_spam,
        state._chat_slap_anti_spam,
        state._mention_anti_spam,
        state._chat_sticker_anti_spam,
        state._chat_stickernobg_anti_spam,
        state._faq_anti_spam,
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
            addr = dc_fingerprint.get_self_addr(bot.rpc, accid)
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
    """The contact's key fingerprint, or None; see dc_fingerprint. `contact` is unused, kept for callers."""
    return dc_fingerprint.get_contact_fingerprint(bot.rpc, accid, contact_id)


def admin_key_contact_id(bot, accid):
    """The admin's contact WITH their key, for messages to the admin.

    The core keeps an address-only contact and one contact per key for each address, and
    lookup_contact_id_by_addr / create_contact give the address-only one: current cores cannot
    send to it ("e2e encryption unavailable"), so admin notices were lost. The key contact with
    the fingerprint saved by /initadmin is preferred, then the most recently seen key contact of
    the admin address (another device of the admin), then the old lookup (older cores)."""
    email = (database.get_config("admin_dc_email") or "").strip().lower()
    if not email:
        return None
    admin_fp = re.sub(r"[^0-9A-F]", "", (database.get_admin_fingerprint() or "").upper())
    try:
        candidates = bot.rpc.get_contact_ids(accid, 0, email) or []
    except Exception as e:
        logger.debug(f"Could not list admin contacts: {e}")
        candidates = []
    key_contacts = []
    for cid in candidates:
        try:
            c = bot.rpc.get_contact(accid, cid)
            get_val = c.get if isinstance(c, dict) else (lambda k, d=None, _c=c: getattr(_c, k, d))
            if (get_val("address") or "").strip().lower() != email or not get_val("is_key_contact", False):
                continue
            if admin_fp and admin_fp in (_get_contact_fingerprint(bot, accid, cid, c) or "").split(","):
                return cid
            key_contacts.append((get_val("last_seen", 0) or 0, cid))
        except Exception:
            continue
    if key_contacts:
        return max(key_contacts)[1]
    try:
        return bot.rpc.lookup_contact_id_by_addr(accid, email) or None
    except Exception:
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


def _send_now(bot, accid, chat_id, msg_data):
    """send_msg right now (inside an outbox job); errors are logged and give None."""
    try:
        return bot.rpc.send_msg(accid, chat_id, msg_data)
    except Exception as e:
        if "not a member of the chat" in str(e).lower():
            logger.warning(f"Cannot send message to chat {chat_id}: bot is not a member of the chat.")
        else:
            logger.error(f"Failed to send message to chat {chat_id}: {e}")
        return None


def _result(fut, wait: bool):
    """The job's result if it ran already (outbox not started, as in tests) or wait is set, else None."""
    if wait or fut.done():
        return fut.result()
    return None


def queue_msg(bot, accid, chat_id, msg_data, priority=PRIO_REPLY, wait=False, label=None):
    """Sends a prepared MsgData through the paced outbox (see outbox.py).

    wait=True blocks until it was sent and returns the msg_id - for a message that is edited
    later, or whose file is deleted right after (the core reads the file when it sends)."""
    fut = outbox.submit(lambda: _send_now(bot, accid, chat_id, msg_data), priority=priority,
                        label=label or f"message to chat {chat_id}")
    return _result(fut, wait)


def _send(bot, accid, chat_id, text, reply_to_id=None, file=None, viewtype=None, priority=PRIO_REPLY, wait=False):
    """Queues a text (optionally with a file) in the paced outbox; default priority: a reply.

    Returns the msg_id only with wait=True or when sent right away (tests), else None."""
    msg_data = MsgData(text=text)
    if reply_to_id:
        msg_data.quoted_message_id = reply_to_id
    if file:
        msg_data.file = file
        msg_data.viewtype = viewtype or "File"
    return queue_msg(bot, accid, chat_id, msg_data, priority=priority, wait=wait)


def _edit_or_send(bot, accid, chat_id, msg_id, text, priority=PRIO_REPLY, key=None, reply_to_id=None,
                  on_new_id=None, wait=False):
    """Queues an edit of msg_id, or a new message if there is none or the edit fails.

    msg_id may be a callable returning the current id: it is read when the job runs, so a first
    message still waiting is merged with later updates (same key) and goes out once, as a new
    message with the latest text. on_new_id(new_id) stores a new message's id. key defaults to
    the message (a newer edit of it replaces a queued one)."""
    def job():
        current = msg_id() if callable(msg_id) else msg_id
        if current:
            try:
                bot.rpc.send_edit_request(accid, current, text)
                return current
            except Exception as e:
                logger.warning(f"Editing message {current} in chat {chat_id} failed (sending a new one): {e}")
        msg_data = MsgData(text=text)
        if reply_to_id:
            msg_data.quoted_message_id = reply_to_id
        new_id = _send_now(bot, accid, chat_id, msg_data)
        if new_id and on_new_id:
            on_new_id(new_id)
        return new_id

    if key is None and msg_id and not callable(msg_id):
        key = ("edit", msg_id)
    fut = outbox.submit(job, priority=priority, key=key, label=f"edit/message in chat {chat_id}")
    return _result(fut, wait)


def _react(bot, accid, msg_id, reaction, priority=PRIO_REPLY):
    """Queues a reaction; keyed by message, so a ⏳ still waiting is replaced by the final one."""
    def job():
        try:
            bot.rpc.send_reaction(accid, msg_id, [reaction] if reaction else [])
        except Exception as e:
            logger.warning(f"Failed to send reaction {reaction}: {e}")
    outbox.submit(job, priority=priority, key=("reaction", msg_id), label=f"reaction {reaction} on {msg_id}")


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


def is_bot_address(address: str, bot_addrs) -> bool:
    """True if address is one of bot_addrs (full address or bare local part)."""
    addr = (address or "").strip().lower()
    if not addr:
        return False
    local_part = addr.split("@", 1)[0]
    return any(a == addr or ("@" not in a and a == local_part) for a in bot_addrs)


is_ntfy_bot_address = is_bot_address


def chat_has_ntfy_bot(bot, accid, chat_id, ntfy_addrs) -> bool:
    """True if a member of chat_id is Ntfy Bot (matched by address, see config.NTFY_BOT_ADDRS).
    Matching by address and not by display name: anyone can call themselves "Ntfy Bot"."""
    if not ntfy_addrs:
        return False
    try:
        contacts = bot.rpc.get_chat_contacts(accid, chat_id)
    except Exception:
        return False
    for contact_id in contacts:
        if contact_id == 1:  # ourselves
            continue
        try:
            contact = bot.rpc.get_contact(accid, contact_id)
        except Exception:
            continue
        if is_ntfy_bot_address(getattr(contact, "address", ""), ntfy_addrs):
            return True
    return False


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
