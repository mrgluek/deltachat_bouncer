"""Catalog submissions by regular users: eligibility checks, red flags,
auto-approval, the admin review card, approve/reject/ban, and reports.

The admin path of /chatadd and /dchanneladd is unchanged (channels.py). A
non-admin submission lands here and is pending until reviewed, unless the
submitter is old enough (CATALOG_AUTO_APPROVE_DAYS) and nothing looks suspicious.

Dynamic commands handled here (see handle_dynamic_command):
  /chatapprove<ID>  /chatreject<ID> [reason]  /chatban<ID> [reason]  /chatreport<ID> [reason]
  /dchannelapprove<ID>  /dchannelreject<ID> [reason]  /dchannelban<ID> [reason]
  /dchannelreport<ID> [reason]  /dchannelweb<ID>on|off
"""
import os
import re
import shutil
import threading
import time

from deltachat2 import events

import config
import database
import dc_helpers
import state
import virustotal
import web.routes

KIND_CHAT = "chat"
KIND_CHANNEL = "channel"

DAY = 24 * 3600
VT_MAX_URLS = 3  # each URL costs up to ~5 VirusTotal requests at 15 s apart
_URL_RE = re.compile(r'https?://[^\s<>"]+', re.IGNORECASE)

# Submitters whose channel join is still running. The submission is logged only
# after the join (the catalog id is not known before), so without this a burst of
# /dchanneladd would pass the per-user limits several times.
_inflight_lock = threading.Lock()
_inflight_submitters: set[int] = set()

# Securejoin link kinds a user may submit as a channel. Contact links are admin-only:
# they lead to a person, not to a channel.
USER_CHANNEL_KINDS = (
    "askverifygroup", "askjoinbroadcast", "withdrawverifygroup", "withdrawjoinbroadcast",
    "reviveverifygroup", "revivejoinbroadcast",
)


# ── Settings and small helpers ──

def submissions_enabled() -> bool:
    """/catalogsubmit on|off (database) wins over CATALOG_SUBMISSIONS (env)."""
    value = database.get_config("catalog_submissions")
    if value in ("on", "off"):
        return value == "on"
    return config.CATALOG_SUBMISSIONS_ENABLED


def contact_age_days(contact_id: int) -> float:
    """Days since the bot first saw this contact; a contact seen for the first time is 0 days old."""
    now = time.time()
    first_seen = database.get_contact_first_seen(contact_id)
    if first_seen is None:
        database.ensure_contact_first_seen(contact_id, now)
        return 0.0
    return max(0.0, (now - first_seen) / DAY)


def _contact_label(bot, accid, contact_id) -> tuple[str, str]:
    """(display text for the admin, lowercase address)."""
    try:
        c = bot.rpc.get_contact(accid, contact_id)
        addr = (getattr(c, "address", "") or "").lower()
        name = getattr(c, "display_name", None) or getattr(c, "name", None) or addr
    except Exception:
        return f"contact {contact_id}", ""
    return (f"{name} ({addr})" if name and name.lower() != addr else addr), addr


def _admin_chat_id(bot, accid):
    """The bot's 1:1 chat with the admin, or None when no admin is configured."""
    admin_email = database.get_config("admin_dc_email")
    if not admin_email:
        return None
    try:
        contact_id = dc_helpers.admin_key_contact_id(bot, accid)
        if not contact_id:
            return None
        return bot.rpc.create_chat_by_contact_id(accid, contact_id)
    except Exception as e:
        config.logger.warning(f"Catalog: cannot open the admin chat: {e}")
        return None


def _notify_admin(bot, accid, text):
    chat_id = _admin_chat_id(bot, accid)
    if chat_id:
        dc_helpers._send(bot, accid, chat_id, text, priority=dc_helpers.PRIO_LOW)


def _dm(bot, accid, contact_id, text):
    if not contact_id:
        return
    try:
        chat_id = bot.rpc.create_chat_by_contact_id(accid, contact_id)
        dc_helpers._send(bot, accid, chat_id, text)
    except Exception as e:
        config.logger.warning(f"Catalog: cannot message contact {contact_id}: {e}")


def _preview_url(token: str) -> str:
    base_url = database.get_config("base_url") or os.getenv("BASE_URL") or ""
    return f"{base_url.rstrip('/')}/c/{token}" if base_url else f"/c/{token}"


