"""Chat/channel catalog: post ingestion into the web-preview cache
(with ActivityPub delivery queuing), catalog backfill, and the
/chats, /dchannels, /chatadd, /chatremove, /private, /dchanneladd,
/dchannelremove command handlers."""
import os
import re
import shutil
import threading
import time

from deltachat2 import events

import activitypub
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
                if followers_count > 0:
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


@config.dc_cli.on(events.NewMessage(command="/chats"))
def chats_command(bot, accid, event):
    msg = event.msg
    catalog_chats = database.get_all_catalog_chats()
    if not catalog_chats:
        dc_helpers._send(bot, accid, msg.chat_id, "ℹ️ The chat catalog is empty.")
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
        else:
            entry_parts = [f"/dchannel{ch['id']} 🔒 **{ch['name']}** [Unlisted]"]

        desc = (ch.get('description') or "").strip()
        if desc:
            if len(desc) > 200:
                desc = desc[:200] + "..."
            entry_parts.append(desc)
        token = ch.get('token')
        if base_url and token:
            entry_parts.append(f"🌐 Preview: {base_url.rstrip('/')}/c/{token}")

        if is_admin_dm and not is_pub:
            entry_parts.append(f"Publish: /dchannelpub{ch['id']}on")

        entries.append("\n".join(entry_parts))

    reply = "\n\n".join(entries)
    dc_helpers._send(bot, accid, msg.chat_id, reply)

@config.dc_cli.on(events.NewMessage(command="/chatadd"))
def chatadd_command(bot, accid, event):
    msg = event.msg
    if not dc_helpers._is_dc_admin(bot, accid, msg.from_id):
        dc_helpers._send(bot, accid, msg.chat_id, "❌ Only the bot administrator can use this command.")
        return

    try:
        chat = bot.rpc.get_basic_chat_info(accid, msg.chat_id)
        chat_type = chat.get('chat_type', 'Single') if isinstance(chat, dict) else getattr(chat, 'chat_type', 'Single')
    except Exception as e:
        config.logger.error(f"Failed to get chat info: {e}")
        return

    GROUP_TYPES = {"Group", "Mailinglist", "OutBroadcast", "InBroadcast"}
    if str(chat_type) not in GROUP_TYPES:
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

    database.add_catalog_chat(msg.chat_id, chat_name, description, member_count)
    dc_helpers._send(bot, accid, msg.chat_id, f"✅ Chat **{chat_name}** has been added to the catalog.")

@config.dc_cli.on(events.NewMessage(command="/chatremove"))
def chatremove_command(bot, accid, event):
    msg = event.msg
    if not dc_helpers._is_dc_admin(bot, accid, msg.from_id):
        dc_helpers._send(bot, accid, msg.chat_id, "❌ Only the bot administrator can use this command.")
        return

    database.remove_catalog_chat(msg.chat_id)
    dc_helpers._send(bot, accid, msg.chat_id, "✅ Chat has been removed from the catalog.")

@config.dc_cli.on(events.NewMessage(command="/private"))
def private_command(bot, accid, event):
    msg = event.msg
    if not dc_helpers._is_dc_admin(bot, accid, msg.from_id):
        dc_helpers._send(bot, accid, msg.chat_id, "❌ Only the bot administrator can use this command.")
        return

    arg = event.payload.strip().lower() if event.payload else ""
    if arg not in ("on", "off"):
        dc_helpers._send(bot, accid, msg.chat_id, "Usage: /private on or /private off")
        return

    catalog_chat = database.get_catalog_chat_by_chat_id(msg.chat_id)
    if not catalog_chat:
        dc_helpers._send(bot, accid, msg.chat_id, "❌ Please add this chat to the catalog first using /chatadd.")
        return

    is_private = 1 if arg == "on" else 0
    database.update_catalog_chat_privacy(msg.chat_id, is_private)
    status_str = "private (by request)" if is_private else "public"
    dc_helpers._send(bot, accid, msg.chat_id, f"✅ Chat is now {status_str}.")

