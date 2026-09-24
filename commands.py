"""General bot commands: /initadmin, /bounce, /slap, /me, /away, /back,
/search, /help, /donate, /invite, /relays, /url, /welcome.

Calls into moderation.py for the autokick overview/inactivity report used
by /bounce, and into web.routes for cache invalidation used by /url."""
import os
import re
import tempfile
import time
from datetime import datetime

import qrcode
from deltachat2 import events, MsgData

import config
import database
import dc_helpers
import moderation
import state
import web.routes

@config.dc_cli.on(events.NewMessage(command="/initadmin"))
def initadmin_command(bot, accid, event):
    msg = event.msg
    admin_email = database.get_config("admin_dc_email")
    admin_fp = database.get_admin_fingerprint()

    if admin_email or admin_fp:
        dc_helpers._send(bot, accid, msg.chat_id, "❌ Admin is already set. Use `set_admin.py` on the server to change.")
        return

    contact = bot.rpc.get_contact(accid, msg.from_id)
    email = contact.address
    database.set_config("admin_dc_email", email)

    fp = dc_helpers._get_contact_fingerprint(bot, accid, msg.from_id, contact=contact)
    if fp:
        first_fp = fp.split(',')[0]
        database.set_admin_fingerprint(first_fp)
        dc_helpers._send(bot, accid, msg.chat_id,
              f"✅ You are now the admin!\n\nEmail: `{email}`\nFingerprint: `{first_fp[-8:]}`")
    else:
        dc_helpers._send(bot, accid, msg.chat_id,
              f"✅ You are now the admin!\n\nEmail: `{email}`\n⚠️ Fingerprint not available yet.")

