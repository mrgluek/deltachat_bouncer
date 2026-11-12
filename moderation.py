"""Chat moderation: inactivity/top-posters reporting, the auto-kick engine
(overview/candidates/warnings/perform), the hourly background monitor loop,
and their /autokick, /kick and /top command handlers.
"""
import re
import time
import threading
from datetime import datetime

from deltachat2 import events

import config
import database
import dc_helpers
import state

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
        config.logger.error(f"Error getting top posters for chat {chat_id}: {e}")
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
        except Exception:
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
        config.logger.error(f"Failed to get chat contacts for {chat_id}: {e}")
        return ""

    if len(contacts) <= 2:
        return "" # Ignore 1-on-1 chats and empty groups
        
    total_members = 0
    active_count = 0
    inactive_users = []
    lurkers_skipped = 0
    
    for contact_id in contacts:
        if contact_id == config.DC_CONTACT_ID_SELF:
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
                if now - monitored_since >= config.INACTIVITY_SECONDS_THRESHOLD:
                    inactive_users.append(f"• /contact{contact_id} **{name}** ({address}) [never seen]")
                else:
                    lurkers_skipped += 1
            else:
                inactive_duration = now - last_seen
                if inactive_duration > config.INACTIVITY_SECONDS_THRESHOLD:
                    days_ago = int(inactive_duration / (24 * 3600))
                    date_str = datetime.fromtimestamp(last_seen).strftime("%-d %b %Y")
                    inactive_users.append(f"• /contact{contact_id} **{name}** ({address}) [last seen {date_str}, {days_ago}d ago]")
                else:
                    active_count += 1
        except Exception as e:
            config.logger.error(f"Error checking contact {contact_id}: {e}")

    monitored_days = int((now - monitored_since) / (24 * 3600))
    
    if not inactive_users:
        if lurkers_skipped > 0:
            return f"ℹ️ **Inactivity Check**\nI've been monitoring this group for {monitored_days} days. {lurkers_skipped} members haven't spoken yet, but I need {config.INACTIVITY_DAYS_THRESHOLD} days of observation before reporting them as inactive."
        return ""
        
    # Build the report
    report = "⚠️ **Inactivity Report**\n"
    report += f"Monitoring this group for {monitored_days} days.\n\n"
    report += f"• Total members: {total_members}\n"
    report += f"• Active recently: {active_count}\n"
    
    if inactive_users:
        report += f"• ⚠️ Inactive (>{config.INACTIVITY_DAYS_THRESHOLD}d): {len(inactive_users)}\n\n"
        report += "\n".join(inactive_users)
    else:
        report += f"• Inactive (>{config.INACTIVITY_DAYS_THRESHOLD}d): 0\n"

    if lurkers_skipped > 0:
        report += f"\n\n_Note: {lurkers_skipped} more members haven't spoken yet, but they are still in the {config.INACTIVITY_DAYS_THRESHOLD}-day grace period._"

    return report

def _get_chat_autokick_overview(bot, accid, chat_id: int, days: int) -> dict:
    """Collect autokick statistics and candidates for chat_id.
    Returns a dict with:
    - 'monitored_days': int
    - 'total_members': int (eligible members inspected)
    - 'active_count': int (members with recent activity)
    - 'silent_count': int (never seen members still within observation window)
    - 'earliest_warn_days': int | None (minimum days until a silent member reaches warn threshold)
    - 'warn_candidates': list[dict]
    - 'kick_candidates': list[dict]
    - 'ignored_count': int
    - 'away_count': int
    """
    empty_result = {
        "monitored_days": 0,
        "total_members": 0,
        "active_count": 0,
        "silent_count": 0,
        "earliest_warn_days": None,
        "warn_candidates": [],
        "kick_candidates": [],
        "ignored_count": 0,
        "away_count": 0,
    }
    if days <= 0:
        return empty_result

    monitored_since = database.get_chat_monitored_since(chat_id)
    now = time.time()
    if monitored_since is None:
        monitored_since = now
        database.set_chat_monitored_since(chat_id, monitored_since)

    monitored_days = int((now - monitored_since) / (24 * 3600))
    empty_result["monitored_days"] = monitored_days

    try:
        contacts = bot.rpc.get_chat_contacts(accid, chat_id)
    except Exception as e:
        config.logger.error(f"Failed to get chat contacts for autokick in {chat_id}: {e}")
        return empty_result

    if len(contacts) <= 2:
        return empty_result

    warn_threshold_days = dc_helpers._get_chat_autokick_warn_threshold(days)
    warn_threshold_seconds = warn_threshold_days * 24 * 3600
    kick_threshold_seconds = days * 24 * 3600

    warn_candidates = []
    kick_candidates = []
    total_members = 0
    active_count = 0
    silent_count = 0
    earliest_warn_days = None
    ignored_count = 0
    away_count = 0

    for contact_id in contacts:
        if contact_id == config.DC_CONTACT_ID_SELF or contact_id <= 9:
            continue
        if dc_helpers._is_dc_admin(bot, accid, contact_id):
            continue

        try:
            contact = bot.rpc.get_contact(accid, contact_id)
            if contact.address and contact.address.lower() == "deltachat@system.local":
                continue

            total_members += 1

            # Exempt users whose cryptographic fingerprint is in the ignore list
            if dc_helpers._is_contact_autokick_ignored(bot, accid, contact_id, contact=contact):
                ignored_count += 1
                continue

            # Exempt users who currently have an active /away status
            if database.get_away_status(contact_id) is not None:
                away_count += 1
                continue

            if isinstance(contact, dict):
                last_seen = contact.get("last_seen", 0)
            else:
                last_seen = getattr(contact, "last_seen", 0)

            name = contact.name or contact.display_name or "Unknown"
            address = contact.address or "no_email@example.com"

            if last_seen == 0:
                first_seen = database.get_contact_first_seen(contact_id)
                if first_seen is not None:
                    inactive_duration = now - first_seen
                    days_ago = int(inactive_duration / (24 * 3600))
                    reason = f"never seen in {days_ago}d of observation"
                else:
                    inactive_duration = now - monitored_since
                    days_ago = int(inactive_duration / (24 * 3600))
                    reason = f"never seen in >{days_ago}d of observation"
            else:
                inactive_duration = now - last_seen
                days_ago = int(inactive_duration / (24 * 3600))
                reason = f"inactive for {days_ago}d (threshold: {days}d)"

            # If the user is currently active (less than warning threshold), clear any old warning
            if inactive_duration < warn_threshold_seconds:
                if database.get_autokick_warning(chat_id, contact_id) is not None:
                    database.clear_autokick_warning(chat_id, contact_id)

                if last_seen == 0:
                    silent_count += 1
                    days_left = max(1, warn_threshold_days - days_ago)
                    if earliest_warn_days is None or days_left < earliest_warn_days:
                        earliest_warn_days = days_left
                else:
                    active_count += 1
                continue

            candidate = {
                "id": contact_id,
                "name": name,
                "address": address,
                "reason": reason,
                "inactive_days": days_ago,
                "inactive_duration": inactive_duration
            }
            warn_candidates.append(candidate)

            # Only kick if inactive for >= days AND user has already received a warning at least 24h ago
            if inactive_duration >= kick_threshold_seconds:
                warned_at = database.get_autokick_warning(chat_id, contact_id)
                if warned_at is not None and (now - warned_at >= 86400):
                    kick_candidates.append(candidate)

        except Exception as e:
            config.logger.error(f"Error checking contact {contact_id} in autokick candidates: {e}")

    return {
        "monitored_days": monitored_days,
        "total_members": total_members,
        "active_count": active_count,
        "silent_count": silent_count,
        "earliest_warn_days": earliest_warn_days,
        "warn_candidates": warn_candidates,
        "kick_candidates": kick_candidates,
        "ignored_count": ignored_count,
        "away_count": away_count,
    }


