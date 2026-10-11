"""Group FAQ: /faq, /save, /remove, and answering saved topics.

Works like the standalone FAQ Bot so groups can switch without relearning
anything: the same three commands, the {faq} and {name} placeholders, and a
message that is exactly a topic name gets that topic's answer. Differences:
- only a topic's author or the bot admin can overwrite or remove it (in public
  groups anyone could otherwise wipe the rules);
- the answer is copied into our database (text, plus its file under
  data/faq/), because the original message is deleted from the device after
  36 hours (delete_device_after);
- topics can't start with "/" (bot commands) and the same topic is answered at
  most once per FAQ_COOLDOWN_SECONDS in a chat.

While a FAQ Bot is a member of the group we don't answer, so the group doesn't
get every answer twice, but we keep mirroring /save and /remove silently
("shadow mode"). Once the FAQ Bot is removed, the topics saved in the meantime
are already here. A command addressed to us explicitly (/faq@bouncer) is always
answered.
"""
import os
import shutil
import threading
import time
import uuid

from deltachat2 import events

import config
import database
import dc_helpers
import state

SAVE_USAGE = (
    "Reply to a message with `/save <topic>` to save it as the answer to that topic.\n"
    "The answer may contain:\n"
    "{faq} — replaced by the list of topics\n"
    "{name} — replaced by the name of whoever asked (or of the person the question replied to)"
)
REMOVE_USAGE = "Usage: `/remove <topic>`"

# view_type of a message without an attachment
_TEXT_VIEWTYPES = (None, "", "Text")
# Message kinds that can't be saved as an answer (nothing to resend)
_UNSAVABLE_VIEWTYPES = ("Call",)


# ── Topics ──

def display_tag(tag: str) -> str:
    """Topic as shown to users: surrounding and repeated whitespace removed."""
    return " ".join(tag.split())


def normalize_tag(tag: str) -> str:
    """Lookup key: "Rules", " rules " and "RULES" are the same topic."""
    return display_tag(tag).casefold()


def validate_tag(tag: str) -> str | None:
    """Return an error message, or None if the topic name is usable."""
    tag = display_tag(tag)
    if not tag:
        return "Please name the topic."
    if tag.startswith("/"):
        # A "/rules" topic would be parsed as a command and never reach the
        # topic lookup (and could shadow a real command).
        return "Topic names can't start with \"/\", that is reserved for bot commands."
    if len(tag) > config.FAQ_MAX_TAG_LENGTH:
        return f"Topic names can be at most {config.FAQ_MAX_TAG_LENGTH} characters long."
    return None


def topics_text(chat_id: int) -> str:
    topics = database.get_faq_topics(chat_id)
    if not topics:
        return "(no topics yet)"
    return "\n".join(f"• {t}" for t in topics)


def render_answer(text: str, chat_id: int, name: str) -> str:
    # {name} first: a topic name that happens to contain "{name}" stays literal
    text = text.replace("{name}", name)
    if "{faq}" in text:
        text = text.replace("{faq}", topics_text(chat_id))
    return text


# ── FAQ Bot detection ──

def _is_faq_bot_contact(contact) -> bool:
    # Require the bot flag: a human renaming themselves "FAQ Bot" must not be
    # able to switch our FAQ off.
    if getattr(contact, "is_bot", False) is not True:
        return False
    for attr in ("display_name", "name", "auth_name"):
        value = getattr(contact, attr, None)
        if isinstance(value, str) and "faq" in value.lower():
            return True
    return False


def faq_bot_in_chat(bot, accid, chat_id) -> bool:
    """True if a FAQ Bot is a member of the chat. Cached for FAQ_BOT_CHECK_TTL,
    so after the FAQ Bot is removed we take over within a minute."""
    now = time.time()
    cached = state._faq_bot_cache.get(chat_id)
    if cached and now - cached[0] < config.FAQ_BOT_CHECK_TTL:
        return cached[1]

    present = False
    try:
        contact_ids = bot.rpc.get_chat_contacts(accid, chat_id)
    except Exception as e:
        config.logger.warning(f"FAQ: could not list members of chat {chat_id}: {e}")
        return False  # not cached: try again next time
    for contact_id in contact_ids:
        # ids <= 9 are special (1 = ourselves)
        if contact_id <= 9:
            continue
        try:
            contact = bot.rpc.get_contact(accid, contact_id)
        except Exception:
            continue
        if _is_faq_bot_contact(contact):
            present = True
            break
    state._faq_bot_cache[chat_id] = (now, present)
    return present


