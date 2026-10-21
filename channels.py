"""Chat/channel catalog: post ingestion into the web-preview cache
(with ActivityPub delivery queuing), catalog backfill, and the
/chats, /dchannels, /chatadd, /chatremove, /private, /dchanneladd,
/dchannelremove command handlers. Submissions by non-admins go through
catalog_review (pending review, auto-approval, reports)."""
import filecmp
import os
import re
import shutil
import threading
import time

from deltachat2 import events

import activitypub
import catalog_review
import config
import database
import dc_helpers
import state
import stickers
import web.routes

def _ingest_channel_post(bot, accid, msg, catalog_channel: dict, token: str):
    """Save a channel post and its media attachment into the catalog cache for web preview."""
    try:
        raw_id = getattr(msg, "id", None) if not isinstance(msg, dict) else msg.get("id")
        msg_id = raw_id if isinstance(raw_id, int) else None
        if not msg_id:
            return

        is_info = getattr(msg, "is_info", False) if not isinstance(msg, dict) else msg.get("is_info", False)
        if is_info:
            return

        chat_id = catalog_channel.get("chat_id")
        if not chat_id:
            return

        # Safety net: if the rename/avatar info message was missed, catch up on the next post.
        sync_channel_profile(bot, accid, chat_id)

        text = getattr(msg, "text", None) if not isinstance(msg, dict) else msg.get("text")
        text = (text or "").strip()

        raw_ts = getattr(msg, "timestamp", None) if not isinstance(msg, dict) else msg.get("timestamp")
        if isinstance(raw_ts, (int, float)) and raw_ts > 0:
            msg_ts = raw_ts / 1000.0 if raw_ts > 1e11 else float(raw_ts)
        else:
            msg_ts = time.time()

        from_name = ""
        from_id = getattr(msg, "from_id", None) if not isinstance(msg, dict) else msg.get("from_id")
        if from_id and from_id != 1 and bot and hasattr(bot, "rpc"):
            try:
                c = bot.rpc.get_contact(accid, from_id)
                from_name = getattr(c, "name", None) or getattr(c, "display_name", None) or ""
            except Exception:
                pass

        media_type = None
        media_filename = None
        media_path = None

        file_info = dc_helpers._get_msg_file_info(bot, accid, msg)
        if file_info and file_info.get("path") and os.path.exists(file_info["path"]):
            src_path = file_info["path"]
            orig_filename = file_info.get("filename") or os.path.basename(src_path)
            safe_filename = re.sub(r'[^a-zA-Z0-9_.-]', '_', orig_filename)

            raw_vt = (getattr(msg, "view_type", "") or "")
            raw_vt_str = str(raw_vt).lower()
            ext = os.path.splitext(safe_filename)[1].lower()

            if ext in ('.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp', '.svg') or 'image' in raw_vt_str:
                media_type = 'image'
            elif ext in ('.mp4', '.mkv', '.webm', '.mov', '.avi') or 'video' in raw_vt_str:
                media_type = 'video'
            elif ext in ('.mp3', '.ogg', '.opus', '.m4a', '.wav', '.flac', '.aac') or any(k in raw_vt_str for k in ('audio', 'voice')):
                media_type = 'audio'
            else:
                media_type = 'file'

            dest_dir = os.path.join(state.CHANNEL_MEDIA_DIR, token)
            os.makedirs(dest_dir, exist_ok=True)

            if media_type == 'image' and ext not in ('.gif', '.svg'):
                stem = os.path.splitext(safe_filename)[0]
                webp_filename = f"{stem}.webp"
                webp_dest_file = f"{msg_id}_{webp_filename}"
                webp_dest_path = os.path.join(dest_dir, webp_dest_file)
                if os.path.exists(webp_dest_path) or stickers._optimize_image_to_webp(src_path, webp_dest_path):
                    media_path = webp_dest_path
                    media_filename = webp_filename
                else:
                    dest_file = f"{msg_id}_{safe_filename}"
                    dest_path = os.path.join(dest_dir, dest_file)
                    try:
                        if not os.path.exists(dest_path):
                            shutil.copy2(src_path, dest_path)
                        media_path = dest_path
                        media_filename = safe_filename
                    except Exception as e:
                        config.logger.warning(f"Failed to copy media for channel post {msg_id}: {e}")
            else:
                dest_file = f"{msg_id}_{safe_filename}"
                dest_path = os.path.join(dest_dir, dest_file)
                try:
                    if not os.path.exists(dest_path):
                        shutil.copy2(src_path, dest_path)
                    media_path = dest_path
                    media_filename = safe_filename
                except Exception as e:
                    config.logger.warning(f"Failed to copy media for channel post {msg_id}: {e}")

        if media_path or file_info:
            text = config.DC_FALLBACK_PATTERN.sub('', text).strip()

        if text or media_path:
            database.save_channel_post(
                chat_id=chat_id,
                msg_id=msg_id,
                from_name=from_name,
                text=text,
                media_type=media_type,
                media_filename=media_filename,
                media_path=media_path,
                timestamp=msg_ts
            )
            database.prune_channel_posts(chat_id, 100)
            web.routes.invalidate_channel_cache(chat_id=chat_id, token=token)
            # ── ActivityPub: queue post delivery to Fediverse followers ──
            try:
                followers_count = database.get_ap_followers_count(token)
                if followers_count > 0 and database.channel_web_visible(database.get_catalog_channel_by_chat_id(chat_id)):
                    base_url = (database.get_config("base_url") or os.getenv("BASE_URL") or "").strip().rstrip('/')
                    if base_url:
                        post_data = {
                            "msg_id": msg_id,
                            "chat_id": chat_id,
                            "text": text,
                            "from_name": from_name,
                            "media_type": media_type,
                            "media_filename": media_filename,
                            "timestamp": msg_ts,
                        }
                        activitypub.queue_post_delivery(token, post_data, base_url)
            except Exception as e:
                config.logger.warning(f"AP delivery queue error: {e}")
    except Exception as e:
        config.logger.error(f"Error ingesting channel post: {e}")