@config.dc_cli.on(events.NewMessage(command="/bounce"))
def bounce_command(bot, accid, event, skip_cooldown: bool = False):
    msg = event.msg
    
    # Allow everyone to use /bounce, but with a cooldown (admins are exempt)
    is_admin = dc_helpers._is_dc_admin(bot, accid, msg.from_id)
    now = time.time()
    
    if not is_admin and not skip_cooldown:
        last_bounce = state._chat_bounce_anti_spam.get(msg.chat_id, 0)
        diff = now - last_bounce
        if diff < config.BOUNCE_COOLDOWN_SECONDS:
            remaining_sec = max(1, int(config.BOUNCE_COOLDOWN_SECONDS - diff))
            dc_helpers._queue_delayed_command(bot, accid, msg, "bounce", remaining_sec, bounce_command, bot, accid, event, skip_cooldown=True)
            return

    query = event.payload.strip() if event.payload else ""
    
    # Check if this is a reply to another message
    target_contact_id = None
    if hasattr(msg, "quote") and msg.quote and isinstance(msg.quote, dict):
        quote_msg_id = msg.quote.get('message_id')
        if quote_msg_id:
            try:
                quoted_msg = bot.rpc.get_message(accid, quote_msg_id)
                if quoted_msg.from_id > 9:
                    target_contact_id = quoted_msg.from_id
            except Exception:
                pass

    if target_contact_id or query:
        matched_contacts = []
        if target_contact_id:
            try:
                contact = bot.rpc.get_contact(accid, target_contact_id)
                matched_contacts = [contact]
            except Exception as e:
                config.logger.error(f"Failed to get contact for quote target {target_contact_id}: {e}")
        else:
            clean_query = query.lstrip('@').lower()
            try:
                chat_contacts = bot.rpc.get_chat_contacts(accid, msg.chat_id)
            except Exception as e:
                config.logger.error(f"Failed to get chat contacts: {e}")
                chat_contacts = []

            for contact_id in chat_contacts:
                if contact_id <= 9 or contact_id == config.DC_CONTACT_ID_SELF:
                    continue
                try:
                    contact = bot.rpc.get_contact(accid, contact_id)
                    contact_name = (contact.name or "").lower()
                    contact_display = (contact.display_name or "").lower()
                    contact_address = (contact.address or "").lower()
                    contact_id_str = str(contact_id)
                    
                    if (clean_query == contact_id_str or
                        clean_query in contact_name or
                        clean_query in contact_display or
                        clean_query in contact_address.split('@')[0] or
                        clean_query in contact_address):
                        matched_contacts.append(contact)
                except Exception:
                    continue

        if not matched_contacts:
            dc_helpers._send(bot, accid, msg.chat_id, f"🔍 User '{query}' was not found in this chat.", reply_to_id=msg.id)
            return

        # Update timestamp for cooldown since this was a successful check
        state._chat_bounce_anti_spam[msg.chat_id] = now

        report_lines = []
        for contact in matched_contacts:
            name = contact.name or contact.display_name or "Unknown"
            address = contact.address or "no_email@example.com"
            last_seen = contact.last_seen if hasattr(contact, "last_seen") else contact.get("last_seen", 0)
            
            # Ensure contact first-seen tracking is updated
            database.ensure_contact_first_seen(contact.id, now)
            age = dc_helpers._get_contact_age_indicator(contact.id)
            badges = dc_helpers._get_user_badges(bot, accid, contact.id, contact=contact)
            badge_str = f" {badges}" if badges else ""
            
            if last_seen == 0:
                report_lines.append(f"• /contact{contact.id} {age} **{name}**{badge_str} ({address}) — [never seen]")
            else:
                date_str = datetime.fromtimestamp(last_seen).strftime("%-d %b %Y")
                days_ago = int((now - last_seen) / (24 * 3600))
                if days_ago == 0:
                    seen_str = "today"
                elif days_ago == 1:
                    seen_str = "yesterday"
                else:
                    seen_str = f"{days_ago}d ago"
                report_lines.append(f"• /contact{contact.id} {age} **{name}**{badge_str} ({address}) — last seen {date_str} ({seen_str})")

        if len(matched_contacts) > 1:
            report = f"🔍 **Activity Check Matches ({len(matched_contacts)}):**\n\n" + "\n".join(report_lines)
        else:
            report = f"ℹ️ **Activity Check:**\n" + "\n".join(report_lines)

        dc_helpers._send(bot, accid, msg.chat_id, report)
        return

    # Update timestamp
    state._chat_bounce_anti_spam[msg.chat_id] = now

    autokick_days = database.get_chat_autokick(msg.chat_id)
    if autokick_days > 0:
        overview = moderation._get_chat_autokick_overview(bot, accid, msg.chat_id, autokick_days)
        warn_candidates = overview["warn_candidates"]
        warn_threshold = dc_helpers._get_chat_autokick_warn_threshold(autokick_days)
        monitored_days = overview["monitored_days"]
        silent_count = overview["silent_count"]
        active_count = overview["active_count"]
        total_members = overview["total_members"]
        earliest_warn = overview["earliest_warn_days"]

        if warn_candidates:
            lines = []
            for c in warn_candidates:
                remaining = max(1, autokick_days - c["inactive_days"])
                badges = dc_helpers._get_user_badges(bot, accid, c["id"])
                badge_str = f" {badges}" if badges else ""
                lines.append(f"• /contact{c['id']} **{c['name']}**{badge_str} ({c['address']}) — {c['reason']} ({remaining}d remaining)")
            days_left = autokick_days - warn_threshold
            unit_str = f"<{days_left}d" if days_left > 1 else "<1d"
            report = (
                f"⚠️ **Inactivity Warning ({unit_str} until auto-kick):**\n"
                f"Auto-kick threshold for this group: **{autokick_days} days** (warning at > {warn_threshold} days).\n"
                f"Monitoring this group for: **{monitored_days} days**.\n\n"
                + "\n".join(lines)
            )
            if silent_count > 0 and earliest_warn is not None:
                report += f"\n\n_⏳ Plus {silent_count} other member(s) under observation (never seen, earliest warning in {earliest_warn}d)._"
            dc_helpers._send(bot, accid, msg.chat_id, report)
        else:
            if silent_count > 0 and earliest_warn is not None:
                report = (
                    f"ℹ️ **Activity Check & Auto-kick Status**\n"
                    f"• Monitoring this group for: **{monitored_days} days** (threshold: **{autokick_days}d**, warnings start at **{warn_threshold}d**)\n"
                    f"• Active members recently: **{active_count}**\n\n"
                    f"⏳ **Observation in progress:**\n"
                    f"**{silent_count} member(s)** have not posted since monitoring began.\n"
                    f"Earliest warnings will start in **{earliest_warn} days** (at day {warn_threshold} of observation)."
                )
                dc_helpers._send(bot, accid, msg.chat_id, report)
            else:
                dc_helpers._send(bot, accid, msg.chat_id, f"✅ All {total_members} members are active (posted within the last {autokick_days} days).")
    else:
        report = moderation._check_chat_inactivity(bot, accid, msg.chat_id)
        if report:
            dc_helpers._send(bot, accid, msg.chat_id, report)
        else:
            dc_helpers._send(bot, accid, msg.chat_id, "✅ All users are active or this is not a group chat.")

