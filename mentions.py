"""@mentions: a group message that starts with "@Nick" is sent again by the bot
as a reply to Nick's last message in the chat, so Nick gets a notification.

Delta Chat has no real mentions; a reply to someone's message is what makes
their client notify them (also in a muted chat). The copy goes out under the
author's name (override_sender_name, shown as "~Name"), like TG Bridge does.

When nobody in the chat has that nick but Username Bot is a member, the bot
asks Username Bot's web API for it and tells only the author, in private, the
invite link (https://i.delta.chat/#..., opens right in the app) and the short
link (https://deltachat.id/nick). It never writes to the mentioned
person: that would carry group messages outside the group and let anyone make
the bot message strangers.
"""

import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from deltachat2 import MsgData

import config
import dc_helpers
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


def _chat_members(bot, accid, chat_id: int) -> list | None:
    """Members of the chat as (contact_id, contact), the bot itself included."""
    try:
        member_ids = bot.rpc.get_chat_contacts(accid, chat_id)
    except Exception as e:
        config.logger.error(f"Failed to get chat contacts for mention in chat {chat_id}: {e}")
        return None

    members = []
    for contact_id in member_ids:
        if contact_id <= 9 and contact_id != config.DC_CONTACT_ID_SELF:
            continue
        try:
            members.append((contact_id, bot.rpc.get_contact(accid, contact_id)))
        except Exception:
            continue
    return members


def resolve_mention(members, nick: str):
    """(member, matched) for nick: member is the (contact_id, contact) it names or None;
    matched is False when nick fits no member at all (not even ambiguously).
    An exact match wins; otherwise a prefix match is used only when it is unique."""
    exact, prefix = [], []
    for contact_id, contact in members:
        rank = _match_rank(contact, nick)
        if rank == 2:
            exact.append((contact_id, contact))
        elif rank == 1:
            prefix.append((contact_id, contact))

    if len(exact) == 1:
        return exact[0], True
    if not exact and len(prefix) == 1:
        return prefix[0], True
    return None, bool(exact or prefix)


# --- Username Bot lookup for people who are not in the chat ---

USERNAME_RE = re.compile(r"^[A-Za-z0-9_-]{3,32}$")  # Username Bot's own rule
USERNAME_STOPWORDS = {"all", "here", "everyone", "channel", "admin", "admins"}
USERNAME_CACHE_SECONDS = 3600
OFFICIAL_INVITE_PREFIX = "https://i.delta.chat/#"  # Delta Chat apps open these without a browser
_username_cache: dict[str, tuple[float, dict | None]] = {}
_username_cache_lock = threading.Lock()


def has_username_bot(members) -> bool:
    """True if Username Bot is a member (matched by address, not by display name)."""
    return any(
        dc_helpers.is_bot_address(getattr(contact, "address", ""), config.USERNAME_BOT_ADDRS)
        for contact_id, contact in members
        if contact_id != config.DC_CONTACT_ID_SELF
    )


def lookup_username(nick: str) -> dict | None:
    """Username Bot's public record for nick ({username, type, display_name, url, invite_link}) or None.
    Found and not-found answers are cached for an hour; errors are not cached."""
    nick = nick.lower()
    if not config.USERNAME_LOOKUP_URL or not USERNAME_RE.match(nick) or nick in USERNAME_STOPWORDS:
        return None

    now = time.time()
    with _username_cache_lock:
        cached = _username_cache.get(nick)
        if cached and now - cached[0] < USERNAME_CACHE_SECONDS:
            return cached[1]

    url = f"{config.USERNAME_LOOKUP_URL.rstrip('/')}/api/username/{urllib.parse.quote(nick)}"
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": f"BouncerBot/{config.VERSION}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        result = data if isinstance(data, dict) and data.get("url") else None
    except urllib.error.HTTPError as e:
        if e.code != 404:
            config.logger.warning(f"Username lookup for {nick} failed: HTTP {e.code}")
            return None
        result = None
    except Exception as e:
        config.logger.warning(f"Username lookup for {nick} failed: {e}")
        return None

    with _username_cache_lock:
        if len(_username_cache) > 1000:
            for key in [k for k, (ts, _) in _username_cache.items() if now - ts >= USERNAME_CACHE_SECONDS]:
                _username_cache.pop(key, None)
        _username_cache[nick] = (now, result)
    return result


def _format_username_hint(nick: str, record: dict) -> str:
    username = record.get("username") or nick.lower()
    name = (record.get("display_name") or "").strip()
    who = f"**{name}** (@{username})" if name and name.lower() != username else f"**@{username}**"
    kind = record.get("type")
    if kind == "group":
        what = f"@{username} is not a member here, it is the username of the group {who}"
        tail = "Open the link to join it."
    elif kind == "channel":
        what = f"@{username} is not a member here, it is the username of the channel {who}"
        tail = "Open the link to join it."
    else:
        what = f"{who} is not in this chat"
        tail = "They won't see your message here: write to them directly, or send them this chat's invite link."
    invite_link = record.get("invite_link") or ""
    if not invite_link.startswith(OFFICIAL_INVITE_PREFIX):
        # Older Username Bot without invite_link: the short link redirects to the invite
        return f"🔎 {what}. Found via Username Bot:\n{record['url']}\n\n{tail}"
    # i.delta.chat opens right in the app; the short link is nicer to share
    return f"🔎 {what}. Found via Username Bot:\n{invite_link}\n\nShort link: {record['url']}\n\n{tail}"


def _send_username_hint(bot, accid, msg, nick: str, members, cooldown_key, now: float) -> bool:
    """Tell the author, privately, how to reach a nick that is not in the chat."""
    if not has_username_bot(members) or not USERNAME_RE.match(nick) or nick.lower() in USERNAME_STOPWORDS:
        return False
    state._mention_anti_spam[cooldown_key] = now
    record = lookup_username(nick)
    if not record:
        return False
    try:
        private_chat_id = bot.rpc.create_chat_by_contact_id(accid, msg.from_id)
        dc_helpers.queue_msg(bot, accid, private_chat_id, MsgData(text=_format_username_hint(nick, record)))
    except Exception as e:
        config.logger.error(f"Failed to send username hint to {msg.from_id}: {e}")
        return False
    return True


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

    members = _chat_members(bot, accid, msg.chat_id)
    if members is None:
        return False
    target, matched = resolve_mention(members, nick)
    if not target:
        if matched:
            return False  # ambiguous
        # Nobody here by that name: maybe Username Bot knows them
        return _send_username_hint(bot, accid, msg, nick, members, key, now)
    target_id, _target_contact = target
    if target_id in (msg.from_id, config.DC_CONTACT_ID_SELF):
        return False

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
        dc_helpers.queue_msg(bot, accid, msg.chat_id, MsgData(
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