def _chat_field(info, *names):
    """Read the first non-empty field from an RPC object that may be a dict (camelCase) or an attr object."""
    for n in names:
        val = info.get(n) if isinstance(info, dict) else getattr(info, n, None)
        if val:
            return val
    return None


def sync_channel_profile(bot, accid, chat_id: int) -> bool:
    """Pull the channel's current name and avatar from core into the catalog.

    The catalog stores the name at join time and the web preview caches the avatar
    forever, so without this a rename / new picture in Delta Chat never reached
    the site, RSS or the Fediverse actor. Returns True if anything changed.
    """
    channel = database.get_catalog_channel_by_chat_id(chat_id)
    if not channel:
        return False
    try:
        info = bot.rpc.get_basic_chat_info(accid, chat_id)
    except Exception as e:
        config.logger.debug(f"sync_channel_profile: cannot read chat {chat_id}: {e}")
        return False

    changed = False
    token = channel.get("token")

    new_name = _chat_field(info, "name")
    if isinstance(new_name, str) and new_name.strip() and new_name != channel.get("name"):
        database.update_catalog_channel_name(chat_id, new_name)
        config.logger.info(f"Channel {chat_id} renamed: '{channel.get('name')}' -> '{new_name}'")
        changed = True

    if token:
        cache_path = os.path.join(state.CHANNEL_MEDIA_DIR, token, "avatar.png")
        new_img = _chat_field(info, "profile_image", "profileImage")
        try:
            if new_img and os.path.exists(new_img):
                # Compare bytes, not paths: core reuses the blob dir and a changed
                # picture may keep a similar name; an unchanged one must not bust the cache.
                if not os.path.exists(cache_path) or not filecmp.cmp(new_img, cache_path, shallow=False):
                    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
                    shutil.copy2(new_img, cache_path)
                    config.logger.info(f"Channel {chat_id} avatar updated")
                    changed = True
            elif not new_img and os.path.exists(cache_path):
                # Avatar was removed: drop the stale copy so the default is served again.
                os.remove(cache_path)
                config.logger.info(f"Channel {chat_id} avatar removed")
                changed = True
        except OSError as e:
            config.logger.warning(f"sync_channel_profile: avatar cache error for channel {chat_id}: {e}")

    if changed:
        web.routes.invalidate_channel_cache(chat_id=chat_id, token=token)
    return changed