@config.dc_cli.on(events.NewMessage(command="/slap"))
def slap_command(bot, accid, event, skip_cooldown: bool = False):
    msg = event.msg
    now = time.time()
    if not dc_helpers._is_dc_admin(bot, accid, msg.from_id) and not skip_cooldown:
        last_slap = state._chat_slap_anti_spam.get(msg.chat_id, 0)
        diff = now - last_slap
        if diff < config.SLAP_COOLDOWN_SECONDS:
            remaining_sec = max(1, int(config.SLAP_COOLDOWN_SECONDS - diff))
            dc_helpers._queue_delayed_command(bot, accid, msg, "slap", remaining_sec, slap_command, bot, accid, event, skip_cooldown=True)
            return

    state._chat_slap_anti_spam[msg.chat_id] = now
    query = event.payload.strip() if event.payload else ""
    
    # Check if this is a reply to another message
    target_contact_id = None
    target_msg_id = None
    if hasattr(msg, "quote") and msg.quote and isinstance(msg.quote, dict):
        quote_msg_id = msg.quote.get('message_id')
        if quote_msg_id:
            try:
                quoted_msg = bot.rpc.get_message(accid, quote_msg_id)
                if quoted_msg.from_id > 9 or quoted_msg.from_id == config.DC_CONTACT_ID_SELF:
                    target_contact_id = quoted_msg.from_id
                    target_msg_id = quote_msg_id
            except Exception:
                pass

    if not query and not target_msg_id:
        dc_helpers._send(bot, accid, msg.chat_id, "Usage: /slap <username> (or reply to a message)", reply_to_id=msg.id)
        return

    found_msg_id = target_msg_id
    matched_contact = None

    if target_contact_id:
        try:
            matched_contact = bot.rpc.get_contact(accid, target_contact_id)
        except Exception as e:
            config.logger.error(f"Failed to get contact for slap quote target {target_contact_id}: {e}")

    if not matched_contact and query:
        clean_query = query.lstrip('@').lower()
        try:
            msg_ids = bot.rpc.get_message_ids(accid, msg.chat_id, False, False)
        except Exception as e:
            config.logger.error(f"Failed to get message IDs for chat {msg.chat_id}: {e}")
            dc_helpers._send(bot, accid, msg.chat_id, "❌ Failed to retrieve messages in this chat.")
            return

        for msg_id in reversed(msg_ids[-1000:]):
            try:
                m = bot.rpc.get_message(accid, msg_id)
                if m.from_id <= 9 and m.from_id != config.DC_CONTACT_ID_SELF:
                    continue
                
                contact = bot.rpc.get_contact(accid, m.from_id)
                contact_name = (contact.name or "").lower()
                contact_display = (contact.display_name or "").lower()
                contact_address = (contact.address or "").lower()
                contact_id_str = str(m.from_id)

                if (clean_query == contact_id_str or
                    clean_query in contact_name or
                    clean_query in contact_display or
                    clean_query in contact_address.split('@')[0] or
                    clean_query in contact_address):
                    
                    found_msg_id = msg_id
                    matched_contact = contact
                    break
            except Exception as e:
                config.logger.error(f"Error checking message {msg_id} in /slap: {e}")
                continue

    try:
        sender_contact = bot.rpc.get_contact(accid, msg.from_id)
        sender_name = sender_contact.name or sender_contact.display_name or sender_contact.address or "Unknown"
    except Exception:
        sender_name = "Unknown"

    if found_msg_id and matched_contact:
        target_name = matched_contact.name or matched_contact.display_name or matched_contact.address or query
        reply_text = f"_{sender_name} slaps {target_name} around a bit with a large trout_"
        dc_helpers._send(bot, accid, msg.chat_id, reply_text, reply_to_id=found_msg_id)
    else:
        if query:
            reply_text = f"_{sender_name} slaps themself around a bit with a large trout_"
            dc_helpers._send(bot, accid, msg.chat_id, reply_text)
        else:
            dc_helpers._send(bot, accid, msg.chat_id, "Usage: /slap <username> (or reply to a message)", reply_to_id=msg.id)