def _get_chat_autokick_candidates(bot, accid, chat_id: int, days: int) -> tuple[list[dict], list[dict]]:
    """Inspect contacts in chat_id and return (warn_candidates, kick_candidates).
    - warn_candidates: inactive >= warn_threshold_days, not exempt.
    - kick_candidates: inactive >= days, not exempt, AND has received a warning at least 24h ago.
    """
    overview = _get_chat_autokick_overview(bot, accid, chat_id, days)
    return overview["warn_candidates"], overview["kick_candidates"]


def _perform_autokick_warnings_for_chat(bot, accid, chat_id: int, days: int, force_group: bool = False) -> list[dict]:
    """Check inactive members in chat_id and issue warnings:
    1. Send private 1-on-1 warning to newly detected candidates.
    2. Broadcast daily warning in group chat (once every 24h).
    Returns list of warning candidates.
    """
    if days <= 0:
        return []

    now = time.time()
    warn_candidates, _ = _get_chat_autokick_candidates(bot, accid, chat_id, days)
    if not warn_candidates:
        return []

    # 1. Send private 1-on-1 warning to each candidate who hasn't received one yet
    chat_name = "the group"
    try:
        chat_info = bot.rpc.get_basic_chat_info(accid, chat_id)
        chat_name = chat_info.get('name', 'the group') if isinstance(chat_info, dict) else getattr(chat_info, 'name', 'the group')
    except Exception:
        pass

    for c in warn_candidates:
        existing_warning = database.get_autokick_warning(chat_id, c["id"])
        if existing_warning is None:
            try:
                private_chat_id = bot.rpc.create_chat_by_contact_id(accid, c["id"])
                remaining = max(1, days - c["inactive_days"])
                dm_text = (
                    f"⚠️ **Inactivity Warning for \"{chat_name}\"**\n\n"
                    f"You have been inactive in **{chat_name}** for {c['inactive_days']} days. "
                    f"Inactive members are automatically removed after {days} days.\n\n"
                    f"You will be removed in approximately **{remaining} day(s)**. "
                    f"If you wish to remain in **{chat_name}**, please post a message in the group!"
                )
                msg_id = dc_helpers._send(bot, accid, private_chat_id, dm_text, priority=dc_helpers.PRIO_LOW, wait=True)
                if msg_id is None:
                    config.logger.error(f"Failed to send private autokick warning to contact {c['id']}: no message ID returned")
                    continue
                # The private warning starts the grace period once accepted by the core.
                database.record_autokick_warning(chat_id, c["id"], time.time())
                config.logger.info(f"Sent autokick DM warning to {c['name']} ({c['address']}, id={c['id']}) for chat {chat_id}")
            except Exception as dm_err:
                config.logger.error(f"Failed to send private autokick warning to contact {c['id']}: {dm_err}")

    # 2. Daily broadcast in the group chat (at most once every 24h)
    last_warn_at = database.get_chat_last_autokick_warn_at(chat_id)
    if force_group or (now - last_warn_at >= 86400):
        try:
            lines = []
            for c in warn_candidates:
                remaining = max(1, days - c["inactive_days"])
                lines.append(f"• **{c['name']}** ({c['address']}) — {c['reason']} ({remaining}d remaining)")

            group_msg = (
                f"⚠️ **Auto-kick Inactivity Warning:**\n"
                f"The following member(s) will be automatically removed due to inactivity (threshold: {days}d). "
                f"To remain in the group, please post a message here:\n\n" + "\n".join(lines)
            )
            msg_id = dc_helpers._send(bot, accid, chat_id, group_msg, priority=dc_helpers.PRIO_LOW, wait=True)
            if msg_id is None:
                config.logger.error(f"Failed to send daily autokick warning broadcast in chat {chat_id}: no message ID returned")
            else:
                database.set_chat_last_autokick_warn_at(chat_id, time.time())
        except Exception as e:
            config.logger.error(f"Failed to send daily autokick warning broadcast in chat {chat_id}: {e}")

    return warn_candidates