def _backfill_existing_catalog_channels(bot, accid):
    """Backfill recent messages from core for existing catalog channels with empty cache."""
    try:
        time.sleep(3)
        channels = database.get_all_catalog_channels()
        for ch in channels:
            chat_id = ch.get("chat_id")
            token = ch.get("token")
            if not chat_id or not token:
                continue
            existing = database.get_channel_posts(chat_id, limit=1)
            if not existing:
                try:
                    chat_msgs = bot.rpc.get_message_ids(accid, chat_id, False, False)
                    if chat_msgs:
                        config.logger.info(f"Backfilling {len(chat_msgs[-10:])} messages for existing channel '{ch.get('name')}' (chat {chat_id})")
                        for mid in chat_msgs[-10:]:
                            if isinstance(mid, int) and mid > 0:
                                try:
                                    m = bot.rpc.get_message(accid, mid)
                                    if m and not getattr(m, "is_info", False):
                                        _ingest_channel_post(bot, accid, m, ch, token)
                                except Exception as me:
                                    config.logger.warning(f"Failed to backfill msg {mid} for channel {chat_id}: {me}")
                except Exception as ce:
                    config.logger.warning(f"Failed to get messages for channel {chat_id}: {ce}")
    except Exception as e:
        config.logger.warning(f"Error in _backfill_existing_catalog_channels: {e}")


def short_description(text: str, limit: int) -> str:
    """One-line summary of a description for the catalog lists.

    Chat descriptions are often several lines (rules, links); the lists show only
    the first non-empty line, cut at a word boundary. The full text stays stored.
    """
    first = next((line for line in (text or "").splitlines() if line.strip()), "")
    first = " ".join(first.split())
    if len(first) <= limit:
        return first
    cut = first[:limit].rsplit(" ", 1)[0] if " " in first[:limit] else first[:limit]
    return cut.rstrip(" ,.;:-—") + "…"


@config.dc_cli.on(events.NewMessage(command="/chats"))
def chats_command(bot, accid, event):
    msg = event.msg
    catalog_chats = database.get_all_catalog_chats(approved_only=True)
    if not catalog_chats:
        dc_helpers._send(bot, accid, msg.chat_id, "ℹ️ The chat catalog is empty.")
        return

    lines = []
    for ch in catalog_chats:
        private_str = "🔐 " if ch['is_private'] else ""
        desc = short_description(ch['description'], 100)
        desc_str = f" {desc}" if desc else ""
        lines.append(f"/chat{ch['id']} {private_str}**{ch['name']}**{desc_str} 👥 {ch['member_count']}")

    reply = "\n".join(lines)
    dc_helpers._send(bot, accid, msg.chat_id, reply)