def _explicitly_addressed(msg) -> bool:
    """/faq@bouncer. Commands addressed to another bot never reach our handlers
    (bot.setup_custom_command_parser clears them), so any @ here means us."""
    parts = (msg.text or "").split(maxsplit=1)
    return bool(parts) and "@" in parts[0]


def _defer_to_faq_bot(bot, accid, msg) -> bool:
    return not _explicitly_addressed(msg) and faq_bot_in_chat(bot, accid, msg.chat_id)


# ── Helpers ──

def _quoted_msg_id(msg) -> int | None:
    quote = getattr(msg, "quote", None)
    if isinstance(quote, dict):
        qid = quote.get("message_id") or quote.get("messageId")
        if isinstance(qid, int):
            return qid
    return None


def _has_attachment(msg) -> bool:
    file_path = getattr(msg, "file", None)
    view_type = getattr(msg, "view_type", None)
    return (isinstance(file_path, str) and bool(file_path)) or view_type not in _TEXT_VIEWTYPES


def _contact_name(bot, accid, contact_id) -> str:
    try:
        contact = bot.rpc.get_contact(accid, contact_id)
        for attr in ("display_name", "name", "address"):
            value = getattr(contact, attr, None)
            if isinstance(value, str) and value.strip():
                return value.strip()
    except Exception:
        pass
    return "friend"


def _can_edit(bot, accid, contact_id, entry) -> bool:
    return entry.get("author_id") == contact_id or dc_helpers._is_dc_admin(bot, accid, contact_id)


def _throttled(bot, accid, msg, key: str) -> bool:
    """True if this topic (or the /faq list) was already answered in this chat
    less than FAQ_COOLDOWN_SECONDS ago. The admin is never throttled."""
    now = time.time()
    k = (msg.chat_id, key)
    if now - state._faq_anti_spam.get(k, 0) < config.FAQ_COOLDOWN_SECONDS:
        if not dc_helpers._is_dc_admin(bot, accid, msg.from_id):
            return True
    state._faq_anti_spam[k] = now
    return False


def _store_file(chat_id: int, src_path: str, filename: str | None) -> str:
    """Copy an attachment into data/faq/<chat>/<random>/<original name>. The
    random directory keeps the original file name (Delta Chat shows the
    basename of the sent file) without collisions."""
    name = os.path.basename(filename or "") or os.path.basename(src_path) or "file"
    dest_dir = os.path.join(state.FAQ_MEDIA_DIR, str(chat_id), uuid.uuid4().hex)
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, name)
    shutil.copyfile(src_path, dest)
    return dest


def _delete_file(path: str | None):
    """Delete a stored answer file with its random directory. Refuses anything
    outside FAQ_MEDIA_DIR, in case the database holds a foreign path."""
    if not path:
        return
    media_root = os.path.realpath(state.FAQ_MEDIA_DIR)
    entry_dir = os.path.realpath(os.path.dirname(path))
    if os.path.dirname(os.path.dirname(entry_dir)) != media_root:
        config.logger.warning(f"FAQ: not deleting {path}, it is outside {media_root}")
        return
    shutil.rmtree(entry_dir, ignore_errors=True)


# ── Answering ──