def check_eligibility(bot, accid, contact_id: int, addr: str) -> str | None:
    """None if the contact may submit now, otherwise the reason (sent to the user)."""
    if database.is_catalog_submitter_banned(addr, contact_id):
        return "❌ You cannot submit to the catalog."
    age = contact_age_days(contact_id)
    if age < config.CATALOG_MIN_AGE_DAYS:
        wait_days = max(1, int(config.CATALOG_MIN_AGE_DAYS - age + 0.999))
        return (
            "⏳ Catalog submissions are open to contacts I have known for at least "
            f"{config.CATALOG_MIN_AGE_DAYS:g} days. Please try again in {wait_days} day(s)."
        )
    if database.count_catalog_submissions(submitter_id=contact_id, pending_only=True) >= config.CATALOG_MAX_PENDING_PER_USER:
        return "⏳ You already have submissions waiting for review. Please wait until they are decided."
    if database.count_catalog_submissions(submitter_id=contact_id, since=time.time() - 7 * DAY) >= config.CATALOG_MAX_PER_WEEK:
        return f"⏳ You can submit at most {config.CATALOG_MAX_PER_WEEK} entries per week. Please try again later."
    if database.count_catalog_submissions(pending_only=True) >= config.CATALOG_MAX_PENDING_TOTAL:
        return "⏳ The review queue is full right now. Please try again later."
    return None


def find_stopwords(*texts) -> list[str]:
    blob = " ".join(t for t in texts if t).lower()
    return [w for w in config.CATALOG_STOPWORDS if w in blob]


def extract_urls(*texts) -> list[str]:
    seen = []
    for t in texts:
        for u in _URL_RE.findall(t or ""):
            u = u.rstrip(".,!?:;)'\"")
            if u and u not in seen:
                seen.append(u)
    return seen


def collect_red_flags(texts: list[str]) -> tuple[list[str], list[str]]:
    """(flags, notes). Any flag stops auto-approval; notes are shown to the admin only.

    Runs VirusTotal lookups (slow), so call it from a background thread.
    """
    flags, notes = [], []
    words = find_stopwords(*texts)
    if words:
        flags.append("🚩 Stop words: " + ", ".join(words))

    urls = extract_urls(*texts)
    vt_key = os.environ.get("VIRUSTOTAL_API_KEY", "").strip().strip("'\"")
    if urls and vt_key:
        for url in urls[:VT_MAX_URLS]:
            verdict = virustotal.url_verdict(url, vt_key)
            if verdict is None:
                notes.append(f"⚪️ VirusTotal: no answer for {url}")
                continue
            mal, sus, total = verdict
            if mal or sus:
                flags.append(f"🚨 VirusTotal: {url} — {mal} malicious, {sus} suspicious of {total}")
            else:
                notes.append(f"🟢 VirusTotal: {url} — clean ({total} vendors)")
        if len(urls) > VT_MAX_URLS:
            notes.append(f"ℹ️ {len(urls) - VT_MAX_URLS} more link(s) not checked")
    elif urls:
        notes.append(f"ℹ️ {len(urls)} link(s), VirusTotal is not configured")
    return flags, notes


def should_auto_approve(age_days: float, flags: list[str]) -> bool:
    return config.CATALOG_AUTO_APPROVE_DAYS > 0 and age_days >= config.CATALOG_AUTO_APPROVE_DAYS and not flags


def _review_card(kind, item, submitter_text, age_days, flags, notes, extra_lines=(), auto=False, header=None):
    cmd = "chat" if kind == KIND_CHAT else "dchannel"
    cid = item["id"]
    what = "chat" if kind == KIND_CHAT else "channel"
    if header is None:
        header = (f"🆗 Auto-approved {what} submission #{cid}" if auto
                  else f"📥 New {what} submission #{cid}")
    desc = (item.get("description") or "").strip()
    if len(desc) > 300:
        desc = desc[:300] + "..."
    lines = [header, f"**{item.get('name') or '?'}**" + (f" — {desc}" if desc else "")]
    lines.append(f"From: {dc_helpers._get_contact_age_indicator(item.get('submitted_by') or 0)} "
                 f"{submitter_text}, known for {age_days:.0f} day(s)")
    lines.extend(extra_lines)
    lines.extend(flags)
    lines.extend(notes)
    if auto:
        lines.append(f"\nRemove: /{cmd}reject{cid} [reason]   Remove + ban: /{cmd}ban{cid}")
    else:
        lines.append(f"\n/{cmd}approve{cid}   /{cmd}reject{cid} [reason]   /{cmd}ban{cid} [reason]")
    return "\n".join(lines)