def _perform_autokick_for_chat(bot, accid, chat_id: int, days: int) -> list[dict]:
    """Check inactive members in chat_id and kick anyone inactive for >= days WHO HAS RECEIVED A WARNING.
    Returns a list of kicked member info dicts: [{'id': ..., 'name': ..., 'address': ..., 'reason': ...}].
    """
    if days <= 0:
        return []

    _, kick_candidates = _get_chat_autokick_candidates(bot, accid, chat_id, days)
    kicked_members = []

    for c in kick_candidates:
        try:
            config.logger.info(f"Auto-kicking member {c['name']} ({c['address']}, contact_id={c['id']}) from chat {chat_id}: {c['reason']}")
            bot.rpc.remove_contact_from_chat(accid, chat_id, c['id'])
            database.clear_autokick_warning(chat_id, c['id'])
            kicked_members.append(c)
        except Exception as kick_err:
            config.logger.error(f"Failed to auto-kick contact {c['id']} from chat {chat_id}: {kick_err}")

    if kicked_members:
        try:
            lines = [f"• **{m['name']}** ({m['address']}) — {m['reason']}" for m in kicked_members]
            notice = f"🧹 **Auto-kick:** Removed {len(kicked_members)} inactive member(s) (> {days}d inactive):\n\n" + "\n".join(lines)
            dc_helpers._send(bot, accid, chat_id, notice)
        except Exception as e:
            config.logger.error(f"Failed to send autokick notification in chat {chat_id}: {e}")

    return kicked_members


def _background_monitor_loop(bot, accid):
    config.logger.info("Background monitor task started.")
    time.sleep(10) # Wait for bot to connect and sync
    while True:
        try:
            # Reconcile bans on startup (after sync) and each hour, including unknown contact IDs.
            try:
                ban_sweep(bot, accid)
            except Exception as ban_err:
                config.logger.warning(f"Ban sweep failed: {ban_err}")

            try:
                chats = bot.rpc.get_chatlist_entries(accid, None, None, None)
                config.logger.info(f"Background check: tracking {len(chats)} chats")
            except Exception as e:
                config.logger.error(f"get_chatlist_entries failed: {e}")
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
                            config.logger.info(f"Started monitoring new group: {chat_id}")
                            database.set_chat_monitored_since(chat_id, time.time())

                        # Run autokick check if enabled for this group
                        autokick_days = database.get_chat_autokick(chat_id)
                        if autokick_days > 0:
                            _perform_autokick_warnings_for_chat(bot, accid, chat_id, autokick_days)
                            _perform_autokick_for_chat(bot, accid, chat_id, autokick_days)
                except Exception as e:
                    config.logger.error(f"Error checking chat {chat_id} in background monitor: {e}")
            
            # Refresh member counts for all catalog chats and channels
            _refresh_catalog_member_counts(bot, accid)

            # Periodic cleanup of old records and flush transport statistics
            try:
                database.flush_transport_stats()
                cleaned = database.cleanup_old_records(retention_days=30)
                if any(cleaned.values()):
                    config.logger.info(f"Database periodic cleanup completed: {cleaned}")
            except Exception as clean_err:
                config.logger.warning(f"Error during periodic DB cleanup: {clean_err}")

            # Prune anti-spam timestamps and unused domain locks
            try:
                dc_helpers._prune_anti_spam_dicts()
                bot_domains = set(dc_helpers._get_bot_domains(bot, accid))
                mon_domains = set(database.get_all_cmping_monitors())
                dc_helpers._prune_domain_locks(bot_domains | mon_domains)
            except Exception as prune_err:
                config.logger.warning(f"Error during memory pruning: {prune_err}")
            
        except Exception as e:
            config.logger.error(f"Background loop error: {e}")
            
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
                # Seed first_seen for all contacts in a single batch transaction
                valid_contacts = [c for c in contacts if c != 1]
                if valid_contacts:
                    database.ensure_contacts_first_seen_batch(valid_contacts, now)
                old_count = cat_chat.get('member_count', 0)
                if member_count != old_count:
                    database.update_catalog_chat_member_count(chat_id, member_count)
                    config.logger.info(f"Refreshed catalog chat {cat_chat['name']!r} member count: {old_count} -> {member_count}")
                    updated += 1
            except Exception as e:
                config.logger.error(f"Failed to refresh member count for catalog chat {cat_chat.get('name', '?')}: {e}")
    except Exception as e:
        config.logger.error(f"Failed to load catalog chats for member refresh: {e}")
    
    # Catalog channels
    try:
        catalog_channels = database.get_all_catalog_channels()
        for cat_chan in catalog_channels:
            try:
                chat_id = cat_chan['chat_id']
                contacts = bot.rpc.get_chat_contacts(accid, chat_id)
                now = time.time()
                member_count = sum(1 for c in contacts if c != 1)
                # Seed first_seen for all contacts in a single batch transaction
                valid_contacts = [c for c in contacts if c != 1]
                if valid_contacts:
                    database.ensure_contacts_first_seen_batch(valid_contacts, now)
                old_count = cat_chan.get('member_count', 0)
                if member_count != old_count:
                    database.update_catalog_channel_member_count(chat_id, member_count)
                    config.logger.info(f"Refreshed catalog channel {cat_chan['name']!r} member count: {old_count} -> {member_count}")
                    updated += 1
            except Exception as e:
                config.logger.error(f"Failed to refresh member count for catalog channel {cat_chan.get('name', '?')}: {e}")
    except Exception as e:
        config.logger.error(f"Failed to load catalog channels for member refresh: {e}")
    
    config.logger.info(f"Catalog member count refresh complete: {updated} updated")