@config.dc_cli.on(events.NewMessage(command="/me"))
def me_command(bot, accid, event):
    msg = event.msg
    query = event.payload.strip() if event.payload else ""
    if not query:
        return

    try:
        sender_contact = bot.rpc.get_contact(accid, msg.from_id)
        sender_name = sender_contact.name or sender_contact.display_name or sender_contact.address or "Unknown"
    except Exception:
        sender_name = "Unknown"

    reply_text = f"_{sender_name} {query}_"
    dc_helpers._send(bot, accid, msg.chat_id, reply_text)

@config.dc_cli.on(events.NewMessage(command="/away"))
def away_command(bot, accid, event):
    msg = event.msg
    text = event.payload.strip() if event.payload else ""
    
    try:
        sender_contact = bot.rpc.get_contact(accid, msg.from_id)
        sender_name = sender_contact.name or sender_contact.display_name or sender_contact.address or "Unknown"
    except Exception:
        sender_name = "Unknown"

    if text:
        database.set_away_status(msg.from_id, text)
        reply_text = f"_{sender_name} is now away: {text}_"
        dc_helpers._send(bot, accid, msg.chat_id, reply_text)
    else:
        away_info = database.get_away_status_details(msg.from_id)
        if away_info:
            current_status, _ = away_info
            dc_helpers._send(bot, accid, msg.chat_id, f"ℹ️ You are currently away: _{current_status}_\n\nTo update your status, use `/away <message>`.\nTo clear your status and mark yourself back, use `/back`.")
        else:
            dc_helpers._send(bot, accid, msg.chat_id, "ℹ️ You are not currently away.\n\nUsage: `/away <message>` — set your away status.\nExample: `/away at lunch until 2pm`")

@config.dc_cli.on(events.NewMessage(command="/back"))
def back_command(bot, accid, event):
    msg = event.msg
    
    try:
        sender_contact = bot.rpc.get_contact(accid, msg.from_id)
        sender_name = sender_contact.name or sender_contact.display_name or sender_contact.address or "Unknown"
    except Exception:
        sender_name = "Unknown"

    try:
        recipients = database.get_notified_recipients(msg.from_id)
    except Exception as e:
        config.logger.error(f"Error getting notified recipients: {e}")
        recipients = []

    database.remove_away_status(msg.from_id)
    reply_text = f"_{sender_name} is back_"
    dc_helpers._send(bot, accid, msg.chat_id, reply_text)

    # Notify recipients in PM
    for recipient_id in recipients:
        try:
            private_chat_id = bot.rpc.create_chat_by_contact_id(accid, recipient_id)
            dc_helpers._send(bot, accid, private_chat_id, f"_{sender_name} is back_")
        except Exception as e:
            config.logger.error(f"Error notifying recipient {recipient_id} that sender is back: {e}")