# ── Chat submissions ──

def submit_chat(bot, accid, msg, chat_name: str, description: str, member_count: int):
    """/chatadd by a non-admin, sent inside the group (only plain groups)."""
    existing = database.get_catalog_chat_by_chat_id(msg.chat_id)
    if existing:
        if (existing.get("status") or "approved") == "pending":
            dc_helpers._send(bot, accid, msg.chat_id, "⏳ This chat is already waiting for review.")
        else:
            dc_helpers._send(bot, accid, msg.chat_id, f"ℹ️ This chat is already in the catalog: /chat{existing['id']}")
        return

    submitter_text, addr = _contact_label(bot, accid, msg.from_id)
    error = check_eligibility(bot, accid, msg.from_id, addr)
    if error:
        dc_helpers._send(bot, accid, msg.chat_id, error)
        return

    catalog_id = database.add_catalog_chat_submission(msg.chat_id, chat_name, description, member_count, msg.from_id)
    database.log_catalog_submission(KIND_CHAT, catalog_id, msg.chat_id, msg.from_id, addr, chat_name)

    if extract_urls(chat_name, description) and os.environ.get("VIRUSTOTAL_API_KEY", "").strip():
        dc_helpers._send(bot, accid, msg.chat_id, "🔎 Checking the links in the description, this takes a minute...")
    threading.Thread(
        target=_evaluate_chat, args=(bot, accid, catalog_id, submitter_text), daemon=True
    ).start()


def _evaluate_chat(bot, accid, catalog_id: int, submitter_text: str):
    try:
        item = database.get_catalog_chat_by_id(catalog_id)
        if not item:
            return
        age = contact_age_days(item["submitted_by"])
        flags, notes = collect_red_flags([item.get("name"), item.get("description")])
        auto = should_auto_approve(age, flags)
        extra = [f"👥 {item.get('member_count', 0)} members"]
        privacy = (
            "Joining needs approval by the members of this group (they answer the requests here). "
            "To let everyone join at once, a member can send /private off."
        )
        if auto:
            database.set_catalog_chat_status(catalog_id, "approved")
            database.decide_catalog_submission(KIND_CHAT, catalog_id, "auto")
            dc_helpers._send(bot, accid, item["chat_id"],
                             f"✅ This chat is now in the catalog as /chat{catalog_id}.\n{privacy}\n"
                             f"To take it out of the catalog, a member can send /chatremove.")
        else:
            dc_helpers._send(bot, accid, item["chat_id"],
                             f"📥 This chat was submitted to the catalog and waits for review by the bot admin.\n"
                             f"Once listed: {privacy}")
        _notify_admin(bot, accid, _review_card(KIND_CHAT, item, submitter_text, age, flags, notes, extra, auto=auto))
    except Exception as e:
        config.logger.error(f"Catalog: evaluating chat submission {catalog_id} failed: {e}")


# ── Channel submissions ──

def is_link_owner(bot, accid, qr_info: dict, contact_id: int) -> bool:
    """True if the invite link was created by the submitter.

    A channel invite link carries its owner's key: check_qr resolves it to a contact
    (and fingerprint). For a group link it is the member who made the link.
    """
    qr_contact = qr_info.get("contact_id") if isinstance(qr_info, dict) else None
    if isinstance(qr_contact, int) and qr_contact == contact_id:
        return True
    link_fp = str((qr_info or {}).get("fingerprint") or "").replace(" ", "").replace(":", "").upper()
    if link_fp:
        sender_fp = dc_helpers._get_contact_fingerprint(bot, accid, contact_id) or ""
        return link_fp in sender_fp.upper().split(",")
    return False


def submit_channel(bot, accid, msg, url: str, qr_info: dict, chat_name: str):
    """/dchanneladd by a non-admin. The link kind and duplicates are already checked."""
    kind = str((qr_info or {}).get("kind", "")).lower()
    if kind not in USER_CHANNEL_KINDS:
        dc_helpers._send(bot, accid, msg.chat_id, "❌ Please send a Delta Chat channel or group invite link.")
        return

    submitter_text, addr = _contact_label(bot, accid, msg.from_id)
    error = check_eligibility(bot, accid, msg.from_id, addr)
    if error:
        dc_helpers._send(bot, accid, msg.chat_id, error)
        return

    with _inflight_lock:
        if msg.from_id in _inflight_submitters:
            dc_helpers._send(bot, accid, msg.chat_id, "⏳ Your previous channel is still being checked, please wait.")
            return
        _inflight_submitters.add(msg.from_id)

    is_owner = is_link_owner(bot, accid, qr_info, msg.from_id)
    dc_helpers._send(bot, accid, msg.chat_id,
                     f"⏳ Joining **{chat_name or 'channel'}** to check it. I will report back here.")
    threading.Thread(
        target=_channel_submission_worker,
        args=(bot, accid, msg.chat_id, msg.from_id, submitter_text, addr, url, chat_name, qr_info, is_owner),
        daemon=True,
    ).start()


