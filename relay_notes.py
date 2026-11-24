"""Relay notes: tell a person when their own relay is down or is a public mail provider.

A contact's relays come from get_contact_encryption_info(): the core lists every address of the
contact's key under "Relays:" and announces relay changes, so the list is current. Only the
domains are kept, in the contact_relays table, refreshed at most once a day per contact and only
when that contact shows up (welcome greeting, a message to the bot, /relays). Nothing sweeps over
all contacts.

A note goes out when one of the person's relays has been down (cmping monitor) for at least
MIN_DOWN_SECONDS, or is a public Russian mail provider (config.REGULAR_MAIL_DOMAINS), at most
once per NOTE_INTERVAL_SECONDS per contact and domain. It suggests two working relays as
https://<domain>/new links: the Delta Chat app offers to add the relay when the link is opened.

Cost per message (maybe_note_for_message): one contact_relays read, plus once a day per contact
one RPC and one write. The relay health is read from memory. Only when something is wrong for
this person: one downtime query, one rate-limit query; and when a note is really sent, one
transport list RPC, one monitor list query, one batched ping query and one write.
"""
import os
import random
import re
import threading
import time

import config
import database
import dc_helpers
import relaygeo
import state

MIN_DOWN_SECONDS = int(os.getenv("RELAY_NOTE_MIN_DOWN_SECONDS", "14400"))
NOTE_INTERVAL_SECONDS = 6 * 3600        # a relay that is down: at most one note per 6 h
PUBLIC_NOTE_INTERVAL_SECONDS = 7 * 86400  # a public mail provider is a standing condition: once a week
CACHE_SECONDS = 24 * 3600
EMPTY_RETRY_SECONDS = 600  # nothing readable (new contact, RPC error): look again after 10 min
MIN_PINGS = 10
TOP_N = 5
SUGGESTIONS = 2
GROUP_NOTE_COOLDOWN_SECONDS = 300  # a group gets at most one relay note per 5 min (a dead shared relay must not flood it)
SUGGEST_CACHE_SECONDS = 600       # ping averages change every ~30 min; the ranking is reused for 10 min
WHITELIST_CHECK_TOP = 30          # whitelist lookups only for the best candidates by ping
LIST_RELAYS = 15  # /relays: relays listed in the unreachable block
LIST_NAMES = 10   # /relays: people named per relay
# Seconds to wait before a note that follows a command, so the command's own reply goes first
# (the command handler and the catch-all handler run in no fixed order). 0 = send right away.
SEND_DELAY_SECONDS = 3.0

_EMAIL_RE = re.compile(r"[a-zA-Z0-9_.+-]+@([a-zA-Z0-9-]+(?:\.[a-zA-Z0-9-]+)+)")
_RELAYS_HEADER_RE = re.compile(r"^\s*relays\s*:\s*$", re.IGNORECASE)
_DOMAIN_RE = re.compile(r"^[a-z0-9-]+(?:\.[a-z0-9-]+)+$")

# chat_id -> True for a 1:1 chat. A chat's type never changes, so get_chat runs once per chat.
_dm_cache: dict[int, bool] = {}
_dm_cache_lock = threading.Lock()
_DM_CACHE_MAX = 20000

# check-then-mark of the rate limit must not interleave between two messages of one person
_note_lock = threading.Lock()
_chat_last_note: dict[int, float] = {}  # group chat_id -> time of the last note
_suggest_cache: dict = {}  # {'at', 'servers', 'averages'}
_suggest_lock = threading.Lock()


# ── Parsing ──────────────────────────────────────────────────────────────────

def domain_of(addr) -> str:
    """Lower-case domain of an address, "" when it is not one."""
    addr = (addr or "").strip().lower()
    if "@" not in addr:
        return ""
    domain = addr.rsplit("@", 1)[1].strip(".")
    return domain if _DOMAIN_RE.match(domain) else ""