@config.dc_cli.on(events.NewMessage(command="/dchannels"))
def dchannels_command(bot, accid, event):
    msg = event.msg
    try:
        chat = bot.rpc.get_basic_chat_info(accid, msg.chat_id)
        chat_type = chat.get('chat_type', 'Single') if isinstance(chat, dict) else getattr(chat, 'chat_type', 'Single')
    except Exception:
        chat_type = 'Single'
    is_private_chat = (str(chat_type) == "Single")
    is_admin = dc_helpers._is_dc_admin(bot, accid, msg.from_id)
    is_admin_dm = (is_admin and is_private_chat)

    if is_admin_dm:
        catalog_channels = database.get_all_catalog_channels(include_deleted=False, public_only=False)
    else:
        catalog_channels = database.get_all_catalog_channels(include_deleted=False, public_only=True)

    if not catalog_channels:
        dc_helpers._send(bot, accid, msg.chat_id, "ℹ️ The channel catalog is empty.")
        return

    base_url = database.get_config("base_url") or os.getenv("BASE_URL") or ""
    entries = []
    for ch in catalog_channels:
        is_pub = bool(ch.get('is_public', 1))
        if is_pub:
            entry_parts = [f"/dchannel{ch['id']} **{ch['name']}**"]
        elif ch.get('status') == 'pending':
            entry_parts = [f"/dchannel{ch['id']} ⏳ **{ch['name']}** [Pending review]"]
        else:
            entry_parts = [f"/dchannel{ch['id']} 🔒 **{ch['name']}** [Unlisted]"]

        desc = short_description(ch.get('description'), 200)
        if desc:
            entry_parts.append(desc)
        token = ch.get('token')
        if base_url and token and database.channel_web_visible(ch):
            entry_parts.append(f"🌐 Preview: {base_url.rstrip('/')}/c/{token}")

        if is_admin_dm and ch.get('status') == 'pending':
            entry_parts.append(f"Review: /dchannelapprove{ch['id']}  /dchannelreject{ch['id']}")
        elif is_admin_dm and not is_pub:
            entry_parts.append(f"Publish: /dchannelpub{ch['id']}on")

        entries.append("\n".join(entry_parts))

    reply = "\n\n".join(entries)
    dc_helpers._send(bot, accid, msg.chat_id, reply)

@config.dc_cli.on(events.NewMessage(command="/chatadd"))
def chatadd_command(bot, accid, event):
    msg = event.msg
    is_admin = dc_helpers._is_dc_admin(bot, accid, msg.from_id)
    if not is_admin and not catalog_review.submissions_enabled():
        dc_helpers._send(bot, accid, msg.chat_id, "❌ Only the bot administrator can use this command.")
        return

    try:
        chat = bot.rpc.get_basic_chat_info(accid, msg.chat_id)
        chat_type = chat.get('chat_type', 'Single') if isinstance(chat, dict) else getattr(chat, 'chat_type', 'Single')
    except Exception as e:
        config.logger.error(f"Failed to get chat info: {e}")
        return

    GROUP_TYPES = {"Group", "Mailinglist", "OutBroadcast", "InBroadcast"}
    # Users submit groups they are in; channels go through /dchanneladd <link>
    if str(chat_type) not in (GROUP_TYPES if is_admin else {"Group"}):
        dc_helpers._send(bot, accid, msg.chat_id, "❌ Only group chats can be added to the catalog.")
        return

    description = event.payload.strip() if event.payload else ""
    if not description:
        try:
            description = bot.rpc.get_chat_description(accid, msg.chat_id) or ""
            description = description.strip()
        except Exception as e:
            config.logger.warning(f"Failed to get chat description from core: {e}")
            description = ""

    chat_name = chat.get('name') if isinstance(chat, dict) else getattr(chat, 'name', 'Group')

    try:
        contacts = bot.rpc.get_chat_contacts(accid, msg.chat_id)
        member_count = sum(1 for c in contacts if c != 1)
    except Exception:
        member_count = 0

    if not is_admin:
        catalog_review.submit_chat(bot, accid, msg, chat_name, description, member_count)
        return

    database.add_catalog_chat(msg.chat_id, chat_name, description, member_count)
    dc_helpers._send(bot, accid, msg.chat_id, f"✅ Chat **{chat_name}** has been added to the catalog.")

@config.dc_cli.on(events.NewMessage(command="/chatremove"))
def chatremove_command(bot, accid, event):
    msg = event.msg
    catalog_chat = database.get_catalog_chat_by_chat_id(msg.chat_id)
    is_admin = dc_helpers._is_dc_admin(bot, accid, msg.from_id)
    # Any member may take a user-submitted group out of the catalog: the command is
    # sent inside the group, and removing is the safe direction.
    if not is_admin and not (catalog_chat and catalog_chat.get('submitted_by')):
        dc_helpers._send(bot, accid, msg.chat_id, "❌ Only the bot administrator can use this command.")
        return

    database.remove_catalog_chat(msg.chat_id)
    if catalog_chat and catalog_chat.get('submitted_by'):
        database.decide_catalog_submission(catalog_review.KIND_CHAT, catalog_chat['id'], "withdrawn")
        if not is_admin:
            catalog_review._notify_admin(
                bot, accid, f"ℹ️ Chat **{catalog_chat['name']}** (#{catalog_chat['id']}) was removed from the catalog by a member.")
    dc_helpers._send(bot, accid, msg.chat_id, "✅ Chat has been removed from the catalog.")