@config.dc_cli.on(events.NewMessage(command="/search"))
def search_command(bot, accid, event, skip_cooldown: bool = False):
    msg = event.msg
    
    # 1. Check if this is a global search (admin in private chat) or a local group search
    try:
        chat = bot.rpc.get_basic_chat_info(accid, msg.chat_id)
        chat_type = chat.get('chat_type', 'Single') if isinstance(chat, dict) else getattr(chat, 'chat_type', 'Single')
    except Exception as e:
        config.logger.error(f"Failed to get chat info for {msg.chat_id}: {e}")
        chat_type = 'Single'

    is_admin = dc_helpers._is_dc_admin(bot, accid, msg.from_id)
    
    global_search = False
    if str(chat_type) not in {"Group", "Mailinglist", "OutBroadcast", "InBroadcast"}:
        if not is_admin:
            dc_helpers._send(bot, accid, msg.chat_id, "ℹ️ Global search is only available for the bot administrator.")
            return
        global_search = True

    self_addr = ""
    try:
        self_addr = (bot.rpc.get_config(accid, "configured_addr") or bot.rpc.get_config(accid, "addr") or "").lower().strip()
    except Exception:
        pass

    # 2. Check cooldown (admins/global searches are exempt)
    now = time.time()
    if not global_search and not is_admin and not skip_cooldown:
        last_search = state._chat_search_anti_spam.get(msg.chat_id, 0)
        diff = now - last_search
        if diff < config.SEARCH_COOLDOWN_SECONDS:
            remaining_sec = max(1, int(config.SEARCH_COOLDOWN_SECONDS - diff))
            dc_helpers._queue_delayed_command(bot, accid, msg, "search", remaining_sec, search_command, bot, accid, event, skip_cooldown=True)
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
            dc_helpers._send(bot, accid, msg.chat_id, "ℹ️ No email addresses or domains found in the quoted message to search for.")
        else:
            dc_helpers._send(bot, accid, msg.chat_id, "Usage: /search <query1> <query2> ... (supports domains and partial matches, e.g. /search @testrun.org) or reply to a message containing email addresses or domains.")
        return

    # Update cooldown timestamp for local searches
    if not global_search:
        state._chat_search_anti_spam[msg.chat_id] = now

    # Gather list of chats to search in
    group_chats = []
    GROUP_TYPES = {"Group", "Mailinglist", "OutBroadcast", "InBroadcast"}
    
    if global_search:
        try:
            all_chats = bot.rpc.get_chatlist_entries(accid, None, None, None)
        except Exception as e:
            config.logger.error(f"Failed to get chatlist entries: {e}")
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
                config.logger.error(f"Failed to get basic chat info for chat {chat_id} in global search: {e}")
    else:
        # Local group search
        try:
            c_info = bot.rpc.get_basic_chat_info(accid, msg.chat_id)
            chat_name = c_info.get('name') if isinstance(c_info, dict) else getattr(c_info, 'name', f"Group {msg.chat_id}")
            group_chats.append((msg.chat_id, chat_name or f"Group {msg.chat_id}"))
        except Exception as e:
            config.logger.error(f"Failed to get basic chat info for local chat {msg.chat_id}: {e}")
            group_chats.append((msg.chat_id, f"Group {msg.chat_id}"))

    found_users_map = {}
    
    for chat_id, chat_name in group_chats:
        try:
            contacts = bot.rpc.get_chat_contacts(accid, chat_id)
        except Exception as e:
            config.logger.error(f"Failed to get chat contacts for chat {chat_id}: {e}")
            continue

        for contact_id in contacts:
            if contact_id == config.DC_CONTACT_ID_SELF:
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
                    age = dc_helpers._get_contact_age_indicator(contact_id)
                    badges = dc_helpers._get_user_badges(bot, accid, contact_id, contact=contact)

                    found_users_map[contact_id] = {
                        "name": name,
                        "addrs_str": addrs_str,
                        "status": status,
                        "age": age,
                        "badges": badges,
                        "groups": [chat_name]
                    }
            except Exception as e:
                config.logger.error(f"Error checking contact {contact_id} in search: {e}")

    queries_str = ", ".join(f"'{q}'" for q in queries)
    
    if found_users_map:
        if global_search:
            reply = f"🔍 **Global Search Results for {queries_str} ({len(found_users_map)}):**\n\n"
            for contact_id, info in found_users_map.items():
                groups_str = ", ".join(info["groups"])
                badge_str = f" {info['badges']}" if info.get('badges') else ""
                reply += f"• /contact{contact_id} {info['age']} **{info['name']}**{badge_str} ({info['addrs_str']}) [{info['status']}]\n  ↳ Groups: {groups_str}\n"
        else:
            reply = f"🔍 **Search Results for {queries_str} ({len(found_users_map)}):**\n\n"
            for contact_id, info in found_users_map.items():
                badge_str = f" {info['badges']}" if info.get('badges') else ""
                reply += f"• /contact{contact_id} {info['age']} **{info['name']}**{badge_str} ({info['addrs_str']}) [{info['status']}]\n"
        
        dc_helpers._send(bot, accid, msg.chat_id, reply)
    else:
        if global_search:
            dc_helpers._send(bot, accid, msg.chat_id, f"🔍 No members matching {queries_str} found in any group chats.")
        else:
            dc_helpers._send(bot, accid, msg.chat_id, f"🔍 No members matching {queries_str} found in this group.")

HELP_PRIVATE_NOTE = "\n\n💬 Sent privately because you asked in a group. Use /help@bouncer there to show it to everyone."


def _get_help_chat_id(bot, accid, msg):
    """Plain /help in a group is answered privately to the sender so several bots
    don't flood the group; /help@<bot> is still answered in the group itself."""
    cmd = msg.text.split(maxsplit=1)[0] if msg.text else ""
    if "@" in cmd:
        return msg.chat_id
    try:
        chat = bot.rpc.get_basic_chat_info(accid, msg.chat_id)
        chat_type = chat.get('chat_type', 'Single') if isinstance(chat, dict) else getattr(chat, 'chat_type', 'Single')
    except Exception:
        chat_type = 'Single'
    if str(chat_type) == "Single":
        return msg.chat_id
    return bot.rpc.create_chat_by_contact_id(accid, msg.from_id)