def parse_relay_domains(enc_info) -> list[str]:
    """Relay domains from a get_contact_encryption_info() text (lower case, sorted, unique).

    The core (contact.rs get_encrinfo) ends the text with

        Relays:
        alice@relay1.example
        alice@relay2.example

    "Relays:" is a literal, not a translated string. Only that section is read: the fingerprint
    headers above it also hold our own address ("Me (bot@...)") on older cores, and a contact
    name may contain anything. No section (no key, or an unknown format) gives []: the contact
    is skipped rather than guessed."""
    if not isinstance(enc_info, str) or not enc_info:
        return []
    # Only "\n" separates lines (str.splitlines() would also split on U+2028 and similar, which
    # a contact name may carry). The section is the last "Relays:" line: a contact named "Relays"
    # puts the same header into the fingerprint part above it.
    lines = enc_info.split("\n")
    headers = [i for i, line in enumerate(lines) if _RELAYS_HEADER_RE.match(line)]
    if not headers:
        return []
    domains = set()
    for line in lines[headers[-1] + 1:]:
        line = line.strip()
        if not line:
            if domains:
                break  # blank line after the list ends the section
            continue
        m = _EMAIL_RE.fullmatch(line)
        if not m:
            break  # not an address: the section is over
        domain = domain_of(line)
        if domain:
            domains.add(domain)
    return sorted(domains)


# ── Contact relay cache ──────────────────────────────────────────────────────

def fetch_contact_domains(bot, accid, contact_id, primary_addr=None) -> list[str]:
    """One RPC: the contact's relay domains, plus the domain of primary_addr when given."""
    domains = set()
    try:
        domains.update(parse_relay_domains(bot.rpc.get_contact_encryption_info(accid, contact_id)))
    except Exception as e:
        config.logger.debug(f"Relay notes: encryption info of contact {contact_id} failed: {e}")
        raise
    primary = domain_of(primary_addr)
    if primary:
        domains.add(primary)
    return sorted(domains)


def contact_domains(bot, accid, contact_id, primary_addr=None, now=None) -> list[str]:
    """The contact's relay domains from the cache; refreshed (one RPC) when older than a day."""
    now = time.time() if now is None else now
    cached = database.get_contact_relays(contact_id)
    if cached is not None and now - cached[1] < CACHE_SECONDS:
        return cached[0]
    failed = False
    try:
        domains = fetch_contact_domains(bot, accid, contact_id, primary_addr)
    except Exception:
        failed = True  # the RPC failed: never replace a known list by a partial one
        domains = cached[0] if cached is not None else []
    if not failed and not domains and cached is not None:
        domains = cached[0]  # nothing readable this time: keep the old list
    # An empty result is kept only briefly: a contact whose key is not known yet must not stay
    # unnoticed for a day, and a persistent failure still costs at most one RPC per 10 minutes
    stamp = now if domains and not failed else now - CACHE_SECONDS + EMPTY_RETRY_SECONDS
    database.set_contact_relays(contact_id, domains, stamp)
    return domains


# ── Relay health ─────────────────────────────────────────────────────────────

def down_domains(domains, now=None, min_down=0) -> dict:
    """{domain: {"went_down_at", "error_msg"}} of these domains that are down now.

    The health comes from memory (cmping monitor); servers without a status yet are skipped. The
    downtime start is read in one query, and only when one of the domains is down. With min_down,
    only domains down at least that long (a known start is needed then)."""
    now = time.time() if now is None else now
    status = state.cmping_status_of(domains)
    down = [d for d, healthy in status.items() if healthy is False]
    if not down:
        return {}
    events = database.get_open_cmping_downtimes(down)
    result = {}
    for d in down:
        ev = events.get(d) or {"went_down_at": None, "error_msg": ""}
        if min_down:
            if not ev["went_down_at"] or now - ev["went_down_at"] < min_down:
                continue
        result[d] = ev
    return result


def format_duration(seconds) -> str:
    seconds = max(0, int(seconds))
    if seconds < 3600:
        return f"{max(1, seconds // 60)} min"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    days, rest = divmod(seconds, 86400)
    hours = rest // 3600
    return f"{days}d {hours}h" if hours else f"{days}d"