@config.dc_cli.on(events.NewMessage(command="/autokick"))
def autokick_command(bot, accid, event):
    msg = event.msg
    if not dc_helpers._is_dc_admin(bot, accid, msg.from_id):
        dc_helpers._send(bot, accid, msg.chat_id, "⚠️ Only the bot administrator can configure auto-kick.")
        return

    try:
        chat = bot.rpc.get_basic_chat_info(accid, msg.chat_id)
        chat_type = chat.get('chat_type', 'Single') if isinstance(chat, dict) else getattr(chat, 'chat_type', 'Single')
    except Exception:
        chat_type = "Single"

    GROUP_TYPES = {"Group", "Mailinglist", "OutBroadcast", "InBroadcast"}
    if str(chat_type) not in GROUP_TYPES:
        dc_helpers._send(bot, accid, msg.chat_id, "⚠️ /autokick can only be used in group chats.")
        return

    payload = event.payload.strip() if event.payload else ""
    payload_lower = payload.lower()

    if not payload or payload_lower == "status":
        current_days = database.get_chat_autokick(msg.chat_id)
        ignored_count = len(database.get_all_autokick_ignored_fingerprints())
        monitored_since = database.get_chat_monitored_since(msg.chat_id)
        now = time.time()
        monitored_days = int((now - monitored_since) / 86400) if monitored_since else 0

        if current_days > 0:
            warn_threshold = dc_helpers._get_chat_autokick_warn_threshold(current_days)
            overview = _get_chat_autokick_overview(bot, accid, msg.chat_id, current_days)
            silent_count = overview["silent_count"]
            earliest_warn = overview["earliest_warn_days"]
            warn_count = len(overview["warn_candidates"])

            obs_detail = f"• Members under observation: **{silent_count} silent**"
            if silent_count > 0 and earliest_warn is not None:
                obs_detail += f" (earliest warning in **{earliest_warn} days**)"

            days_left = current_days - warn_threshold
            unit_str = f"<{days_left}d" if days_left > 1 else "<1d"

            status_reply = (
                f"🛡️ **Auto-kick is ON** for this group (threshold: **{current_days} days**).\n\n"
                f"• Group monitored for: **{overview['monitored_days']} days**\n"
                f"• Inactivity warning begins at: **{warn_threshold} days**\n"
                f"{obs_detail}\n"
                f"• In warning zone ({unit_str} to kick): **{warn_count}**\n"
                f"• Daily warning broadcast: active (once every 24h)\n"
                f"• Single private DM warning sent to inactive candidates\n"
                f"• Ignored members/bots: **{ignored_count}**\n\n"
                f"To change or disable:\n"
                f"• `/autokick <days>` (e.g. `/autokick 30`)\n"
                f"• `/autokick ignore <email/nick>` — exempt member by fingerprint\n"
                f"• `/autokick unignore <fingerprint/email/nick>` — remove exemption\n"
                f"• `/autokick off`"
            )
        else:
            status_reply = (
                f"🛡️ **Auto-kick is OFF** for this group.\n\n"
                f"• Group monitored for: **{monitored_days} days**\n"
                f"• Ignored members/bots: **{ignored_count}**\n\n"
                f"To enable:\n"
                f"• `/autokick on` (default: 90 days)\n"
                f"• `/autokick <days>` (e.g. `/autokick 30`)\n"
                f"• `/autokick ignore <email/nick>` — exempt member by fingerprint"
            )
        dc_helpers._send(bot, accid, msg.chat_id, status_reply)
        return

    if payload_lower.startswith("ignore"):
        parts = payload.split(maxsplit=1)
        if len(parts) == 1 or parts[1].strip().lower() in ("list", "show", "status"):
            ignored = database.get_all_autokick_ignored_fingerprints()
            if not ignored:
                dc_helpers._send(bot, accid, msg.chat_id, "🛡️ **Auto-kick ignore list is empty.**\n\nUse `/autokick ignore <email/nick/id>` to exempt a member by their cryptographic fingerprint.")
            else:
                lines = []
                for fp, note, _ in ignored:
                    note_str = f" **{note}** — " if note else ""
                    lines.append(f"• {note_str}`{fp[:8]}...{fp[-8:]}`")
                dc_helpers._send(bot, accid, msg.chat_id, f"🛡️ **Auto-kick Ignored Members/Bots ({len(ignored)}):**\n\n" + "\n".join(lines) + "\n\nUse `/autokick unignore <fingerprint/email/nick>` to remove.")
            return

        target = parts[1].strip()
        clean_target = target.replace(" ", "").replace(":", "").upper()
        if re.match(r'^[0-9A-F]{32,64}$', clean_target):
            database.add_autokick_ignored_fingerprint(clean_target, note="Direct fingerprint entry")
            dc_helpers._send(bot, accid, msg.chat_id, f"🛡️ Fingerprint `{clean_target}` added to auto-kick ignore list.")
            return

        matched_contacts = []
        try:
            contacts = bot.rpc.get_chat_contacts(accid, msg.chat_id)
        except Exception as e:
            config.logger.error(f"Failed to get chat contacts: {e}")
            contacts = []

        clean_q = target.lstrip('@').lower()
        seen_ids = set()
        for cid in contacts:
            if cid == config.DC_CONTACT_ID_SELF or cid <= 9:
                continue
            try:
                c = bot.rpc.get_contact(accid, cid)
                c_name = str(c.name).lower() if (hasattr(c, 'name') and isinstance(c.name, str)) else ""
                c_display = str(c.display_name).lower() if (hasattr(c, 'display_name') and isinstance(c.display_name, str)) else ""
                c_addr = str(c.address).lower() if (hasattr(c, 'address') and isinstance(c.address, str)) else ""
                if (clean_q == str(cid) or
                    (c_addr and (clean_q == c_addr or clean_q in c_addr.split('@')[0])) or
                    (c_name and clean_q in c_name) or
                    (c_display and clean_q in c_display)):
                    if cid not in seen_ids:
                        seen_ids.add(cid)
                        matched_contacts.append(c)
            except Exception:
                continue

        if not matched_contacts:
            dc_helpers._send(bot, accid, msg.chat_id, f"🔍 Participant '{target}' was not found in this group.")
            return

        if len(matched_contacts) > 1:
            candidates_str = ", ".join([f"**{c.name or c.address}** ({c.address})" for c in matched_contacts[:5]])
            dc_helpers._send(bot, accid, msg.chat_id, f"⚠️ Multiple participants matched '{target}': {candidates_str}. Please specify exact email or contact ID.")
            return

        target_contact = matched_contacts[0]
        c_fp = dc_helpers._get_contact_fingerprint(bot, accid, target_contact.id, contact=target_contact)
        c_name = target_contact.name if (hasattr(target_contact, 'name') and isinstance(target_contact.name, str)) else (target_contact.display_name if (hasattr(target_contact, 'display_name') and isinstance(target_contact.display_name, str)) else "User")
        c_addr = target_contact.address if (hasattr(target_contact, 'address') and isinstance(target_contact.address, str)) else "unknown"
        if not c_fp:
            dc_helpers._send(bot, accid, msg.chat_id, f"⚠️ Could not find a cryptographic fingerprint for **{c_name}** ({c_addr}). Ensure end-to-end encryption is established.")
            return

        note = f"{c_name} ({c_addr})"
        for fp in c_fp.upper().split(','):
            clean_fp = fp.strip()
            if clean_fp:
                database.add_autokick_ignored_fingerprint(clean_fp, note=note)

        dc_helpers._send(bot, accid, msg.chat_id, f"🛡️ Added **{c_name}** ({c_addr}) to auto-kick ignore list.\nFingerprint: `{c_fp}`")
        return

    if payload_lower.startswith("unignore"):
        parts = payload.split(maxsplit=1)
        if len(parts) < 2:
            dc_helpers._send(bot, accid, msg.chat_id, "⚠️ Usage: `/autokick unignore <fingerprint/email/nick>`")
            return

        target = parts[1].strip()
        clean_target = target.replace(" ", "").replace(":", "").upper()
        if database.remove_autokick_ignored_fingerprint(clean_target):
            dc_helpers._send(bot, accid, msg.chat_id, f"✅ Removed `{clean_target}` from auto-kick ignore list.")
            return

        ignored = database.get_all_autokick_ignored_fingerprints()
        found_fps = []
        target_lower = target.lower()
        for fp, note, _ in ignored:
            if target_lower in note.lower() or target_lower in fp.lower():
                found_fps.append((fp, note))

        if not found_fps:
            dc_helpers._send(bot, accid, msg.chat_id, f"🔍 No ignored entry found matching '{target}'.")
            return

        for fp, note in found_fps:
            database.remove_autokick_ignored_fingerprint(fp)

        removed_desc = ", ".join([f"**{note}** (`{fp[:8]}...`)" if note else f"`{fp[:8]}...`" for fp, note in found_fps])
        dc_helpers._send(bot, accid, msg.chat_id, f"✅ Removed from auto-kick ignore list: {removed_desc}")
        return

    if payload_lower in ("off", "disable", "stop", "0"):
        database.set_chat_autokick(msg.chat_id, 0)
        database.clear_chat_autokick_warnings(msg.chat_id)
        dc_helpers._send(bot, accid, msg.chat_id, "🛑 **Auto-kick disabled** for this group.")
        return

    if payload_lower in ("on", "enable"):
        default_days = 90
        database.set_chat_autokick(msg.chat_id, default_days)
        warn_threshold = dc_helpers._get_chat_autokick_warn_threshold(default_days)
        dc_helpers._send(bot, accid, msg.chat_id, 
              f"✅ **Auto-kick enabled** for this group with a threshold of **{default_days} days** (warnings start at **{warn_threshold} days**).\n\n"
              f"Members inactive for more than {default_days} days who have received a warning will be automatically removed.")
        return

    if payload.isdigit():
        days = int(payload)
        if days < 1 or days > 3650:
            dc_helpers._send(bot, accid, msg.chat_id, "⚠️ Please specify a valid number of days between 1 and 3650 (e.g. `/autokick 30`).")
            return
        database.set_chat_autokick(msg.chat_id, days)
        warn_threshold = dc_helpers._get_chat_autokick_warn_threshold(days)
        dc_helpers._send(bot, accid, msg.chat_id, 
              f"✅ **Auto-kick enabled** for this group with a threshold of **{days} days** (warnings start at **{warn_threshold} days**).\n\n"
              f"Members inactive for more than {days} days who have received a warning will be automatically removed.")
        return

    dc_helpers._send(bot, accid, msg.chat_id,
          "ℹ️ **Usage:**\n"
          "• `/autokick` — Show current auto-kick status\n"
          "• `/autokick on` — Enable with default 90-day threshold\n"
          "• `/autokick <days>` — Enable with custom threshold (e.g. `/autokick 30`)\n"
          "• `/autokick ignore <email/nick/id>` — Add member to ignore list by fingerprint\n"
          "• `/autokick unignore <fingerprint/email/nick>` — Remove from ignore list\n"
          "• `/autokick off` — Disable auto-kick")