@config.dc_cli.on(events.NewMessage(command="/help"))
def help_command(bot, accid, event):
    msg = event.msg
    contact = bot.rpc.get_contact(accid, msg.from_id)
    sender_email = contact.address
    
    try:
        status_text = bot.rpc.get_config(accid, "selfstatus")
    except Exception:
        status_text = os.environ.get("STATUS_TEXT")
    if not status_text:
        status_text = f"I monitor groups and report inactive users (no activity for {config.INACTIVITY_DAYS_THRESHOLD} days)."

    help_text = (
        f"🤖 **Delta Chat Bouncer Bot v{config.VERSION}**\n\n"
        f"👋 Hi {sender_email}!\n\n"
        f"{status_text}\n\n"
        f"**Commands:**\n"
        f"/bounce [username] — Show user activity, or list members near auto-kick threshold in group.\n"
        f"/search [query1] ... — Search members by email/domain (e.g. @testrun.org) or reply to a message.\n"
        f"/relays — Find group members using public Russian mail providers (Yandex, Mail.ru, etc.).\n"
        f"/top    — Show top 10 posters in the last 24 hours.\n"
        f"/invite — Generate an invite link/QR code for this group.\n"
        f"/slap <username> — Reply to the user's last message with a trout slap.\n"
        f"/me <action> — Perform an IRC-style action (e.g. /me waves).\n"
        f"/away <text> — Set your away status, auto-notifying anyone who mentions you or replies.\n"
        f"/back — Clear your away status.\n"
        f"/contact<ID> — Share contact card for the given member ID.\n"
        f"/help   — This message.\n"
        f"/chats  — Show catalog of available chats.\n"
        f"/dchannels — Show catalog of available channels.\n"
        f"/cmpinglist — Show monitored servers.\n"
        f"/cmpingstatus [server] — Show monitoring results (optional filter).\n"
        f"/cmpingfail [server] — Show currently failed links (optional filter).\n"
        f"/cmpingevents [id] — Show CMPing incident log or incident details.\n"
        f"/cmpinghistory [server] — Show downtime history for monitored servers.\n"
        f"/cmping <server1> ... — Ping relays to/from specified servers.\n"
        f"/virus <url> — Scan URL or replied file/link with VirusTotal.\n"
        f"/sticker — Convert replied or attached image to a WebP sticker.\n"
        f"/stickernobg — Convert image to a sticker with background removed.\n\n"
        f"/donate — Support development ❤️\n\n"
        f"💡 _Commands have a 1-minute cooldown per group (15s for cmping/slap/stickernobg, 10s for search, 5s for sticker; admins are exempt)._\n\n"
        f"🤖 **Source:** Run your own bot: https://git.gluek.info/gluek/deltachat_bouncer"
    )
    
    admin_email = database.get_config("admin_dc_email")
    admin_fp = database.get_admin_fingerprint()
    is_actually_admin = dc_helpers._is_dc_admin(bot, accid, msg.from_id)
    
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
        help_text += "/autokick [on/off/days/ignore/unignore] — Auto-kick inactive members with warnings & fingerprint ignore\n"
        help_text += "/kick <user_id> — Remove a member from the current group\n"
        help_text += "/chatadd [desc] — Add current chat to catalog\n"
        help_text += "/chatremove — Remove current chat from catalog\n"
        help_text += "/chatdesc<ID> <text> — Update chat description\n"
        help_text += "/private <on/off> — Toggle privacy of current chat\n"
        help_text += "/welcome [on/off/...] — Manage welcome message for new members\n"
        help_text += "/dchanneladd <URL> — Join and add a channel to the catalog\n"
        help_text += "/dchannelremove [ID] — Remove channel from catalog\n"
        help_text += "/dchanneldesc<ID> <text> — Update channel description\n"
        help_text += "/dchannelpub<ID>on / /dchannelpub<ID>off — Toggle channel public catalog visibility\n"
        help_text += "/url [base_url] — Set/view base web URL for channel previews\n"
        help_text += "/cmpingadd <server> — Add server to connectivity monitoring\n"
        help_text += "/cmpingdel <server> — Remove server from monitoring\n"
        help_text += "/cmreport <on/off> — Toggle monitoring alerts in this chat"


    chat_id = _get_help_chat_id(bot, accid, msg)
    if chat_id != msg.chat_id:
        help_text += HELP_PRIVATE_NOTE
    dc_helpers._send(bot, accid, chat_id, help_text)