def bg_channel_join_worker(bot, accid, admin_chat_id, url, chat_name, qr_info=None):
    try:
        old_chats = set(bot.rpc.get_chatlist_entries(accid, None, None, None))
        bot.rpc.secure_join(accid, url)
        
        joined_chat_id = None
        delays = [0.25, 0.25, 0.5, 0.5, 1.0, 1.0, 1.5, 2.0, 2.0, 3.0, 3.0, 4.0, 5.0, 5.0]
        for delay in delays:
            time.sleep(delay)
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
                    config.logger.warning(f"Failed to get contact status: {e}")
            else:
                try:
                    description = bot.rpc.get_chat_description(accid, joined_chat_id) or ""
                    description = description.strip()
                except Exception:
                    pass
                
            token = database.add_catalog_channel(joined_chat_id, c_name, description, 0, url, is_public=0)
            
            # Backfill initial messages delivered by core (up to 10 messages from handshake)
            try:
                chat_msgs = bot.rpc.get_message_ids(accid, joined_chat_id, False, False)
                for mid in chat_msgs[-10:]:
                    if isinstance(mid, int) and mid > 0:
                        try:
                            m = bot.rpc.get_message(accid, mid)
                            if m and not getattr(m, "is_info", False):
                                _ingest_channel_post(bot, accid, m, {"chat_id": joined_chat_id, "token": token}, token)
                        except Exception as e:
                            config.logger.warning(f"Failed to backfill msg {mid}: {e}")
            except Exception as e:
                config.logger.warning(f"Failed to backfill messages for channel {joined_chat_id}: {e}")

            ch = database.get_catalog_channel_by_chat_id(joined_chat_id)
            ch_id = ch['id'] if ch else ""
            base_url = database.get_config("base_url") or os.getenv("BASE_URL") or ""
            preview_url = f"{base_url.rstrip('/')}/c/{token}" if base_url else f"/c/{token}"
            rss_url = f"{preview_url}/rss.xml"
            dc_helpers._send(
                bot,
                accid,
                admin_chat_id,
                f"✅ Channel **{c_name}** has been joined and added as **unlisted** (ID: {ch_id}).\n\n"
                f"🌐 Web preview: {preview_url}\n"
                f"📡 RSS feed: {rss_url}\n"
                f"🔗 Invite link: {url}\n\n"
                f"ℹ️ This channel is currently unlisted (hidden from the public /dchannels catalog and web landing page).\n"
                f"To make it publicly visible in the catalog, send:\n"
                f"/dchannelpub{ch_id}on"
            )
        else:
            dc_helpers._send(bot, accid, admin_chat_id, f"❌ Failed to join channel **{chat_name or 'Channel'}**. The securejoin handshake timed out. Please check that the link is correct and try again.")
            
    except Exception as e:
        config.logger.error(f"Error in bg_channel_join_worker: {e}")
        dc_helpers._send(bot, accid, admin_chat_id, f"❌ An error occurred while adding the channel: {e}")

@config.dc_cli.on(events.NewMessage(command="/dchanneladd"))
def dchanneladd_command(bot, accid, event):
    msg = event.msg
    if not dc_helpers._is_dc_admin(bot, accid, msg.from_id):
        dc_helpers._send(bot, accid, msg.chat_id, "❌ Only the bot administrator can use this command.")
        return

    url = event.payload.strip() if event.payload else ""
    if not url:
        dc_helpers._send(bot, accid, msg.chat_id, "Usage: /dchanneladd <invite_link_url>")
        return

    existing_channels = database.get_all_catalog_channels()
    for ch in existing_channels:
        if ch.get('invite_link') == url:
            dc_helpers._send(bot, accid, msg.chat_id, f"ℹ️ This channel is already in the catalog (ID: {ch['id']}).")
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