def _channel_submission_worker(bot, accid, reply_chat_id, submitter_id, submitter_text, addr,
                               url, chat_name, qr_info, is_owner):
    import channels  # channels imports this module

    try:
        joined_chat_id = bot.rpc.secure_join(accid, url)
        if not isinstance(joined_chat_id, int) or joined_chat_id <= 0:
            dc_helpers._send(bot, accid, reply_chat_id, "❌ Could not join this channel.")
            return
        chat = bot.rpc.get_basic_chat_info(accid, joined_chat_id)
        c_name = (chat.get("name") if isinstance(chat, dict) else getattr(chat, "name", None)) or chat_name or "Channel"
        chat_type = chat.get("chat_type") if isinstance(chat, dict) else getattr(chat, "chat_type", "Single")
        if str(chat_type) == "Single":
            dc_helpers._send(bot, accid, reply_chat_id, "❌ The link opened a private chat, not a channel.")
            return

        # The bot may already follow this channel under another link: never touch that entry.
        existing = database.get_catalog_channel_by_chat_id(joined_chat_id)
        if existing:
            if existing.get("status") == "pending":
                dc_helpers._send(bot, accid, reply_chat_id, f"⏳ **{c_name}** is already waiting for review.")
            else:
                dc_helpers._send(bot, accid, reply_chat_id, f"ℹ️ **{c_name}** is already in the catalog.")
            return

        joined = channels._wait_for_join(bot, accid, joined_chat_id, channels.JOIN_QUICK_WAIT, 1.0)
        description = channels._channel_description(bot, accid, joined_chat_id, chat_type)
        token = database.add_catalog_channel(joined_chat_id, c_name, description, 0, url, is_public=0)
        database.set_catalog_channel_submission(joined_chat_id, submitter_id, web_enabled=is_owner)
        item = database.get_catalog_channel_by_chat_id(joined_chat_id)
        database.log_catalog_submission(KIND_CHANNEL, item["id"], joined_chat_id, submitter_id, addr, c_name)
        if joined:
            channels._backfill_channel(bot, accid, joined_chat_id, token)

        posts = database.get_channel_posts(joined_chat_id, limit=10)
        flags, notes = collect_red_flags([c_name, description] + [p.get("text") or "" for p in posts])
        age = contact_age_days(submitter_id)
        auto = joined and should_auto_approve(age, flags)

        extra = [
            "👤 Owner: ✅ submitted by the channel owner (web preview on)" if is_owner
            else "👤 Owner: ❌ not the owner — listed without web preview / RSS / Fediverse",
            f"📝 {len(posts)} recent post(s) cached" if joined
            else "⏳ Join not finished: the owner's device has not answered yet, no posts to check",
            f"🌐 Review preview: {_preview_url(token)}",
            f"🔗 {url}",
        ]
        if not is_owner:
            extra.append(f"Enable web mirroring later: /dchannelweb{item['id']}on")

        if auto:
            database.set_catalog_channel_status(item["id"], "approved")
            database.decide_catalog_submission(KIND_CHANNEL, item["id"], "auto")
            web.routes.invalidate_channel_cache(chat_id=joined_chat_id, token=token)
            dc_helpers._send(bot, accid, reply_chat_id, _approved_channel_text(item["id"], c_name, token, is_owner))
        else:
            dc_helpers._send(bot, accid, reply_chat_id,
                             f"📥 **{c_name}** was submitted to the catalog and waits for review by the bot admin. "
                             f"I will let you know here.")
        _notify_admin(bot, accid, _review_card(KIND_CHANNEL, item, submitter_text, age, flags, notes, extra, auto=auto))
    except Exception as e:
        config.logger.error(f"Catalog: channel submission failed: {e}")
        dc_helpers._send(bot, accid, reply_chat_id, "❌ An error occurred while submitting the channel.")
    finally:
        with _inflight_lock:
            _inflight_submitters.discard(submitter_id)