def short_reason(error_msg, limit=80) -> str:
    text = " ".join((error_msg or "").split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


# ── Suggestions ──────────────────────────────────────────────────────────────

def select_relay_suggestions(averages, status, exclude=(), whitelisted=frozenset(), prefer_whitelisted=False,
                             k=SUGGESTIONS, top_n=TOP_N, min_pings=MIN_PINGS, rng=None) -> list[str]:
    """Pick k relays to suggest. Pure: no I/O.

    averages: {domain: (avg_ms, count)}; status: {domain: healthy}. A candidate is healthy now,
    not in exclude (the person's own relays) and has at least min_pings measurements. k are
    picked at random from the top_n with the lowest average ping. With prefer_whitelisted, the
    whitelisted candidates are used first and the others only fill up. Fewer candidates give a
    shorter list."""
    rng = rng or random
    exclude = {d.lower() for d in exclude}
    candidates = [d for d, (avg, count) in averages.items()
                  if avg is not None and (count or 0) >= min_pings
                  and status.get(d) is True and d.lower() not in exclude]
    candidates.sort(key=lambda d: (averages[d][0], d))
    groups = ([[d for d in candidates if d in whitelisted], [d for d in candidates if d not in whitelisted]]
              if prefer_whitelisted else [candidates])
    picked = []
    for group in groups:
        top = group[:top_n]
        need = k - len(picked)
        if need <= 0:
            break
        picked += rng.sample(top, min(need, len(top)))
    return picked


def suggest_relays(bot, accid, own_domains, prefer_whitelisted=False) -> list[str]:
    """Relays to suggest, from the monitored servers (bot transports + /cmpingadd list).

    Runs only when a note is about to be sent: one transport list RPC, one monitor query, one
    batched ping query, and for public-provider users one whitelist switch read."""
    now = time.time()
    with _suggest_lock:
        cached = dict(_suggest_cache)
    if not cached or now - cached["at"] > SUGGEST_CACHE_SECONDS:
        servers = list(dc_helpers._get_bot_domains(bot, accid))
        for d in database.get_all_cmping_monitors():
            if d not in servers:
                servers.append(d)
        cached = {"at": now, "averages": database.get_average_pings_for_servers(servers, limit=100)}
        with _suggest_lock:
            _suggest_cache.clear()
            _suggest_cache.update(cached)
    averages = cached["averages"]
    status = state.cmping_status_snapshot()  # health stays live: only the ping ranking is reused
    whitelisted = set()
    if prefer_whitelisted:
        pool = sorted((d for d, (avg, count) in averages.items()
                       if (count or 0) >= MIN_PINGS and status.get(d) is True and d not in own_domains
                       and avg is not None), key=lambda d: (averages[d][0], d))[:WHITELIST_CHECK_TOP]
        whitelisted = relaygeo.whitelisted_many(pool)
    return select_relay_suggestions(averages, status, exclude=own_domains,
                                    whitelisted=whitelisted, prefer_whitelisted=prefer_whitelisted)


# ── The note ─────────────────────────────────────────────────────────────────

def build_note(down: dict, public_domains, links, now=None, name=None) -> str:
    """Text of a relay note. down: {domain: event} past the threshold; public_domains: public mail
    provider domains of the person; links: relay domains to suggest."""
    now = time.time() if now is None else now
    lines = []
    lead = f"**{name}**, your" if name else "Your"
    for domain in sorted(down):
        since = down[domain].get("went_down_at")
        dur = f" for {format_duration(now - since)}" if since else ""
        lines.append(f"⚠️ {lead} relay `{domain}` has been unreachable{dur}.")
        lead = "Your"
    for domain in sorted(public_domains):
        lines.append(f"⚠️ {lead} address at `{domain}` is a public mail provider and may cause "
                     f"delivery issues in large groups.")
        lead = "Your"
    urls = " or ".join(f"https://{d}/new" for d in links)
    if public_domains and not down:
        lines.append(f"Add a chatmail relay: {urls}" if urls else "Adding a chatmail relay to your profile helps.")
    else:
        lines.append(f"Add a working one: {urls}" if urls else "Adding another relay to your profile helps.")
    return "\n".join(lines)


def compute_note(bot, accid, contact_id, domains, now=None, name=None):
    """The note text for this contact, or None when nothing is wrong or it was sent recently.

    Marks the domains as noted when a text is returned."""
    if not domains:
        return None
    now = time.time() if now is None else now
    down = down_domains(domains, now=now, min_down=MIN_DOWN_SECONDS)
    public = sorted(d for d in domains if d in config.REGULAR_MAIL_DOMAINS)
    flagged = set(down) | set(public)
    if not flagged:
        return None
    with _note_lock:
        sent = database.get_relay_notes_sent(contact_id, flagged)
        # a public provider is a standing condition (weekly); a relay that is down is news (every 6 h)
        due = {d for d in flagged
               if now - sent.get(d, 0) >= (PUBLIC_NOTE_INTERVAL_SECONDS if d in public and d not in down
                                           else NOTE_INTERVAL_SECONDS)}
        if not due or _is_bot(bot, accid, contact_id):
            return None
        database.mark_relay_notes_sent(contact_id, due, now)
    down = {d: ev for d, ev in down.items() if d in due}
    public = [d for d in public if d in due]
    try:
        links = suggest_relays(bot, accid, set(domains), prefer_whitelisted=bool(public))
    except Exception as e:
        config.logger.warning(f"Relay notes: could not pick relays to suggest: {e}")
        links = []
    if not links and not down:
        # Only "you use a public provider" and nothing to suggest (monitor not warmed up after a
        # restart, or no healthy relay): a note without a way out is noise, and must not use up the
        # weekly allowance
        database.mark_relay_notes_sent(contact_id, due, 0)
        return None
    return build_note(down, public, links, now=now, name=name)


def _is_dm(bot, accid, chat_id) -> bool:
    with _dm_cache_lock:
        if chat_id in _dm_cache:
            return _dm_cache[chat_id]
    try:
        chat = bot.rpc.get_basic_chat_info(accid, chat_id)
        chat_type = chat.get("chat_type") if isinstance(chat, dict) else getattr(chat, "chat_type", None)
    except Exception:
        return False  # not cached: try again next time
    is_dm = str(chat_type) == "Single"
    with _dm_cache_lock:
        if len(_dm_cache) >= _DM_CACHE_MAX:
            _dm_cache.clear()
        _dm_cache[chat_id] = is_dm
    return is_dm


def _is_bot(bot, accid, contact_id) -> bool:
    """True for a bot contact (another bot's command must not get a note, bots could answer each
    other). Only asked when a note is about to be sent, so it costs nothing per message."""
    try:
        contact = bot.rpc.get_contact(accid, contact_id)
        flag = contact.get("is_bot") if isinstance(contact, dict) else getattr(contact, "is_bot", False)
    except Exception:
        return False
    return flag is True


def _send_later(bot, accid, chat_id, text, reply_to_id=None):
    def send():
        try:
            dc_helpers._send(bot, accid, chat_id, text, reply_to_id=reply_to_id, priority=dc_helpers.PRIO_LOW)
        except Exception as e:
            config.logger.error(f"Relay note: sending to chat {chat_id} failed: {e}")
    if SEND_DELAY_SECONDS > 0:
        timer = threading.Timer(SEND_DELAY_SECONDS, send)
        timer.daemon = True
        timer.start()
    else:
        send()


def maybe_note_for_message(bot, accid, event) -> None:
    """Hook for every incoming message (handlers.handle_all_messages): a person who writes to the
    bot in a 1:1 chat or uses a command in a group gets a relay note when one applies."""
    msg = event.msg
    if getattr(msg, "is_info", False):
        return
    from_id = getattr(msg, "from_id", 0) or 0
    if from_id <= 9 or from_id == config.DC_CONTACT_ID_SELF:
        return
    # In a group only a command this bot recognised counts: event.command is cleared for
    # "/cmd@otherbot" and in groups with several bots, and is empty for "/etc/hosts" or a
    # command meant for another bot. In a 1:1 chat every message counts.
    if not getattr(event, "command", "") and not _is_dm(bot, accid, msg.chat_id):
        return
    in_group = not _is_dm(bot, accid, msg.chat_id)
    now = time.time()
    if in_group and now - _chat_last_note.get(msg.chat_id, 0) < GROUP_NOTE_COOLDOWN_SECONDS:
        return  # this group just got a note: nobody is marked, they get theirs on a later command
    domains = contact_domains(bot, accid, from_id)
    note = compute_note(bot, accid, from_id, domains)
    if note:
        if in_group:
            if len(_chat_last_note) >= _DM_CACHE_MAX:
                _chat_last_note.clear()
            _chat_last_note[msg.chat_id] = now
        # The note quotes the person's message, so in a group it is clear whom it is for
        _send_later(bot, accid, msg.chat_id, note, reply_to_id=getattr(msg, "id", None))


def note_after_welcome(bot, accid, chat_id, contact_id, name=None, primary_addr=None) -> None:
    """Second message right after a welcome greeting, when a note applies to the new member."""
    now = time.time()
    if now - _chat_last_note.get(chat_id, 0) < GROUP_NOTE_COOLDOWN_SECONDS:
        return  # several people joined at once: not one note each
    domains = contact_domains(bot, accid, contact_id, primary_addr=primary_addr)
    note = compute_note(bot, accid, contact_id, domains, name=name)
    if note:
        _chat_last_note[chat_id] = now
        dc_helpers._send(bot, accid, chat_id, note)
