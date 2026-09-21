"""Global Delta Chat event handlers: system-message (member join/leave)
handling for catalog chats/channels + welcome greetings, and the catch-all
NewMessage handler that ingests channel posts, runs the away-mention
notifier, and dispatches dynamic /chatNN, /dchannelNN, /approveNN,
/declineNN and /contactNN commands.

handle_all_messages has no command filter, so it fires on every incoming
message alongside whichever specific-command handler (if any) also
matches - deltachat2's HookCollection stores hooks in a plain `set`, so
import/registration order has no effect on dispatch order between them;
each handler is independent and simply no-ops when its own condition
doesn't match.
"""
import os
import re
import tempfile
import threading
import time

from deltachat2 import events, MsgData, SystemMessageType

import channels
import config
import database
import dc_helpers
import web.routes

@config.dc_cli.on(events.NewMessage(is_info=True, is_bot=None, is_outgoing=None))

def handle_dc_info_message(bot, accid, event):
    msg = event.msg
    dc_chat_id = msg.chat_id
    
    smt = msg.system_message_type
    smt_str = str(smt).lower() if smt is not None else ""
    msg_text = (msg.text or "").lower()
    
    config.logger.info(f"handle_dc_info_message: dc_chat_id={dc_chat_id}, smt={smt!r}, smt_str={smt_str!r}, msg_text={msg_text!r}")
    
    is_member_event = False
    is_join_event = False
    is_remove_event = False
    try:
        if smt == SystemMessageType.MEMBER_ADDED_TO_GROUP:
            is_member_event = True
            is_join_event = True
        elif smt == SystemMessageType.MEMBER_REMOVED_FROM_GROUP:
            is_member_event = True
            is_remove_event = True
    except Exception:
        pass
        
    if not is_member_event:
        is_member_event = any(kw in smt_str or kw in msg_text for kw in ("memberadded", "memberremoved", "member_added", "member_removed", "left", "added", "removed"))
        is_join_event = any(kw in smt_str or kw in msg_text for kw in ("memberadded", "member_added", "joined", "added"))
        is_remove_event = any(kw in smt_str or kw in msg_text for kw in ("memberremoved", "member_removed", "left", "removed"))

    info_contact_id = None
    for attr_name in ('info_contact_id', 'infoContactId'):
        val = getattr(msg, attr_name, None)
        if val:
            info_contact_id = val
            break
    if not info_contact_id and isinstance(msg, dict):
        info_contact_id = msg.get('info_contact_id') or msg.get('infoContactId')

    is_self_removed = False
    if is_remove_event:
        if info_contact_id == config.DC_CONTACT_ID_SELF:
            is_self_removed = True
        elif any(p in msg_text for p in ("you were removed", "you left", "removed you")):
            is_self_removed = True
        elif info_contact_id is None or info_contact_id == config.DC_CONTACT_ID_SELF:
            try:
                contacts = bot.rpc.get_chat_contacts(accid, dc_chat_id)
                if config.DC_CONTACT_ID_SELF not in contacts:
                    is_self_removed = True
            except Exception:
                is_self_removed = True

    if is_member_event:
        catalog_channel = database.get_catalog_channel_by_chat_id(dc_chat_id)
        if catalog_channel:
            if is_self_removed:
                database.remove_catalog_channel(dc_chat_id)
                web.routes.invalidate_channel_cache(chat_id=dc_chat_id, token=catalog_channel.get('token'))
                config.logger.info(f"Bot was removed from channel {dc_chat_id} ('{catalog_channel.get('name')}'). Soft-removed channel from catalog.")
                return
            else:
                try:
                    contacts = bot.rpc.get_chat_contacts(accid, dc_chat_id)
                    member_count = sum(1 for c in contacts if c != config.DC_CONTACT_ID_SELF)
                    database.update_catalog_channel_member_count(dc_chat_id, member_count)
                    config.logger.info(f"Updated member count for catalog channel {dc_chat_id} to {member_count}")
                except Exception as e:
                    config.logger.debug(f"Failed to update member count for catalog channel {dc_chat_id}: {e}")

        catalog_chat = database.get_catalog_chat_by_chat_id(dc_chat_id)
        if catalog_chat:
            if is_self_removed:
                database.remove_catalog_chat(dc_chat_id)
                config.logger.info(f"Bot was removed from chat {dc_chat_id} ('{catalog_chat.get('name')}'). Removed chat from catalog.")
                return
            try:
                contacts = bot.rpc.get_chat_contacts(accid, dc_chat_id)
                member_count = sum(1 for c in contacts if c != 1)
                database.update_catalog_chat_member_count(dc_chat_id, member_count)
                config.logger.info(f"Updated member count for catalog chat {dc_chat_id} to {member_count}")
                
                # Check if it was a join event and welcome is enabled
                if is_join_event and catalog_chat.get('welcome_enabled'):
                    new_member_id = info_contact_id
                    config.logger.info(f"Welcome greeting: join event in chat {dc_chat_id}, msg.text={msg.text!r}, info_contact_id={new_member_id!r}")
                    
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
                        
                        config.logger.info(f"Welcome greeting: parsed added_name={added_name!r} from text={msg.text!r}")
                        
                        if added_name:
                            # Try lookup by email address first
                            try:
                                new_member_id = bot.rpc.lookup_contact_id_by_addr(accid, added_name)
                            except Exception:
                                new_member_id = None
                            
                            # Try finding in chat contacts by name/address
                            if not new_member_id:
                                new_member_id = dc_helpers.find_contact_in_chat(bot, accid, dc_chat_id, added_name)
                            
                            config.logger.info(f"Welcome greeting: lookup for {added_name!r} -> contact_id={new_member_id!r}")
                    
                    # Last resort: if we detected a join but couldn't identify who,
                    # check from_id (the person who triggered the event)
                    if not new_member_id and msg.from_id and msg.from_id != 1:
                        # from_id in a join info message is usually the person who added,
                        # not the person who was added — so skip this for self-join scenarios
                        config.logger.info(f"Welcome greeting: could not resolve new member, from_id={msg.from_id}")
                    
                    config.logger.info(f"Welcome greeting: resolved new_member_id={new_member_id!r}")
                    if new_member_id and new_member_id != 1:
                        try:
                            contact = bot.rpc.get_contact(accid, new_member_id)
                            member_name = contact.name or contact.display_name or contact.address or "New member"
                            user_chat_count = dc_helpers.get_user_chat_count(bot, accid, new_member_id)
                            is_multi = (user_chat_count > 1)
                            
                            database.ensure_contact_first_seen(new_member_id, time.time())
                            age = dc_helpers._get_contact_age_indicator(new_member_id, is_multi_chat=is_multi)
                            
                            chat_name = catalog_chat['name']
                            welcome_suffix = f" {catalog_chat['welcome_text']}" if catalog_chat.get('welcome_text') else ""
                            
                            welcome_msg = f"👋🏻 {age} **{member_name}**, welcome to {chat_name} group!{welcome_suffix}"
                            dc_helpers._send(bot, accid, dc_chat_id, welcome_msg)
                            config.logger.info(f"Welcome greeting sent for {member_name} in chat {dc_chat_id}")
                        except Exception as welcome_err:
                            config.logger.error(f"Failed to send welcome greeting: {welcome_err}")
                
                # Check if chat is private and has an active invite link to revoke
                if is_join_event and catalog_chat.get('is_private') and catalog_chat.get('invite_link'):
                    invite_link = catalog_chat['invite_link']
                    invite_msg_id = catalog_chat.get('invite_msg_id')
                    try:
                        qr_info = bot.rpc.check_qr(accid, invite_link)
                        if qr_info.get("kind") == "withdrawVerifyGroup":
                            bot.rpc.set_config_from_qr(accid, invite_link)
                            config.logger.info(f"Withdrew/invalidated invite link for private chat {dc_chat_id}")
                    except Exception as qr_err:
                        config.logger.error(f"Failed to withdraw invite link: {qr_err}")

                    if invite_msg_id:
                        try:
                            bot.rpc.delete_messages_for_all(accid, [invite_msg_id])
                            config.logger.info(f"Deleted invite message {invite_msg_id} for all in private chat {dc_chat_id}")
                        except Exception as del_err:
                            config.logger.warning(f"Failed to delete invite message {invite_msg_id} for all: {del_err}. Attempting to edit instead...")
                            try:
                                bot.rpc.send_edit_request(accid, invite_msg_id, "❌ This single-use invite link has expired.")
                                config.logger.info(f"Edited invite message {invite_msg_id} to mark as expired")
                            except Exception as edit_err:
                                config.logger.error(f"Failed to edit invite message {invite_msg_id}: {edit_err}")

                    database.update_catalog_chat_invite_link(dc_chat_id, None, None)
            except Exception as e:
                config.logger.error(f"Failed to update catalog chat member count: {e}")