@config.dc_cli.on(events.NewMessage(command="/private"))
def private_command(bot, accid, event):
    msg = event.msg
    catalog_chat = database.get_catalog_chat_by_chat_id(msg.chat_id)
    # Members decide for groups they submitted themselves (sent inside the group)
    if not dc_helpers._is_dc_admin(bot, accid, msg.from_id) and not (catalog_chat and catalog_chat.get('submitted_by')):
        dc_helpers._send(bot, accid, msg.chat_id, "❌ Only the bot administrator can use this command.")
        return

    arg = event.payload.strip().lower() if event.payload else ""
    if arg not in ("on", "off"):
        dc_helpers._send(bot, accid, msg.chat_id, "Usage: /private on or /private off")
        return

    if not catalog_chat:
        dc_helpers._send(bot, accid, msg.chat_id, "❌ Please add this chat to the catalog first using /chatadd.")
        return

    is_private = 1 if arg == "on" else 0
    database.update_catalog_chat_privacy(msg.chat_id, is_private)
    status_str = "private (by request)" if is_private else "public"
    dc_helpers._send(bot, accid, msg.chat_id, f"✅ Chat is now {status_str}.")

# How long /dchanneladd waits for the inviter's device before reporting "pending",
# and how long it keeps watching in the background after that.
JOIN_QUICK_WAIT = 30
JOIN_BACKGROUND_WAIT = 30 * 60


def _self_in_chat(bot, accid, chat_id: int) -> bool:
    """True once the bot is a member of the chat, i.e. the securejoin handshake finished."""
    try:
        full = bot.rpc.get_full_chat_by_id(accid, chat_id)
    except Exception:
        return False
    return bool(_chat_field(full, "self_in_group", "selfInGroup"))


def _wait_for_join(bot, accid, chat_id: int, timeout: float, step: float) -> bool:
    deadline = time.time() + timeout
    while True:
        if _self_in_chat(bot, accid, chat_id):
            return True
        if time.time() >= deadline:
            return False
        time.sleep(step)


def _backfill_channel(bot, accid, chat_id: int, token: str):
    """Cache the last messages core already has in the chat.

    A channel join delivers up to 10 recent posts; they may arrive before the catalog
    row exists or while the bot is busy, so pull them from core once the join is done.
    Ingestion is idempotent (msg_id is unique), so overlap with the live handler is fine.
    """
    try:
        chat_msgs = bot.rpc.get_message_ids(accid, chat_id, False, False)
    except Exception as e:
        config.logger.warning(f"Failed to backfill messages for channel {chat_id}: {e}")
        return
    for mid in chat_msgs[-10:]:
        if isinstance(mid, int) and mid > 0:
            try:
                m = bot.rpc.get_message(accid, mid)
                if m and not getattr(m, "is_info", False):
                    _ingest_channel_post(bot, accid, m, {"chat_id": chat_id, "token": token}, token)
            except Exception as e:
                config.logger.warning(f"Failed to backfill msg {mid}: {e}")


def _channel_description(bot, accid, chat_id: int, chat_type) -> str:
    try:
        if str(chat_type) == "Single":
            contacts = bot.rpc.get_chat_contacts(accid, chat_id)
            other_contacts = [c for c in contacts if c != 1]
            if not other_contacts:
                return ""
            contact = bot.rpc.get_contact(accid, other_contacts[0])
            status = contact.get('status') if isinstance(contact, dict) else getattr(contact, 'status', '')
            return (status or "").strip()
        return (bot.rpc.get_chat_description(accid, chat_id) or "").strip()
    except Exception as e:
        config.logger.warning(f"Failed to get description for chat {chat_id}: {e}")
        return ""