def _approved_channel_text(catalog_id, name, token, web_on) -> str:
    text = f"✅ **{name}** is now in the channel catalog: /dchannel{catalog_id}"
    if web_on:
        text += f"\n🌐 Web preview: {_preview_url(token)}\n📡 RSS feed: {_preview_url(token)}/rss.xml"
    return text


def discard_channel(bot, accid, channel: dict):
    """Leave a rejected channel and delete everything cached about it."""
    chat_id = channel["chat_id"]
    for fn in ("leave_group", "delete_chat"):
        try:
            getattr(bot.rpc, fn)(accid, chat_id)
        except Exception as e:
            config.logger.info(f"Catalog: {fn} for rejected channel {chat_id}: {e}")
    database.hard_remove_catalog_channel(chat_id)
    token = channel.get("token")
    if token and re.fullmatch(r"[A-Za-z0-9_-]+", token):
        shutil.rmtree(os.path.join(state.CHANNEL_MEDIA_DIR, token), ignore_errors=True)
    web.routes.invalidate_channel_cache(chat_id=chat_id, token=token)


# ── Admin decisions ──

def _get_item(kind, catalog_id):
    if kind == KIND_CHAT:
        return database.get_catalog_chat_by_id(catalog_id)
    return database.get_catalog_channel_by_id(catalog_id)


def approve(bot, accid, kind, catalog_id) -> str:
    item = _get_item(kind, catalog_id)
    if not item:
        return "❌ Not found in the catalog."
    if (item.get("status") or "approved") != "pending":
        return "ℹ️ This entry is not waiting for review."
    database.clear_catalog_reports(kind, catalog_id)
    database.decide_catalog_submission(kind, catalog_id, "approved")
    if kind == KIND_CHAT:
        database.set_catalog_chat_status(catalog_id, "approved")
        dc_helpers._send(bot, accid, item["chat_id"], f"✅ This chat is now listed in the catalog as /chat{catalog_id}.")
    else:
        database.set_catalog_channel_status(catalog_id, "approved")
        web.routes.invalidate_channel_cache(chat_id=item["chat_id"], token=item.get("token"))
        _dm(bot, accid, item.get("submitted_by"),
            _approved_channel_text(catalog_id, item["name"], item.get("token"), item.get("web_enabled", 1) != 0))
    return f"✅ **{item['name']}** approved."


def reject(bot, accid, kind, catalog_id, reason: str = "", ban: bool = False) -> str:
    item = _get_item(kind, catalog_id)
    if not item:
        return "❌ Not found in the catalog."
    if not item.get("submitted_by"):
        remove_cmd = "/chatremove (in the chat)" if kind == KIND_CHAT else f"/dchannelremove {catalog_id}"
        return f"ℹ️ This entry was added by the admin, not submitted by a user. Use {remove_cmd}."

    submission = database.get_latest_catalog_submission(kind, catalog_id)
    database.decide_catalog_submission(kind, catalog_id, "rejected", reason or None)
    database.clear_catalog_reports(kind, catalog_id)
    reason_line = f"\nReason: {reason}" if reason else ""
    if kind == KIND_CHAT:
        database.remove_catalog_chat(item["chat_id"])
        dc_helpers._send(bot, accid, item["chat_id"], f"❌ This chat was removed from the catalog by the bot admin.{reason_line}")
    else:
        discard_channel(bot, accid, item)
        _dm(bot, accid, item.get("submitted_by"),
            f"❌ **{item['name']}** was not accepted into the channel catalog.{reason_line}")

    result = f"❌ **{item['name']}** rejected and removed."
    if ban:
        addr = (submission or {}).get("submitter_addr") or _contact_label(bot, accid, item["submitted_by"])[1]
        database.ban_catalog_submitter(addr, item["submitted_by"], reason or None)
        result += f" Submitter {addr} can no longer submit (/catalogunban {addr})."
    return result


def set_web(bot, accid, catalog_id, enabled: bool) -> str:
    channel = database.get_catalog_channel_by_id(catalog_id)
    if not channel:
        return "❌ Channel with this number was not found in the catalog."
    database.set_catalog_channel_web(catalog_id, enabled)
    web.routes.invalidate_channel_cache(chat_id=channel["chat_id"], token=channel.get("token"))
    if enabled:
        return (f"🌐 Web preview, RSS and Fediverse mirroring are **on** for **{channel['name']}**: "
                f"{_preview_url(channel.get('token'))}")
    return f"🔒 **{channel['name']}** is listed in /dchannels only, without web preview, RSS and Fediverse."