@config.dc_cli.on(events.NewMessage)
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

    # ── Public Channel Post Ingestion ──
    try:
        catalog_channel = database.get_catalog_channel_by_chat_id(msg.chat_id)
        if catalog_channel and not getattr(msg, "is_info", False):
            token = catalog_channel.get("token")
            if not token:
                token = database.get_or_create_channel_token(msg.chat_id)
            threading.Thread(
                target=channels._ingest_channel_post,
                args=(bot, accid, msg, catalog_channel, token),
                daemon=True,
            ).start()
    except Exception as e:
        config.logger.error(f"Error ingesting channel post for chat {msg.chat_id}: {e}")
        
    text = (msg.text or "").strip()
    
    # ── Away Check ──
    if msg.from_id > 9 and msg.from_id != config.DC_CONTACT_ID_SELF and not text.startswith("/"):
        notified_away_user_ids = set()
        message_text = (msg.text or "").lower()

        # 1. Check if this message is a reply to an away user
        if hasattr(msg, "quote") and msg.quote and isinstance(msg.quote, dict):
            quote_msg_id = msg.quote.get('message_id')
            if quote_msg_id:
                try:
                    quoted_msg = bot.rpc.get_message(accid, quote_msg_id)
                    quoted_sender_id = quoted_msg.from_id
                    if quoted_sender_id > 9 and quoted_sender_id != msg.from_id and quoted_sender_id != config.DC_CONTACT_ID_SELF:
                        details = database.get_away_status_details(quoted_sender_id)
                        if details:
                            away_text, away_updated_at = details
                            # Debounce: check if we already notified this recipient about this specific away session
                            if not database.has_notified_away(quoted_sender_id, msg.from_id, away_updated_at):
                                database.mark_notified_away(quoted_sender_id, msg.from_id, away_updated_at)
                                notified_away_user_ids.add(quoted_sender_id)
                                private_chat_id = bot.rpc.create_chat_by_contact_id(accid, msg.from_id)
                                away_contact = bot.rpc.get_contact(accid, quoted_sender_id)
                                away_name = away_contact.name or away_contact.display_name or away_contact.address or "User"
                                dc_helpers._send(bot, accid, private_chat_id, f"_{away_name} is away: {away_text}_")
                except Exception as e:
                    config.logger.error(f"Error checking quote sender in away check: {e}")

        # 2. Check if this message mentions any away user in the current chat by their full name
        try:
            chat_contacts = bot.rpc.get_chat_contacts(accid, msg.chat_id)
        except Exception as e:
            config.logger.error(f"Failed to get chat contacts for away mention check: {e}")
            chat_contacts = []

        for contact_id in chat_contacts:
            if contact_id <= 9 or contact_id == msg.from_id or contact_id == config.DC_CONTACT_ID_SELF:
                continue
            if contact_id in notified_away_user_ids:
                continue

            details = database.get_away_status_details(contact_id)
            if not details:
                continue

            away_text, away_updated_at = details
            if database.has_notified_away(contact_id, msg.from_id, away_updated_at):
                continue

            try:
                contact = bot.rpc.get_contact(accid, contact_id)
                names_to_check = []
                if contact.name:
                    names_to_check.append(contact.name.strip().lower())
                if contact.display_name:
                    names_to_check.append(contact.display_name.strip().lower())

                mentioned = False
                for name in names_to_check:
                    if name and len(name) >= 2:
                        pattern = r'(?i)(?<!\w)' + re.escape(name) + r'(?!\w)'
                        if re.search(pattern, message_text):
                            mentioned = True
                            break

                if mentioned:
                    database.mark_notified_away(contact_id, msg.from_id, away_updated_at)
                    notified_away_user_ids.add(contact_id)
                    private_chat_id = bot.rpc.create_chat_by_contact_id(accid, msg.from_id)
                    away_name = contact.name or contact.display_name or contact.address or "User"
                    dc_helpers._send(bot, accid, private_chat_id, f"_{away_name} is away: {away_text}_")
            except Exception as e:
                config.logger.error(f"Error checking contact {contact_id} in away check: {e}")
    
    # Handle /chatdesc<ID> command
    if text.startswith("/chatdesc"):
        m = re.match(r'^/chatdesc(\d+)(?:\s+(.*))?$', text, re.IGNORECASE)
        if m:
            if not dc_helpers._is_dc_admin(bot, accid, msg.from_id):
                dc_helpers._send(bot, accid, msg.chat_id, "❌ Only the bot administrator can use this command.")
                return
                
            catalog_id = int(m.group(1))
            new_desc = m.group(2).strip() if m.group(2) else ""
            
            catalog_chat = database.get_catalog_chat_by_id(catalog_id)
            if not catalog_chat:
                dc_helpers._send(bot, accid, msg.chat_id, "❌ Chat with this number was not found in the catalog.")
                return
                
            database.update_catalog_chat_description(catalog_id, new_desc)
            dc_helpers._send(bot, accid, msg.chat_id, f"✅ Description for chat **{catalog_chat['name']}** has been updated.")
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
                dc_helpers._send(bot, accid, msg.chat_id, "❌ Chat with this number was not found in the catalog.")
                return

            chat_id = catalog_chat['chat_id']
            
            # Check if user is already in the chat
            try:
                contacts = bot.rpc.get_chat_contacts(accid, chat_id)
                if msg.from_id in contacts:
                    dc_helpers._send(bot, accid, msg.chat_id, "ℹ️ You are already a member of this chat.")
                    return
            except Exception as e:
                config.logger.error(f"Failed to check contacts for join request: {e}")

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
                        
                    dc_helpers._send(bot, accid, msg.chat_id, f"🔗 Invite link to chat **{catalog_chat['name']}**:\n{invite_link}")
                except Exception as e:
                    config.logger.error(f"Failed to generate invite: {e}")
                    dc_helpers._send(bot, accid, msg.chat_id, "❌ Failed to generate invite link.")
            else:
                # Private chat: request confirmation from participants in the chat
                try:
                    contact = bot.rpc.get_contact(accid, msg.from_id)
                    requester_name = contact.name or contact.display_name or contact.address or "Unknown"
                    user_chat_count = dc_helpers.get_user_chat_count(bot, accid, msg.from_id)
                    database.ensure_contact_first_seen(msg.from_id, time.time())
                    age = dc_helpers._get_contact_age_indicator(msg.from_id)
                    
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
                    
                    dc_helpers._send(bot, accid, chat_id, req_msg)
                    dc_helpers._send(bot, accid, msg.chat_id, "⏳ Your request to join has been sent to group members. Please wait for approval.")
                except Exception as e:
                    config.logger.error(f"Failed to create join request: {e}")
                    return

    # Handle /dchanneldesc<ID> command
    elif text.startswith("/dchanneldesc"):
        m = re.match(r'^/dchanneldesc(\d+)(?:\s+(.*))?$', text, re.IGNORECASE)
        if m:
            if not dc_helpers._is_dc_admin(bot, accid, msg.from_id):
                dc_helpers._send(bot, accid, msg.chat_id, "❌ Only the bot administrator can use this command.")
                return
                
            catalog_id = int(m.group(1))
            new_desc = m.group(2).strip() if m.group(2) else ""
            
            channel = database.get_catalog_channel_by_id(catalog_id)
            if not channel:
                dc_helpers._send(bot, accid, msg.chat_id, "❌ Channel with this number was not found in the catalog.")
                return
                
            database.update_catalog_channel_description(catalog_id, new_desc)
            dc_helpers._send(bot, accid, msg.chat_id, f"✅ Description for channel **{channel['name']}** has been updated.")
            return

    # Handle /dchannelpub<ID>on and /dchannelpub<ID>off command
    elif text.startswith("/dchannelpub"):
        m = re.match(r'^/dchannelpub\s*(\d+)\s*(on|off)$', text, re.IGNORECASE)
        if m:
            if not dc_helpers._is_dc_admin(bot, accid, msg.from_id):
                dc_helpers._send(bot, accid, msg.chat_id, "❌ Only the bot administrator can use this command.")
                return
            catalog_id = int(m.group(1))
            action = m.group(2).lower()
            channel = database.get_catalog_channel_by_id(catalog_id, include_deleted=False)
            if not channel:
                dc_helpers._send(bot, accid, msg.chat_id, "❌ Channel with this number was not found in the catalog.")
                return

            new_status = 1 if action == "on" else 0
            database.set_catalog_channel_public(catalog_id, new_status)
            web.routes.invalidate_channel_cache(chat_id=channel['chat_id'], token=channel.get('token'))

            if new_status == 1:
                dc_helpers._send(
                    bot, accid, msg.chat_id,
                    f"✅ Channel **{channel['name']}** (ID: {catalog_id}) is now **public**.\n"
                    f"It is visible in /dchannels and on the web landing page."
                )
            else:
                dc_helpers._send(
                    bot, accid, msg.chat_id,
                    f"🔒 Channel **{channel['name']}** (ID: {catalog_id}) is now **unlisted**.\n"
                    f"It is hidden from the public /dchannels list and landing page, but its web preview and RSS feed remain active."
                )
            return
        else:
            if not dc_helpers._is_dc_admin(bot, accid, msg.from_id):
                dc_helpers._send(bot, accid, msg.chat_id, "❌ Only the bot administrator can use this command.")
                return
            dc_helpers._send(bot, accid, msg.chat_id, "Usage: `/dchannelpub<ID>on` or `/dchannelpub<ID>off` (e.g. `/dchannelpub42on`).")
            return

    # Handle /dchannel<ID> command (but NOT /dchannels or /dchannelpub)
    elif text.startswith("/dchannel") and not (text == "/dchannels" or text.startswith("/dchannels ") or text.startswith("/dchannelpub")):
        m = re.match(r'^/dchannel(\d+)', text, re.IGNORECASE)
        if m:
            catalog_id = int(m.group(1))
            channel = database.get_catalog_channel_by_id(catalog_id, include_deleted=True)
            if not channel:
                dc_helpers._send(bot, accid, msg.chat_id, "❌ Channel with this number was not found in the catalog.")
                return
            if channel.get('is_deleted'):
                dc_helpers._send(bot, accid, msg.chat_id, f"ℹ️ Channel **{channel['name']}** was removed from the public catalog.")
                return

            is_pub = bool(channel.get('is_public', 1))
            is_admin = dc_helpers._is_dc_admin(bot, accid, msg.from_id)
            if not is_pub and not is_admin:
                dc_helpers._send(bot, accid, msg.chat_id, "❌ Channel with this number was not found in the catalog.")
                return
                
            invite_link = channel.get('invite_link')
            token = channel.get('token')
            if not token:
                token = database.get_or_create_channel_token(channel['chat_id'])
            base_url = database.get_config("base_url") or os.getenv("BASE_URL") or ""
            preview_url = f"{base_url.rstrip('/')}/c/{token}" if base_url else f"/c/{token}"

            header_str = f"📢 **{channel['name']}**" if is_pub else f"📢 **{channel['name']}** 🔒 [Unlisted]"
            response_lines = [header_str]
            if channel.get('description'):
                response_lines.append(f"{channel['description']}\n")
            if invite_link:
                if invite_link.startswith("OPEN-CHAT:"):
                    join_link = "https://i.delta.chat/#" + invite_link[10:]
                elif invite_link.startswith("OPEN:"):
                    join_link = "https://i.delta.chat/#" + invite_link[5:]
                elif invite_link.startswith("dcqr://"):
                    join_link = "https://i.delta.chat/#" + invite_link[7:]
                else:
                    join_link = invite_link
                response_lines.append(f"🔗 Invite link: {join_link}")
            response_lines.append(f"🌐 Web preview: {preview_url}")
            if not is_pub and is_admin:
                response_lines.append(f"Publish: /dchannelpub{catalog_id}on")

            dc_helpers._send(bot, accid, msg.chat_id, "\n".join(response_lines))
            return
 
    # 2. Handle /approve<ID> command
    # Architectural Note: /approve and /decline intentionally allow any group member to participate
    # in membership approval (community-driven self-moderation). No admin check is required.
    elif text.startswith("/approve"):
        m = re.match(r'^/approve(\d+)$', text, re.IGNORECASE)
        if m:
            request_id = int(m.group(1))
            req = database.get_pending_request(request_id)
            if not req:
                dc_helpers._send(bot, accid, msg.chat_id, "❌ Request not found.")
                return
            if req['approved'] == 1:
                dc_helpers._send(bot, accid, msg.chat_id, "ℹ️ This request has already been approved.")
                return
            elif req['approved'] == 2:
                dc_helpers._send(bot, accid, msg.chat_id, "ℹ️ This request has already been declined.")
                return
            if req['chat_id'] != msg.chat_id:
                dc_helpers._send(bot, accid, msg.chat_id, "❌ You can only approve this request in the corresponding group chat.")
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
                
                dc_helpers._send(bot, accid, user_chat_id, 
                      f"✅ Your request to join chat **{chat_name}** has been approved!\n\n"
                      f"Single-use invite link:\n{invite_link}\n\n"
                      f"⚠️ This invite link is single-use and will be invalidated after one connection.")
                      
                dc_helpers._send(bot, accid, msg.chat_id, f"✅ Request approved. A single-use invite link has been sent to **{req['requester_name']}**.")
            except Exception as e:
                config.logger.error(f"Failed to approve request {request_id}: {e}")
                dc_helpers._send(bot, accid, msg.chat_id, "❌ Error while approving request.")
            return

    # 3. Handle /decline<ID> [comment] command
    elif text.startswith("/decline"):
        m = re.match(r'^/decline(\d+)(?:\s+(.*))?$', text, re.IGNORECASE)
        if m:
            request_id = int(m.group(1))
            reason = m.group(2).strip() if m.group(2) else ""
            
            req = database.get_pending_request(request_id)
            if not req:
                dc_helpers._send(bot, accid, msg.chat_id, "❌ Request not found.")
                return
            if req['approved'] == 1:
                dc_helpers._send(bot, accid, msg.chat_id, "ℹ️ This request has already been approved.")
                return
            elif req['approved'] == 2:
                dc_helpers._send(bot, accid, msg.chat_id, "ℹ️ This request has already been declined.")
                return
            if req['chat_id'] != msg.chat_id:
                dc_helpers._send(bot, accid, msg.chat_id, "❌ You can only decline this request in the corresponding group chat.")
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
                
                dc_helpers._send(bot, accid, user_chat_id, decline_msg)
                dc_helpers._send(bot, accid, msg.chat_id, f"❌ Request declined. The user has been notified.")
            except Exception as e:
                config.logger.error(f"Failed to decline request {request_id}: {e}")
                dc_helpers._send(bot, accid, msg.chat_id, "❌ Error while declining request.")
            return

    # 3. Original contact sharing logic
    elif text.startswith("/contact"):
        config.logger.debug(f"Processing contact request: {text}")
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
                    
                    if contact_id in chat_contact_ids or dc_helpers._is_dc_admin(bot, accid, msg.from_id):
                        config.logger.debug(f"Sharing contact {contact_id} in chat {msg.chat_id}")
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
                                if addr and isinstance(addr, str) and addr != "unknown":
                                    database.increment_transport_sent(addr)
                            except Exception:
                                pass
                            config.logger.debug(f"Successfully shared contact {contact_id}")
                        finally:
                            if temp_path and os.path.exists(temp_path):
                                try:
                                    os.unlink(temp_path)
                                except Exception as e:
                                    config.logger.warning(f"Failed to delete temp vCard file {temp_path}: {e}")
                    else:
                        config.logger.warning(f"User {msg.from_id} tried to access contact {contact_id} not in chat {msg.chat_id}")
                except Exception as e:
                    config.logger.error(f"Error in contact security check: {e}")
        except Exception as e:
            config.logger.error(f"Error parsing contact ID: {e}")