def _match_members(bot, accid, member_ids, query: str) -> list[int]:
    """Members whose name, display name or address is `query`; if none is exactly that, the
    members that contain it. Case-insensitive; the bot and special contacts are skipped."""
    query = query.strip().lstrip("@").lower()
    if not query:
        return []
    exact, partial = [], []
    for cid in member_ids:
        if cid <= 9 or cid == config.DC_CONTACT_ID_SELF:
            continue
        try:
            c = bot.rpc.get_contact(accid, cid)
        except Exception:
            continue
        name, disp, addr = (c.name or "").lower(), (c.display_name or "").lower(), (c.address or "").lower()
        if query in (name, disp, addr, addr.split("@")[0]):
            exact.append(cid)
        elif query in name or query in disp or query in addr:
            partial.append(cid)
    return exact or partial


def _resolve_name_words(bot, accid, member_ids, words: list[str]):
    """Names typed after /kick or /ban -> ([(query, [matching contact ids])]).

    "faq bot" is one name, not "faq" and "bot" (the second would match every bot in the group):
    the words are tried together first, and only when no member matches the whole phrase is each
    word looked up on its own, for "/kick alice bob"."""
    if not words:
        return []
    if len(words) > 1:
        phrase = " ".join(words)
        hits = _match_members(bot, accid, member_ids, phrase)
        if hits:
            return [(phrase, hits)]
    return [(w, _match_members(bot, accid, member_ids, w)) for w in words]


def _name_of(bot, accid, cid) -> str:
    try:
        c = bot.rpc.get_contact(accid, cid)
        return f"{c.name or c.display_name or 'Unknown'} ({c.address})"
    except Exception:
        return f"ID {cid}"