# ── Reports ──

def _is_listed(kind, item) -> bool:
    if (item.get("status") or "approved") != "approved":
        return False
    if kind == KIND_CHANNEL:
        return bool(item.get("is_public")) and not item.get("is_deleted")
    return True


def report(bot, accid, kind, catalog_id, reporter_id, reason: str) -> str:
    item = _get_item(kind, catalog_id)
    if not item or not _is_listed(kind, item):
        return "❌ Not found in the catalog."
    if contact_age_days(reporter_id) < config.CATALOG_MIN_AGE_DAYS:
        return "ℹ️ Thank you. Reports from new contacts are not accepted yet, please try again in a few days."
    if not database.add_catalog_report(kind, catalog_id, reporter_id, reason or None):
        return "ℹ️ You have already reported this entry."

    cmd = "chat" if kind == KIND_CHAT else "dchannel"
    reporter_text, _ = _contact_label(bot, accid, reporter_id)
    reports = database.get_catalog_reports(kind, catalog_id)
    reason_text = f": {reason}" if reason else ""
    if len(reports) >= config.CATALOG_REPORT_THRESHOLD:
        # Hidden, not removed: a few colluding accounts must not be able to delete an entry.
        if kind == KIND_CHAT:
            database.set_catalog_chat_status(catalog_id, "pending")
        else:
            database.set_catalog_channel_status(catalog_id, "pending")
            web.routes.invalidate_channel_cache(chat_id=item["chat_id"], token=item.get("token"))
        lines = [f"🚩 **{item['name']}** (/{cmd}{catalog_id}) is hidden after {len(reports)} reports:"]
        for r in reports[:10]:
            lines.append(f"• {_contact_label(bot, accid, r['reporter_id'])[0]}: {r.get('reason') or '—'}")
        lines.append(f"\nKeep listed: /{cmd}approve{catalog_id}   Remove: /{cmd}reject{catalog_id} [reason]")
        if item.get("submitted_by"):
            lines.append(f"Remove + ban submitter: /{cmd}ban{catalog_id}")
        else:
            remove_cmd = "/chatremove (in the chat)" if kind == KIND_CHAT else f"/dchannelremove {catalog_id}"
            lines.append(f"(added by the admin: remove with {remove_cmd})")
        _notify_admin(bot, accid, "\n".join(lines))
    else:
        _notify_admin(bot, accid, f"🚩 Report {len(reports)}/{config.CATALOG_REPORT_THRESHOLD} for "
                                  f"**{item['name']}** (/{cmd}{catalog_id}) by {reporter_text}{reason_text}")
    return "✅ Thank you, the report was sent to the bot admin."


# ── Dispatch of dynamic commands (called from handlers.handle_all_messages) ──

_DECISION_RE = re.compile(r'^/(chat|dchannel)(approve|reject|ban|report)(\d+)(?:\s+(.*))?$', re.IGNORECASE | re.DOTALL)
_WEB_RE = re.compile(r'^/dchannelweb\s*(\d+)\s*(on|off)$', re.IGNORECASE)


def handle_dynamic_command(bot, accid, msg, text: str) -> bool:
    """True if the text was one of this module's commands (handled or refused)."""
    m = _WEB_RE.match(text)
    if m:
        if not dc_helpers._is_dc_admin(bot, accid, msg.from_id):
            dc_helpers._send(bot, accid, msg.chat_id, "❌ Only the bot administrator can use this command.")
            return True
        dc_helpers._send(bot, accid, msg.chat_id, set_web(bot, accid, int(m.group(1)), m.group(2).lower() == "on"))
        return True

    m = _DECISION_RE.match(text)
    if not m:
        return False
    kind = KIND_CHAT if m.group(1).lower() == "chat" else KIND_CHANNEL
    action = m.group(2).lower()
    catalog_id = int(m.group(3))
    reason = (m.group(4) or "").strip()

    if action == "report":
        dc_helpers._send(bot, accid, msg.chat_id, report(bot, accid, kind, catalog_id, msg.from_id, reason))
        return True

    if not dc_helpers._is_dc_admin(bot, accid, msg.from_id):
        dc_helpers._send(bot, accid, msg.chat_id, "❌ Only the bot administrator can use this command.")
        return True
    if action == "approve":
        result = approve(bot, accid, kind, catalog_id)
    else:
        result = reject(bot, accid, kind, catalog_id, reason, ban=(action == "ban"))
    dc_helpers._send(bot, accid, msg.chat_id, result)
    return True