def send_answer(bot, accid, entry, trigger_msg):
    """Answer with a saved topic. If the question was a reply to someone
    ("rules" sent as a reply to a newcomer), answer that message instead, and
    {name} is that person."""
    reply_to = trigger_msg.id
    asker_id = trigger_msg.from_id
    quoted_id = _quoted_msg_id(trigger_msg)
    if quoted_id:
        try:
            quoted = bot.rpc.get_message(accid, quoted_id)
            reply_to = quoted_id
            asker_id = quoted.from_id
        except Exception:
            pass  # quoted message no longer on the device: answer the question itself

    chat_id = trigger_msg.chat_id
    text = render_answer(entry.get("text") or "", chat_id, _contact_name(bot, accid, asker_id))
    file_path = entry.get("file_path")
    if file_path and not os.path.exists(file_path):
        config.logger.warning(f"FAQ: file of topic {entry.get('tag')!r} in chat {chat_id} is missing: {file_path}")
        file_path = None
        if not text:
            text = "⚠️ The file of this answer is missing. Please save the topic again."
    dc_helpers._send(bot, accid, chat_id, text, reply_to_id=reply_to,
                     file=file_path, viewtype=entry.get("viewtype"))


@config.dc_cli.on(events.NewMessage(is_info=False))
def faq_topic_handler(bot, accid, event):
    """A message that is exactly a topic name gets the topic's answer."""
    msg = event.msg
    text = (msg.text or "").strip()
    if not text or text.startswith("/") or len(text) > config.FAQ_MAX_TAG_LENGTH:
        return
    if _has_attachment(msg):
        return  # an image captioned "rules" is not a question
    tag = normalize_tag(text)
    entry = database.get_faq_entry(msg.chat_id, tag)
    if not entry:
        return
    if faq_bot_in_chat(bot, accid, msg.chat_id) or _throttled(bot, accid, msg, tag):
        return
    send_answer(bot, accid, entry, msg)


# ── Commands ──

@config.dc_cli.on(events.NewMessage(command="/faq"))
def faq_command(bot, accid, event):
    """/faq lists the topics, /faq <topic> shows one answer."""
    msg = event.msg
    if _defer_to_faq_bot(bot, accid, msg):
        return

    topic = (event.payload or "").strip()
    if topic:
        tag = normalize_tag(topic)
        entry = database.get_faq_entry(msg.chat_id, tag)
        if not entry:
            dc_helpers._send(bot, accid, msg.chat_id,
                             f"❌ There is no FAQ topic \"{display_tag(topic)}\". Send /faq for the list.",
                             reply_to_id=msg.id)
        elif not _throttled(bot, accid, msg, tag):
            send_answer(bot, accid, entry, msg)
        return

    if _throttled(bot, accid, msg, "/faq"):
        return
    if not database.get_faq_topics(msg.chat_id):
        dc_helpers._send(bot, accid, msg.chat_id,
                         "📖 No FAQ topics in this chat yet.\n\n" + SAVE_USAGE)
        return
    dc_helpers._send(bot, accid, msg.chat_id,
                     "📖 **FAQ** — send a topic name to get its answer:\n\n" + topics_text(msg.chat_id))


def _save_worker(bot, accid, msg, quoted, tag, shown_tag, shadow):
    """Copy the quoted message into the FAQ. May block up to ~30s downloading
    an attachment, so attachments run it in a thread."""
    def fail(text):
        if not shadow:
            dc_helpers._send(bot, accid, msg.chat_id, f"❌ {text}", reply_to_id=msg.id)

    with_file = _has_attachment(quoted)
    try:
        text = quoted.text or ""
        file_path = viewtype = None
        if with_file:
            info = dc_helpers._get_msg_file_info(bot, accid, quoted)
            if not info:
                fail("Could not download the attachment of the quoted message.")
                return
            size = os.path.getsize(info["path"])
            if size > config.FAQ_MAX_FILE_BYTES:
                fail(f"The attachment is too large ({size / 1048576:.1f} MB, "
                     f"the limit is {config.FAQ_MAX_FILE_BYTES // 1048576} MB).")
                return
            file_path = _store_file(msg.chat_id, info["path"], info.get("filename"))
            viewtype = quoted.view_type
        if not text and not file_path:
            fail("The quoted message is empty.")
            return

        old = database.save_faq_entry(msg.chat_id, tag, shown_tag, text, file_path, viewtype, msg.from_id)
        if old and old.get("file_path") != file_path:
            _delete_file(old.get("file_path"))
    except Exception as e:
        config.logger.error(f"FAQ: saving topic {tag!r} in chat {msg.chat_id} failed: {e}", exc_info=True)
        if with_file and not shadow:
            dc_helpers._react(bot, accid, msg.id, "❌")
        fail("Could not save the topic, please try again.")
        return

    if shadow:
        config.logger.info(f"FAQ: mirrored topic {tag!r} in chat {msg.chat_id} (FAQ Bot present)")
        return
    if with_file:
        dc_helpers._react(bot, accid, msg.id, "☑️")
    verb = "Updated" if old else "Saved"
    dc_helpers._send(bot, accid, msg.chat_id,
                     f"✅ {verb} FAQ topic **{shown_tag}**. Send `{shown_tag}` in this chat to get the answer.",
                     reply_to_id=msg.id)