@config.dc_cli.on(events.NewMessage(command="/kick"))
def kick_command(bot, accid, event):
    msg = event.msg
    if not dc_helpers._is_dc_admin(bot, accid, msg.from_id):
        dc_helpers._send(bot, accid, msg.chat_id, "⚠️ Only the bot administrator can use /kick.")
        return

    try:
        chat = bot.rpc.get_basic_chat_info(accid, msg.chat_id)
        chat_type = chat.get('chat_type', 'Single') if isinstance(chat, dict) else getattr(chat, 'chat_type', 'Single')
    except Exception as e:
        config.logger.error(f"Failed to get chat info for {msg.chat_id}: {e}")
        chat_type = 'Single'

    GROUP_TYPES = {"Group", "Mailinglist", "OutBroadcast", "InBroadcast"}
    if str(chat_type) not in GROUP_TYPES:
        dc_helpers._send(bot, accid, msg.chat_id, "⚠️ /kick can only be used in group chats.")
        return

    try:
        chat_contacts = bot.rpc.get_chat_contacts(accid, msg.chat_id)
    except Exception as e:
        config.logger.error(f"Failed to get chat contacts in /kick: {e}")
        dc_helpers._send(bot, accid, msg.chat_id, "❌ Failed to retrieve group members.")
        return

    chat_contact_set = set(chat_contacts)

    payload = event.payload.strip() if event.payload else ""
    target_contact_ids = []

    # 1. Check if replying to a message
    if hasattr(msg, "quote") and msg.quote and isinstance(msg.quote, dict):
        quote_msg_id = msg.quote.get('message_id')
        if quote_msg_id:
            try:
                quoted_msg = bot.rpc.get_message(accid, quote_msg_id)
                if quoted_msg.from_id > 9 and quoted_msg.from_id not in target_contact_ids:
                    target_contact_ids.append(quoted_msg.from_id)
            except Exception as e:
                config.logger.error(f"Failed to get quote message for /kick: {e}")

    # 2. Parse the payload: contact IDs, and names (a name may have several words)
    name_problems = []
    if payload:
        words = []
        for token in payload.split():
            cid_match = re.match(r'^(?:/?contact|c)?(\d+)$', token.strip(), re.IGNORECASE)
            if cid_match:
                cid = int(cid_match.group(1))
                if cid not in target_contact_ids:
                    target_contact_ids.append(cid)
            else:
                words.append(token.strip())
        # One match kicks; several are refused, nobody is removed on a guess
        for query, hits in _resolve_name_words(bot, accid, chat_contacts, words):
            if len(hits) == 1:
                if hits[0] not in target_contact_ids:
                    target_contact_ids.append(hits[0])
            elif hits:
                names = ", ".join(_name_of(bot, accid, h) for h in hits[:5])
                name_problems.append(f"• `{query[:40]}` matches {len(hits)} members ({names}); use the contact ID or the address.")
            else:
                name_problems.append(f"• `{query[:40]}`: nobody in this group.")

    if not target_contact_ids and name_problems:
        dc_helpers._send(bot, accid, msg.chat_id, "⚠️ **Could not kick:**\n" + "\n".join(name_problems))
        return
    if not target_contact_ids:
        dc_helpers._send(bot, accid, msg.chat_id,
              "ℹ️ **Usage:**\n"
              "• `/kick <user_id>` (e.g. `/kick 123` or `/kick /contact123`)\n"
              "• `/kick <user1> <user2> ...`\n"
              "• Reply to any user's message with `/kick`")
        return

    kicked_success = []
    kicked_failed = list(name_problems)

    for cid in target_contact_ids:
        if cid == config.DC_CONTACT_ID_SELF or cid <= 9:
            kicked_failed.append(f"• ID {cid}: The bot cannot kick itself.")
            continue
        if dc_helpers._is_dc_admin(bot, accid, cid):
            kicked_failed.append(f"• ID {cid}: Cannot kick the bot administrator.")
            continue
        if cid not in chat_contact_set:
            kicked_failed.append(f"• ID {cid}: User is not a member of this group.")
            continue

        try:
            contact = bot.rpc.get_contact(accid, cid)
            name = contact.name or contact.display_name or "Unknown"
            address = contact.address or "no_email"
            bot.rpc.remove_contact_from_chat(accid, msg.chat_id, cid)
            kicked_success.append(f"• **{name}** ({address}) [ID: {cid}]")
        except Exception as e:
            config.logger.error(f"Failed to kick contact {cid} from chat {msg.chat_id}: {e}")
            kicked_failed.append(f"• ID {cid}: Failed to remove from group.")

    reply_parts = []
    if kicked_success:
        reply_parts.append(f"👞 **Kicked {len(kicked_success)} member(s):**\n" + "\n".join(kicked_success))
    if kicked_failed:
        reply_parts.append(f"⚠️ **Could not kick:**\n" + "\n".join(kicked_failed))

    dc_helpers._send(bot, accid, msg.chat_id, "\n\n".join(reply_parts))


# ── /ban, /unban: people kept out of every group the bot is in ──

BAN_CHAT_TYPES = {"Group"}
BAN_RESOLVE_LIMIT = 64
BAN_RESOLVE_PAUSE = 0.1
BAN_JOIN_RETRY_SECONDS = 5
_ban_fingerprint_cache: dict[tuple[int, int], list[str]] = {}
_ban_scan_offset: dict[int, int] = {}
_ban_retry_pending: set[tuple[int, int, int | None]] = set()
_ban_lock = threading.RLock()
_FP_TOKEN_RE = re.compile(r"^(?:[0-9A-F]{40}|[0-9A-F]{64})$")


def _fingerprints(bot, accid, contact_id) -> list[str]:
    """The contact's key fingerprints (normally one), upper case; [] without a key."""
    try:
        fp = dc_helpers._get_contact_fingerprint(bot, accid, contact_id)
    except Exception as e:
        config.logger.debug(f"No fingerprint for contact {contact_id}: {e}")
        return []
    return [x.strip() for x in (fp or "").upper().split(",") if x.strip()]


def _group_chat_ids(bot, accid, chat_ids=None) -> list[int]:
    try:
        chats = chat_ids if chat_ids is not None else bot.rpc.get_chatlist_entries(accid, None, None, None)
    except Exception as e:
        config.logger.error(f"get_chatlist_entries failed in ban sweep: {e}")
        return []
    ids = []
    for chat_id in chats:
        if not isinstance(chat_id, int):
            continue
        try:
            info = bot.rpc.get_basic_chat_info(accid, chat_id)
            ctype = info.get("chat_type", "Single") if isinstance(info, dict) else getattr(info, "chat_type", "Single")
        except Exception:
            continue
        if str(ctype) in BAN_CHAT_TYPES:
            ids.append(chat_id)
    return ids


