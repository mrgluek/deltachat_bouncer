"""@mentions: a group message that starts with "@Nick" is sent again by the bot
as a reply to Nick's last message in the chat, so Nick gets a notification.

Delta Chat has no real mentions; a reply to someone's message is what makes
their client notify them (also in a muted chat). The copy goes out under the
author's name (override_sender_name, shown as "~Name"), like TG Bridge does.
"""

import re
import threading
import time

from deltachat2 import MsgData

import config
import state

# "@Nick" at the very start; the nick ends at whitespace or punctuation
MENTION_RE = re.compile(r"^@([^\s,:;!?()\[\]\"'«»]+)")
MENTION_SCAN_LIMIT = 500  # how many recent messages are searched for the target's last one
MENTION_MIN_PREFIX = 3    # "@Glu" may stand for "Gluek" when only one member matches


def parse_mention(text: str) -> str | None:
    """The nick from "@Nick ..." at the start of text, or None."""
    m = MENTION_RE.match((text or "").strip())
    if not m:
        return None
    nick = m.group(1).rstrip(".")
    return nick or None


def _contact_names(contact) -> list[str]:
    names = []
    for value in (getattr(contact, "name", None), getattr(contact, "display_name", None)):
        value = (value or "").strip().casefold()
        if value and value not in names:
            names.append(value)
    return names


def _match_rank(contact, nick: str) -> int:
    """2: exact match, 1: unambiguous-prefix candidate, 0: no match."""
    nick = nick.casefold()
    address = (getattr(contact, "address", None) or "").strip().casefold()
    local_part = address.split("@")[0]
    names = _contact_names(contact)

    exact = {address, local_part}
    for name in names:
        exact.add(name)
        exact.add(name.replace(" ", ""))
    exact.discard("")
    if nick in exact:
        return 2

    if len(nick) >= MENTION_MIN_PREFIX:
        for candidate in names + [local_part]:
            if candidate.startswith(nick) or candidate.replace(" ", "").startswith(nick):
                return 1
    return 0


def resolve_mention(bot, accid, chat_id: int, nick: str, sender_id: int):
    """The chat member that nick names, as (contact_id, contact), or None.
    An exact match wins; otherwise a prefix match is used only when it is unique."""
    try:
        member_ids = bot.rpc.get_chat_contacts(accid, chat_id)
    except Exception as e:
        config.logger.error(f"Failed to get chat contacts for mention in chat {chat_id}: {e}")
        return None

    exact, prefix = [], []
    for contact_id in member_ids:
        if contact_id <= 9 or contact_id == sender_id:
            continue
        try:
            contact = bot.rpc.get_contact(accid, contact_id)
        except Exception:
            continue
        rank = _match_rank(contact, nick)
        if rank == 2:
            exact.append((contact_id, contact))
        elif rank == 1:
            prefix.append((contact_id, contact))

    if len(exact) == 1:
        return exact[0]
    if not exact and len(prefix) == 1:
        return prefix[0]
    return None


def find_last_message_from(bot, accid, chat_id: int, contact_id: int, before_msg_id: int) -> int | None:
    """Id of contact_id's latest non-info message in the chat before before_msg_id."""
    try:
        msg_ids = bot.rpc.get_message_ids(accid, chat_id, False, False)
    except Exception as e:
        config.logger.error(f"Failed to get message IDs for mention in chat {chat_id}: {e}")
        return None

    for msg_id in reversed(msg_ids[-MENTION_SCAN_LIMIT:]):
        if msg_id >= before_msg_id:
            continue
        try:
            m = bot.rpc.get_message(accid, msg_id)
        except Exception:
            continue
        if m.from_id == contact_id and not getattr(m, "is_info", False):
            return msg_id
    return None


def _quoted_sender(bot, accid, msg) -> int | None:
    quote = getattr(msg, "quote", None)
    if not quote or not isinstance(quote, dict) or not quote.get("message_id"):
        return None
    try:
        return bot.rpc.get_message(accid, quote["message_id"]).from_id
    except Exception:
        return None


def _is_group(bot, accid, chat_id: int) -> bool:
    try:
        chat = bot.rpc.get_basic_chat_info(accid, chat_id)
        chat_type = chat.get("chat_type") if isinstance(chat, dict) else getattr(chat, "chat_type", "Single")
    except Exception as e:
        config.logger.error(f"Failed to get chat info for mention in chat {chat_id}: {e}")
        return False
    return str(chat_type) == "Group"


def handle_mention(bot, accid, msg) -> bool:
    """Re-send "@Nick ..." as a reply to Nick's last message. True if it was sent."""
    text = (msg.text or "").strip()
    nick = parse_mention(text)
    if not nick:
        return False
    if msg.from_id <= 9 or msg.from_id == config.DC_CONTACT_ID_SELF:
        return False
    if not _is_group(bot, accid, msg.chat_id):
        return False

    key = (msg.chat_id, msg.from_id)
    now = time.time()
    if now - state._mention_anti_spam.get(key, 0) < config.MENTION_COOLDOWN_SECONDS:
        return False

    target = resolve_mention(bot, accid, msg.chat_id, nick, msg.from_id)
    if not target:
        return False
    target_id, _target_contact = target

    # Already a reply to the mentioned user: their client notifies them anyway
    if _quoted_sender(bot, accid, msg) == target_id:
        return False

    target_msg_id = find_last_message_from(bot, accid, msg.chat_id, target_id, msg.id)
    if not target_msg_id:
        return False

    try:
        sender = bot.rpc.get_contact(accid, msg.from_id)
        sender_name = sender.display_name or sender.name or sender.address or "User"
    except Exception:
        sender_name = "User"

    state._mention_anti_spam[key] = now
    try:
        bot.rpc.send_msg(accid, msg.chat_id, MsgData(
            text=text,
            override_sender_name=sender_name,
            quoted_message_id=target_msg_id,
        ))
    except Exception as e:
        config.logger.error(f"Failed to send mention reply in chat {msg.chat_id}: {e}")
        return False
    return True


def handle_mention_async(bot, accid, msg):
    """Run handle_mention off the event thread (scanning messages takes many RPC calls)."""
    if not config.MENTION_REPLY_ENABLED or not parse_mention(msg.text or ""):
        return

    def run():
        try:
            handle_mention(bot, accid, msg)
        except Exception as e:
            config.logger.error(f"Error handling mention in chat {msg.chat_id}: {e}")

    threading.Thread(target=run, daemon=True).start()