# ── Admin commands ──

@config.dc_cli.on(events.NewMessage(command="/catalogqueue"))
def catalogqueue_command(bot, accid, event):
    msg = event.msg
    if not dc_helpers._is_dc_admin(bot, accid, msg.from_id):
        dc_helpers._send(bot, accid, msg.chat_id, "❌ Only the bot administrator can use this command.")
        return
    items = database.get_pending_catalog_items()
    state_line = f"Submissions by users: **{'on' if submissions_enabled() else 'off'}** (/catalogsubmit on|off)"
    if not items:
        dc_helpers._send(bot, accid, msg.chat_id, f"✅ Nothing waits for review.\n{state_line}")
        return
    lines = [f"📥 **Waiting for review ({len(items)}):**"]
    for it in items:
        cmd = "chat" if it["kind"] == KIND_CHAT else "dchannel"
        who = _contact_label(bot, accid, it["submitted_by"])[0] if it.get("submitted_by") else "reported entry"
        lines.append(f"• {it['kind']} #{it['id']} **{it['name']}** — {who}\n"
                     f"  /{cmd}approve{it['id']}  /{cmd}reject{it['id']}")
    lines.append(f"\n{state_line}")
    dc_helpers._send(bot, accid, msg.chat_id, "\n".join(lines))


@config.dc_cli.on(events.NewMessage(command="/catalogsubmit"))
def catalogsubmit_command(bot, accid, event):
    msg = event.msg
    if not dc_helpers._is_dc_admin(bot, accid, msg.from_id):
        dc_helpers._send(bot, accid, msg.chat_id, "❌ Only the bot administrator can use this command.")
        return
    arg = (event.payload or "").strip().lower()
    if arg in ("on", "off"):
        database.set_config("catalog_submissions", arg)
    dc_helpers._send(
        bot, accid, msg.chat_id,
        f"Catalog submissions by users are **{'on' if submissions_enabled() else 'off'}**.\n"
        f"Minimum contact age {config.CATALOG_MIN_AGE_DAYS:g} days, auto-approval from "
        f"{config.CATALOG_AUTO_APPROVE_DAYS:g} days (0 = never), {config.CATALOG_MAX_PENDING_PER_USER} pending "
        f"and {config.CATALOG_MAX_PER_WEEK} per week per user, {config.CATALOG_REPORT_THRESHOLD} reports hide an entry.\n"
        f"Usage: /catalogsubmit on|off",
    )


@config.dc_cli.on(events.NewMessage(command="/catalogban"))
def catalogban_command(bot, accid, event):
    msg = event.msg
    if not dc_helpers._is_dc_admin(bot, accid, msg.from_id):
        dc_helpers._send(bot, accid, msg.chat_id, "❌ Only the bot administrator can use this command.")
        return
    addr = (event.payload or "").strip().lower()
    if "@" not in addr:
        dc_helpers._send(bot, accid, msg.chat_id, "Usage: /catalogban <address>")
        return
    contact_id = None
    try:
        contact_id = bot.rpc.lookup_contact_id_by_addr(accid, addr) or None
    except Exception:
        pass
    database.ban_catalog_submitter(addr, contact_id)
    dc_helpers._send(bot, accid, msg.chat_id, f"⛔ {addr} can no longer submit to the catalog.")


@config.dc_cli.on(events.NewMessage(command="/catalogunban"))
def catalogunban_command(bot, accid, event):
    msg = event.msg
    if not dc_helpers._is_dc_admin(bot, accid, msg.from_id):
        dc_helpers._send(bot, accid, msg.chat_id, "❌ Only the bot administrator can use this command.")
        return
    addr = (event.payload or "").strip().lower()
    if not addr:
        dc_helpers._send(bot, accid, msg.chat_id, "Usage: /catalogunban <address>")
        return
    if database.unban_catalog_submitter(addr):
        dc_helpers._send(bot, accid, msg.chat_id, f"✅ {addr} may submit to the catalog again.")
    else:
        dc_helpers._send(bot, accid, msg.chat_id, f"ℹ️ {addr} is not banned.")