def _remove_from(bot, accid, chat_id, contact_id) -> bool:
    try:
        bot.rpc.remove_contact_from_chat(accid, chat_id, contact_id)
        return True
    except Exception as e:
        config.logger.error(f"Could not remove banned contact {contact_id} from chat {chat_id}: {e}")
        return False


def ban_sweep(bot, accid, deep_fps: set | None = None, chat_ids=None) -> int:
    """Reconcile group members, caching resolved keys for the process lifetime.

    Periodic scans resolve at most BAN_RESOLVE_LIMIT uncached contacts per run, rotating
    past unresolved contacts. Explicit /ban and bot-join scans check the whole group.
    """
    banned = database.get_all_banned()
    if not banned:
        return 0
    known_ids = {b["contact_id"] for b in banned if b["contact_id"]}
    fps = deep_fps if deep_fps is not None else {b["fingerprint"] for b in banned}
    groups = _group_chat_ids(bot, accid, chat_ids)
    memberships = {}
    for chat_id in groups:
        try:
            memberships[chat_id] = list(bot.rpc.get_chat_contacts(accid, chat_id))
        except Exception:
            continue
    contacts = list(dict.fromkeys(cid for members in memberships.values() for cid in members
                                 if cid > 9 and cid != config.DC_CONTACT_ID_SELF))
    with _ban_lock:
        pending = [cid for cid in contacts if cid not in known_ids and (accid, cid) not in _ban_fingerprint_cache]
        offset = _ban_scan_offset.get(accid, 0) % len(pending) if pending else 0
        pending = pending[offset:] + pending[:offset]
        limit = len(pending) if deep_fps is not None or chat_ids is not None else BAN_RESOLVE_LIMIT
        selected = pending[:limit]
        _ban_scan_offset[accid] = offset + len(selected)
    for index, cid in enumerate(selected):
        if index and index % BAN_RESOLVE_LIMIT == 0:
            time.sleep(BAN_RESOLVE_PAUSE)
        resolved = _fingerprints(bot, accid, cid)
        if resolved:
            with _ban_lock:
                _ban_fingerprint_cache[accid, cid] = resolved
    with _ban_lock:
        cached = {cid: _ban_fingerprint_cache.get((accid, cid), []) for cid in contacts}
    for cid, resolved in cached.items():
        for fp in resolved:
            if fp in fps:
                database.add_banned_fingerprint(fp, contact_id=cid)
                known_ids.add(cid)
    removed = 0
    for chat_id, members in memberships.items():
        for cid in members:
            if cid > 9 and cid != config.DC_CONTACT_ID_SELF and cid in known_ids:
                if not dc_helpers._is_dc_admin(bot, accid, cid) and _remove_from(bot, accid, chat_id, cid):
                    removed += 1
    return removed


def _retry_ban_join(bot, accid, chat_id, contact_id):
    key = (accid, chat_id, contact_id)
    with _ban_lock:
        if key in _ban_retry_pending:
            return
        _ban_retry_pending.add(key)

    def retry():
        try:
            enforce_ban_on_join(bot, accid, chat_id, contact_id, _retry=False)
        except Exception as e:
            config.logger.warning(f"Delayed ban check failed in chat {chat_id}: {e}")
        finally:
            with _ban_lock:
                _ban_retry_pending.discard(key)

    timer = threading.Timer(BAN_JOIN_RETRY_SECONDS, retry)
    timer.daemon = True
    timer.start()


def enforce_ban_on_join(bot, accid, chat_id, contact_id, _retry=True) -> bool:
    """Check joins (including the bot's own), retrying a missing identity once."""
    if not database.count_banned():
        return False
    info = bot.rpc.get_basic_chat_info(accid, chat_id)
    ctype = info.get("chat_type", "Single") if isinstance(info, dict) else getattr(info, "chat_type", "Single")
    if str(ctype) not in BAN_CHAT_TYPES:
        return False
    if contact_id == config.DC_CONTACT_ID_SELF:
        fps = {b["fingerprint"] for b in database.get_all_banned()}
        ban_sweep(bot, accid, deep_fps=fps, chat_ids=[chat_id])
        return False
    if not contact_id:
        if _retry:
            _retry_ban_join(bot, accid, chat_id, None)
        else:
            ban_sweep(bot, accid, deep_fps={b["fingerprint"] for b in database.get_all_banned()},
                      chat_ids=[chat_id])
        return False
    if contact_id <= 9:
        return False
    fps = _fingerprints(bot, accid, contact_id)
    if not fps:
        if _retry:
            _retry_ban_join(bot, accid, chat_id, contact_id)
        return False
    with _ban_lock:
        _ban_fingerprint_cache[accid, contact_id] = fps
    for fp in fps:
        if database.is_fingerprint_banned(fp):
            if dc_helpers._is_dc_admin(bot, accid, contact_id):
                return False
            database.add_banned_fingerprint(fp, contact_id=contact_id)
            config.logger.info(f"Banned contact {contact_id} joined chat {chat_id}: removing")
            return _remove_from(bot, accid, chat_id, contact_id)
    return False


def _describe(bot, accid, contact_id) -> tuple[str, str]:
    try:
        c = bot.rpc.get_contact(accid, contact_id)
        return (c.name or c.display_name or "Unknown"), (c.address or "")
    except Exception:
        return "Unknown", ""