def bg_channel_join_worker(bot, accid, admin_chat_id, url, chat_name, qr_info=None):
    try:
        kind = str((qr_info or {}).get("kind", "")).lower()
        # secure_join returns the chat the link leads to: the channel/group chat for
        # group and channel links (created right away, even while the handshake is still
        # waiting for the inviter), the 1:1 chat for contact links. Before 2.33.1 the bot
        # diffed the chat list instead and, when the channel chat already existed from an
        # earlier join, fell back to the 1:1 chat with the channel owner and catalogued that.
        joined_chat_id = bot.rpc.secure_join(accid, url)
        if not isinstance(joined_chat_id, int) or joined_chat_id <= 0:
            dc_helpers._send(bot, accid, admin_chat_id, f"❌ Failed to join channel **{chat_name or 'Channel'}**: core returned no chat.")
            return

        chat = bot.rpc.get_basic_chat_info(accid, joined_chat_id)
        c_name = chat.get('name') if isinstance(chat, dict) else getattr(chat, 'name', None)
        c_name = c_name or chat_name or 'Channel'
        chat_type = chat.get('chat_type') if isinstance(chat, dict) else getattr(chat, 'chat_type', 'Single')

        # A group/channel link must never end up as a 1:1 chat in the catalog.
        if "contact" not in kind and str(chat_type) == "Single":
            dc_helpers._send(bot, accid, admin_chat_id, f"❌ Failed to join channel **{c_name}**: the link opened a private chat, not a channel.")
            return

        # Contact links have nothing to wait for; for channels and groups the bot is only
        # a member after the inviter's device answers the handshake.
        joined = "contact" in kind or _wait_for_join(bot, accid, joined_chat_id, JOIN_QUICK_WAIT, 1.0)

        # Register the chat right away, even while pending, so posts that arrive when the
        # join completes are cached by the normal incoming-message handler.
        description = _channel_description(bot, accid, joined_chat_id, chat_type)
        token = database.add_catalog_channel(joined_chat_id, c_name, description, 0, url, is_public=0)
        ch = database.get_catalog_channel_by_chat_id(joined_chat_id)
        ch_id = ch['id'] if ch else ""
        base_url = database.get_config("base_url") or os.getenv("BASE_URL") or ""
        preview_url = f"{base_url.rstrip('/')}/c/{token}" if base_url else f"/c/{token}"
        links = (
            f"🌐 Web preview: {preview_url}\n"
            f"📡 RSS feed: {preview_url}/rss.xml\n"
            f"🔗 Invite link: {url}\n\n"
        )
        publish_hint = (
            f"ℹ️ This channel is currently unlisted (hidden from the public /dchannels catalog and web landing page).\n"
            f"To make it publicly visible in the catalog, send:\n"
            f"/dchannelpub{ch_id}on"
        )

        if joined:
            _backfill_channel(bot, accid, joined_chat_id, token)
            dc_helpers._send(
                bot, accid, admin_chat_id,
                f"✅ Channel **{c_name}** has been joined and added as **unlisted** (ID: {ch_id}).\n\n"
                + links + publish_hint
            )
            return

        dc_helpers._send(
            bot, accid, admin_chat_id,
            f"⏳ Channel **{c_name}** was added as **unlisted** (ID: {ch_id}), but the join is not finished yet: "
            f"the channel owner's device has not answered. Delta Chat completes the join as soon as that device "
            f"comes online; posts will appear automatically. I will report here when it happens "
            f"(watching for {JOIN_BACKGROUND_WAIT // 60} minutes).\n\n"
            + links + publish_hint
        )
        if _wait_for_join(bot, accid, joined_chat_id, JOIN_BACKGROUND_WAIT, 15.0):
            _backfill_channel(bot, accid, joined_chat_id, token)
            dc_helpers._send(bot, accid, admin_chat_id, f"✅ Joined channel **{c_name}** (ID: {ch_id}). Recent posts are now on {preview_url}")
        else:
            config.logger.info(f"Channel join for chat {joined_chat_id} still pending after {JOIN_BACKGROUND_WAIT}s")
            dc_helpers._send(
                bot, accid, admin_chat_id,
                f"⚠️ Channel **{c_name}** (ID: {ch_id}): the owner's device still has not answered the join request. "
                f"It stays in the catalog and fills up if the join completes later; otherwise ask the owner for a fresh invite link, "
                f"remove the entry with /dchannelremove {ch_id} and add the new link."
            )

    except Exception as e:
        config.logger.error(f"Error in bg_channel_join_worker: {e}")
        dc_helpers._send(bot, accid, admin_chat_id, f"❌ An error occurred while adding the channel: {e}")