@config.dc_cli.on(events.NewMessage(command="/save"))
def save_command(bot, accid, event):
    msg = event.msg
    shadow = _defer_to_faq_bot(bot, accid, msg)

    def reply(text):
        if not shadow:
            dc_helpers._send(bot, accid, msg.chat_id, text, reply_to_id=msg.id)

    raw_tag = event.payload or ""
    error = validate_tag(raw_tag)
    quoted_id = _quoted_msg_id(msg)
    if error or not quoted_id:
        reply((f"❌ {error}\n\n" if error and quoted_id else "") + SAVE_USAGE)
        return
    try:
        quoted = bot.rpc.get_message(accid, quoted_id)
    except Exception as e:
        config.logger.warning(f"FAQ: could not load quoted message {quoted_id}: {e}")
        reply("❌ Could not read the quoted message.")
        return
    if getattr(quoted, "view_type", None) in _UNSAVABLE_VIEWTYPES:
        reply("❌ This kind of message can't be saved as an answer.")
        return

    tag = normalize_tag(raw_tag)
    shown_tag = display_tag(raw_tag)
    # In shadow mode we mirror the FAQ Bot, which lets anyone overwrite.
    existing = database.get_faq_entry(msg.chat_id, tag)
    if existing and not shadow and not _can_edit(bot, accid, msg.from_id, existing):
        reply(f"❌ Topic **{existing['display_tag']}** was saved by someone else. "
              f"Only its author or the bot admin can change it.")
        return

    if _has_attachment(quoted):
        if not shadow:
            dc_helpers._react(bot, accid, msg.id, "⏳")
        threading.Thread(
            target=_save_worker,
            args=(bot, accid, msg, quoted, tag, shown_tag, shadow),
            daemon=True,
        ).start()
    else:
        _save_worker(bot, accid, msg, quoted, tag, shown_tag, shadow)


@config.dc_cli.on(events.NewMessage(command="/remove"))
def remove_command(bot, accid, event):
    msg = event.msg
    shadow = _defer_to_faq_bot(bot, accid, msg)

    def reply(text):
        if not shadow:
            dc_helpers._send(bot, accid, msg.chat_id, text, reply_to_id=msg.id)

    raw_tag = event.payload or ""
    if not display_tag(raw_tag):
        reply(REMOVE_USAGE)
        return
    tag = normalize_tag(raw_tag)
    entry = database.get_faq_entry(msg.chat_id, tag)
    if not entry:
        reply(f"❌ There is no FAQ topic \"{display_tag(raw_tag)}\". Send /faq for the list.")
        return
    if not shadow and not _can_edit(bot, accid, msg.from_id, entry):
        reply(f"❌ Topic **{entry['display_tag']}** was saved by someone else. "
              f"Only its author or the bot admin can remove it.")
        return

    database.delete_faq_entry(msg.chat_id, tag)
    _delete_file(entry.get("file_path"))
    if shadow:
        config.logger.info(f"FAQ: mirrored removal of topic {tag!r} in chat {msg.chat_id} (FAQ Bot present)")
        return
    reply(f"🗑️ Removed FAQ topic **{entry['display_tag']}**.")