def _ban_targets(bot, accid, msg, payload):
    """-> ([(fingerprint, contact_id | None, name, addr)], [problem lines]) for the /ban arguments."""
    found, problems = [], []

    def add_contact(cid):
        if cid <= 9 or cid == config.DC_CONTACT_ID_SELF:
            problems.append(f"• ID {cid}: not a person the bot can ban.")
            return
        name, addr = _describe(bot, accid, cid)
        if dc_helpers._is_dc_admin(bot, accid, cid):
            problems.append(f"• {name} ({addr}): cannot ban the bot administrator.")
            return
        fps = _fingerprints(bot, accid, cid)
        if not fps:
            problems.append(f"• {name} ({addr}): no key fingerprint known (no end-to-end encrypted contact yet).")
            return
        for fp in fps:
            found.append((fp, cid, name, addr))

    quote = getattr(msg, "quote", None)
    if quote and isinstance(quote, dict) and quote.get("message_id"):
        try:
            add_contact(bot.rpc.get_message(accid, quote["message_id"]).from_id)
        except Exception as e:
            config.logger.error(f"Failed to read the quoted message for /ban: {e}")

    try:
        members = [c for c in bot.rpc.get_chat_contacts(accid, msg.chat_id) if c > 9]
    except Exception:
        members = []
    words = []
    for tok in payload.split():
        clean = tok.replace(":", "").upper()
        cid_match = re.match(r"^(?:/?contact|c)?(\d+)$", tok, re.IGNORECASE)
        if _FP_TOKEN_RE.match(clean):
            found.append((clean, None, "", ""))
        elif cid_match:
            add_contact(int(cid_match.group(1)))
        else:
            words.append(tok)
    for query, hits in _resolve_name_words(bot, accid, members, words):
        if not hits:  # not in this chat: the core's contact search
            try:
                hits = [c for c in bot.rpc.get_contact_ids(accid, 0, query.lstrip("@")) if c > 9]
            except Exception:
                hits = []
        if len(hits) == 1:
            add_contact(hits[0])
        elif hits:
            problems.append(f"• `{query[:40]}` matches {len(hits)} contacts; use a contact ID or the address.")
        else:
            problems.append(f"• `{query[:40]}`: nobody found.")
    return found, problems


def _ban_list_text() -> str:
    banned = database.get_all_banned(1000)
    total = database.count_banned()
    if not banned:
        return "🚫 The ban list is empty."
    lines = []
    for b in banned:
        who = f"**{b['name']}** ({b['addr']}) " if (b["name"] or b["addr"]) else ""
        lines.append(f"• {who}`{b['fingerprint'][:8]}…{b['fingerprint'][-8:]}`")
    if total > len(banned):
        lines.append(f"… and {total - len(banned)} more")
    return f"🚫 **Banned ({total}):**\n" + "\n".join(lines) + "\n\n`/unban <address|ID|fingerprint start>` lifts a ban."


@config.dc_cli.on(events.NewMessage(command="/ban"))
def ban_command(bot, accid, event):
    msg = event.msg
    if not dc_helpers._is_dc_admin(bot, accid, msg.from_id):
        dc_helpers._send(bot, accid, msg.chat_id, "⚠️ Only the bot administrator can use /ban.")
        return
    payload = (event.payload or "").strip()
    if payload.lower() in ("", "list") and not getattr(msg, "quote", None):
        dc_helpers._send(bot, accid, msg.chat_id,
                         _ban_list_text() + "\n\nUsage: `/ban <user|address|contact ID|fingerprint>` or reply with `/ban`. "
                         "The person is removed from every group the bot is in and cannot join "
                         "through the bot's invites again.")
        return

    found, problems = _ban_targets(bot, accid, msg, payload)
    new, fps = [], set()
    for fp, cid, name, addr in found:
        fps.add(fp)
        if database.add_banned_fingerprint(fp, contact_id=cid, name=name, addr=addr):
            new.append((fp, name, addr))
    removed = ban_sweep(bot, accid, deep_fps=fps) if fps else 0

    parts = []
    if fps:
        who = ", ".join(f"**{n or 'fingerprint'}**" + (f" ({a})" if a else "") if (n or a) else f"`{fp[:8]}…`"
                        for fp, n, a in new) or "already on the list"
        parts.append(f"🚫 Banned: {who}. Removed from {removed} group(s); they cannot join the groups the bot invites to.")
    if problems:
        parts.append("⚠️ **Not banned:**\n" + "\n".join(problems))
    dc_helpers._send(bot, accid, msg.chat_id, "\n\n".join(parts) or "ℹ️ Nobody to ban.")


@config.dc_cli.on(events.NewMessage(command="/unban"))
def unban_command(bot, accid, event):
    msg = event.msg
    if not dc_helpers._is_dc_admin(bot, accid, msg.from_id):
        dc_helpers._send(bot, accid, msg.chat_id, "⚠️ Only the bot administrator can use /unban.")
        return
    q = (event.payload or "").strip().lower().lstrip("@")
    if not q:
        dc_helpers._send(bot, accid, msg.chat_id, "Usage: `/unban <address|contact ID|fingerprint start>`; `/ban` shows the list.")
        return
    qfp = q.replace(":", "").upper()
    matches = [b for b in database.get_all_banned()
               if (len(qfp) >= 8 and b["fingerprint"].startswith(qfp))
               or q == b["addr"].lower() or q == str(b["contact_id"]) or (len(q) >= 3 and q in b["name"].lower())]
    if not matches:
        dc_helpers._send(bot, accid, msg.chat_id, f"🔍 No ban matches `{q[:60]}`.")
        return
    if len(matches) > 1:
        dc_helpers._send(bot, accid, msg.chat_id, f"⚠️ {len(matches)} bans match `{q[:60]}`; be more specific (address or longer fingerprint).")
        return
    b = matches[0]
    database.remove_banned_fingerprint(b["fingerprint"])
    dc_helpers._send(bot, accid, msg.chat_id, f"✅ Unbanned {b['name'] or b['addr'] or b['fingerprint'][:8] + '…'}. They are not added back to any group.")


@config.dc_cli.on(events.NewMessage(command="/top"))
def top_command(bot, accid, event, skip_cooldown: bool = False):
    msg = event.msg
    # 1-minute cooldown for /top
    last_check = state._chat_top_anti_spam.get(msg.chat_id, 0)
    now = time.time()
    
    if not dc_helpers._is_dc_admin(bot, accid, msg.from_id) and not skip_cooldown:
        diff = now - last_check
        if diff < config.BOUNCE_COOLDOWN_SECONDS:
            remaining_sec = max(1, int(config.BOUNCE_COOLDOWN_SECONDS - diff))
            dc_helpers._queue_delayed_command(bot, accid, msg, "top", remaining_sec, top_command, bot, accid, event, skip_cooldown=True)
            return
    
    state._chat_top_anti_spam[msg.chat_id] = now
    dc_helpers._send(bot, accid, msg.chat_id, _get_top_posters_report(bot, accid, msg.chat_id))