@config.dc_cli.on(events.NewMessage(command="/dchanneladd"))
def dchanneladd_command(bot, accid, event):
    msg = event.msg
    is_admin = dc_helpers._is_dc_admin(bot, accid, msg.from_id)
    if not is_admin and not catalog_review.submissions_enabled():
        dc_helpers._send(bot, accid, msg.chat_id, "❌ Only the bot administrator can use this command.")
        return

    url = event.payload.strip() if event.payload else ""
    if not url:
        dc_helpers._send(bot, accid, msg.chat_id, "Usage: /dchanneladd <invite_link_url>")
        return

    existing_channels = database.get_all_catalog_channels()
    for ch in existing_channels:
        if ch.get('invite_link') == url:
            if is_admin:
                dc_helpers._send(bot, accid, msg.chat_id, f"ℹ️ This channel is already in the catalog (ID: {ch['id']}).")
            else:
                dc_helpers._send(bot, accid, msg.chat_id, "ℹ️ This channel is already in the catalog or waiting for review.")
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
            dc_helpers._send(bot, accid, msg.chat_id, "❌ Invalid invite link. Please provide a valid Delta Chat group/channel join link.")
            return
            
        if "contact" in kind_lower or "broadcast" in kind_lower:
            chat_name = qr_info.get("name")
        else:
            chat_name = qr_info.get("grpname")
    except Exception as e:
        dc_helpers._send(bot, accid, msg.chat_id, f"❌ Failed to parse invite link: {e}")
        return

    if not is_admin:
        catalog_review.submit_channel(bot, accid, msg, url, qr_info, chat_name)
        return

    dc_helpers._send(bot, accid, msg.chat_id, f"⏳ Attempting to join channel **{chat_name or 'Channel'}** in the background... This may take up to 30 seconds.")
    threading.Thread(target=bg_channel_join_worker, args=(bot, accid, msg.chat_id, url, chat_name, qr_info), daemon=True).start()

@config.dc_cli.on(events.NewMessage(command="/dchannelremove"))
def dchannelremove_command(bot, accid, event):
    msg = event.msg
    if not dc_helpers._is_dc_admin(bot, accid, msg.from_id):
        dc_helpers._send(bot, accid, msg.chat_id, "❌ Only the bot administrator can use this command.")
        return

    payload = event.payload.strip() if event.payload else ""
    if payload:
        try:
            catalog_id = int(payload)
            channel = database.get_catalog_channel_by_id(catalog_id)
            if channel:
                database.remove_catalog_channel(channel['chat_id'])
                web.routes.invalidate_channel_cache(chat_id=channel['chat_id'], token=channel.get('token'))
                dc_helpers._send(bot, accid, msg.chat_id, f"✅ Channel **{channel['name']}** has been removed from the catalog.")
                return
            else:
                dc_helpers._send(bot, accid, msg.chat_id, "❌ Channel not found in the catalog.")
                return
        except ValueError:
            pass

    channel = database.get_catalog_channel_by_chat_id(msg.chat_id)
    if channel:
        database.remove_catalog_channel(msg.chat_id)
        web.routes.invalidate_channel_cache(chat_id=msg.chat_id, token=channel.get('token'))
        dc_helpers._send(bot, accid, msg.chat_id, f"✅ Channel **{channel['name']}** has been removed from the catalog (web preview disabled).")
    else:
        dc_helpers._send(bot, accid, msg.chat_id, "❌ This chat is not registered as a channel in the catalog. Use `/dchannelremove <ID>` to remove by ID.")