@config.dc_cli.on(events.NewMessage(command="/donate"))
def donate_command(bot, accid, event):
    msg = event.msg
    dc_helpers._send(bot, accid, msg.chat_id,
          "❤️ **Support Bot Development**\n\n"
          "If you find this bot useful, you can support its development:\n\n"
          "☕️ Ko-fi: https://ko-fi.com/gluek (🌍 world cards, paypal)\n"
          "🚀 Tribute: https://web.tribute.tg/d/IWb (🇷🇺 russian cards, SBP)\n\n"
          "Thank you! 🙏")

@config.dc_cli.on(events.NewMessage(command="/invite"))
def invite_command(bot, accid, event, skip_cooldown: bool = False):
    msg = event.msg
    
    # 1. Check if this is a group chat
    try:
        chat = bot.rpc.get_basic_chat_info(accid, msg.chat_id)
        chat_type = chat.get('chat_type', 'Single') if isinstance(chat, dict) else getattr(chat, 'chat_type', 'Single')
    except Exception as e:
        config.logger.error(f"Failed to get chat info for {msg.chat_id}: {e}")
        return

    if str(chat_type) not in {"Group", "Mailinglist", "OutBroadcast", "InBroadcast"}:
        dc_helpers._send(bot, accid, msg.chat_id, "❌ Invite links can only be generated for group chats.")
        return

    # 2. Check cooldown (admins are exempt)
    is_admin = dc_helpers._is_dc_admin(bot, accid, msg.from_id)
    now = time.time()
    
    if not is_admin and not skip_cooldown:
        last_check = state._chat_invite_anti_spam.get(msg.chat_id, 0)
        diff = now - last_check
        if diff < config.BOUNCE_COOLDOWN_SECONDS:
            remaining_sec = max(1, int(config.BOUNCE_COOLDOWN_SECONDS - diff))
            dc_helpers._queue_delayed_command(bot, accid, msg, "invite", remaining_sec, invite_command, bot, accid, event, skip_cooldown=True)
            return

    # Update cooldown timestamp
    state._chat_invite_anti_spam[msg.chat_id] = now

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
        sent_msg_id = None
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
            sent_msg_id = bot.rpc.send_msg(accid, msg.chat_id, MsgData(file=temp_path, text=invite_text))
            
            # Track success
            try:
                addr = bot.rpc.get_config(accid, "configured_addr") or bot.rpc.get_config(accid, "addr") or "unknown"
                if addr and isinstance(addr, str) and addr != "unknown":
                    database.increment_transport_sent(addr)
            except Exception:
                pass
        except Exception as qr_err:
            config.logger.warning(f"Could not generate QR image, falling back to text message: {qr_err}")
            sent_msg_id = dc_helpers._send(bot, accid, msg.chat_id, invite_text)
        finally:
            if temp_path and os.path.exists(temp_path):
                try:
                    os.unlink(temp_path)
                except Exception as e:
                    config.logger.warning(f"Failed to delete temp QR image file {temp_path}: {e}")
                    
        if is_private and sent_msg_id:
            database.update_catalog_chat_invite_link(msg.chat_id, qrdata, sent_msg_id)
                    
    except Exception as e:
        config.logger.error(f"Failed to generate invite: {e}")
        dc_helpers._send(bot, accid, msg.chat_id, "❌ Failed to generate invite link. Please try again later.")

@config.dc_cli.on(events.NewMessage(command="/relays"))
def relays_command(bot, accid, event, skip_cooldown: bool = False):
    """Check group members for regular mail providers, including secondary transports."""
    msg = event.msg
    
    # Allow everyone to use /relays, but with a cooldown (admins are exempt)
    is_admin = dc_helpers._is_dc_admin(bot, accid, msg.from_id)
    now = time.time()
    
    if not is_admin and not skip_cooldown:
        last_check = state._chat_relays_anti_spam.get(msg.chat_id, 0)
        diff = now - last_check
        if diff < config.BOUNCE_COOLDOWN_SECONDS:
            remaining_sec = max(1, int(config.BOUNCE_COOLDOWN_SECONDS - diff))
            dc_helpers._queue_delayed_command(bot, accid, msg, "relays", remaining_sec, relays_command, bot, accid, event, skip_cooldown=True)
            return

    # Update timestamp
    state._chat_relays_anti_spam[msg.chat_id] = now

    try:
        contacts = bot.rpc.get_chat_contacts(accid, msg.chat_id)
        found_users = []
        for contact_id in contacts:
            if contact_id == config.DC_CONTACT_ID_SELF:
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
                    if domain in config.REGULAR_MAIL_DOMAINS:
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
            dc_helpers._send(bot, accid, msg.chat_id, "✅ No group members using public Russian mail providers found.")
            return

        reply = f"⚠️ **Members with public Russian mail providers ({len(found_users)}):**\n\n"
        reply += "These users may experience delivery issues in large groups:\n\n"
        for user in found_users:
            matches_str = ", ".join(user["matches"])
            reply += f"• /contact{user['contact_id']} **{user['name']}** ({user['primary']}) [{user['last_seen']}] — {matches_str}\n"
        
        reply += "\nConsider asking them to use chatmail relays or private servers."
        dc_helpers._send(bot, accid, msg.chat_id, reply)

    except Exception as e:
        config.logger.error(f"Error in /relays command: {e}")
        dc_helpers._send(bot, accid, msg.chat_id, "❌ Failed to check members. Please try again.")

@config.dc_cli.on(events.NewMessage(command="/url"))
def url_command(bot, accid, event):
    """Set or view the base web preview URL. Admin only."""
    msg = event.msg
    if not dc_helpers._is_dc_admin(bot, accid, msg.from_id):
        dc_helpers._send(bot, accid, msg.chat_id, "❌ Only the bot administrator can use /url.")
        return

    payload = event.payload.strip() if event.payload else ""
    if not payload:
        raw_url = database.get_config("base_url") or os.getenv("BASE_URL") or ""
        current_url = raw_url.rstrip("/") if raw_url else "Not set"
        dc_helpers._send(bot, accid, msg.chat_id, f"🌐 Current base web URL: `{current_url}`\n\nTo set a new base URL, use:\n`/url https://channels.yourdomain.com`")
        return

    url = payload
    if not (url.startswith("http://") or url.startswith("https://")):
        dc_helpers._send(bot, accid, msg.chat_id, "❌ Invalid URL format. Please start with `http://` or `https://`.")
        return

    url = url.rstrip("/")
    database.set_config("base_url", url)
    web.routes.invalidate_channel_cache()
    dc_helpers._send(bot, accid, msg.chat_id, f"✅ Base web URL has been set to `{url}`.")

@config.dc_cli.on(events.NewMessage(command="/welcome"))
def welcome_command(bot, accid, event):
    msg = event.msg
    if not dc_helpers._is_dc_admin(bot, accid, msg.from_id):
        dc_helpers._send(bot, accid, msg.chat_id, "❌ Only the bot administrator can use this command.")
        return

    catalog_chat = database.get_catalog_chat_by_chat_id(msg.chat_id)
    if not catalog_chat:
        dc_helpers._send(bot, accid, msg.chat_id, "❌ Please add this chat to the catalog first using /chatadd.")
        return

    payload = event.payload.strip() if event.payload else ""
    if not payload:
        status = "enabled" if catalog_chat.get('welcome_enabled') else "disabled"
        add_text = catalog_chat.get('welcome_text') or ""
        text_suffix = f"\nAdditional text: \"{add_text}\"" if add_text else ""
        dc_helpers._send(bot, accid, msg.chat_id, f"ℹ️ Welcome greeting for new members is {status}.{text_suffix}")
        return

    payload_lower = payload.lower()
    if payload_lower == "off":
        database.update_catalog_chat_welcome(msg.chat_id, 0, None)
        dc_helpers._send(bot, accid, msg.chat_id, "✅ Welcome greeting for new members has been disabled.")
    elif payload_lower == "on" or payload_lower.startswith("on "):
        welcome_text = None
        if payload_lower.startswith("on "):
            welcome_text = payload[3:].strip()
            
        database.update_catalog_chat_welcome(msg.chat_id, 1, welcome_text)
        
        preview_text = f" {welcome_text}" if welcome_text else ""
        dc_helpers._send(bot, accid, msg.chat_id, 
              f"✅ Welcome greeting for new members has been enabled!\n"
              f"Example: 👋🏻 🔴 **Name**, welcome to {catalog_chat['name']} group!{preview_text}")
    else:
        dc_helpers._send(bot, accid, msg.chat_id, 
              "/welcome on <additional text> — enable greeting with additional text\n"
              "/welcome off — disable greeting")
