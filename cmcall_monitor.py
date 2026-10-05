"""Periodic call monitoring between relays (the call-side twin of the cmping monitor).

Every CMCALL_MONITOR_INTERVAL seconds (default 1 h) one source relay is picked
round-robin from the same server list as cmping (bot transports + /cmpingadd,
minus /cmcallskip) and calls every other relay with `cmcall --json`
(relay-only ICE, so both relays' TURN servers are in the path). One call per
pair is enough - the offer travels source->target and the answer back - and
the direction alternates every cycle. A lone relay calls itself.

Health per relay: ok / degraded / down / n/a (no TURN server announced).
- Hard failures (setup/signaling/ice/media) are retried once; if the source
  fails with all of >= 2 targets the source is down, otherwise each failing
  target is down (same root-cause logic as cmping).
- Packet loss >= CMCALL_DEGRADED_LOSS_PCT in two checks in a row: degraded.
  Quality is measured from the bot's host only, so it never counts as down.
- Relays without TURN are "n/a" and never alert.

Each down/degraded episode is one "📞" message per /cmreport chat, edited in
place when the error changes and when it resolves.
"""
import time
from datetime import datetime, timezone

from deltachat2 import events

import calls
import cmping
import config
import database
import dc_helpers
import state

STATUS_ICONS = {
    "ok": "✅",
    "degraded": "⚠️",
    "down": "🚨",
    "n/a": "➖",
    None: "⚪️",
}


# --------------------------------------------------------------------------
# server list
# --------------------------------------------------------------------------


def monitored_servers(bot, accid) -> list[str]:
    servers = list(dc_helpers._get_bot_domains(bot, accid))
    for d in database.get_all_cmping_monitors():
        if d not in servers:
            servers.append(d)
    skipped = set(database.get_cmcall_skipped())
    return [s for s in servers if s not in skipped]


# --------------------------------------------------------------------------
# checks
# --------------------------------------------------------------------------


def _no_turn_servers(result: dict, a: str, b: str) -> set[str]:
    """Relays the result shows to have no TURN server (cmcall's 'none ...' label)."""
    out = set()
    if str(result.get("caller_turn") or "").startswith("none"):
        out.add(a)
    if str(result.get("callee_turn") or "").startswith("none"):
        out.add(b)
    return out


def _max_loss(result: dict):
    losses = [
        (result.get(side) or {}).get("rtp", {}).get("loss_pct")
        for side in ("caller_stats", "callee_stats")
    ]
    losses = [x for x in losses if x is not None]
    return max(losses) if losses else None


def run_monitor_single(a: str, b: str, sleep=time.sleep) -> dict:
    """One monitored call a -> b, retried once on a hard failure."""

    def check():
        state._cmcall_global_lock.acquire()
        try:
            return calls.run_cmcall([a] if a == b else [a, b], duration=config.CMCALL_MONITOR_DURATION)
        finally:
            state._cmcall_global_lock.release()

    result = check()
    if not result.get("ok") and not _no_turn_servers(result, a, b):
        config.logger.info(
            f"CMCall monitor: {a} -> {b} failed at {result.get('stage')} ({result.get('error')}), retrying in 10s"
        )
        sleep(10)
        result = check()
    result["checked_at"] = time.time()
    return result


def _remember_turn(result: dict, a: str, b: str) -> None:
    for server, key in ((a, "caller_turn"), (b, "callee_turn")):
        label = result.get(key)
        if label:
            database.set_cmcall_server_turn(server, label)


# --------------------------------------------------------------------------
# health evaluation (pure on top of the DB, unit tested)
# --------------------------------------------------------------------------


def _pair_error(result: dict, a: str, b: str) -> str:
    return f"{result.get('stage') or '?'}: {result.get('error') or 'unknown error'} (call {a} → {b})"


def evaluate_cycle(source: str, results: list[tuple[str, str, str, dict]]) -> None:
    """Update relay health from one cycle.

    ``results``: (target, caller, callee, result) for every pair of the cycle.
    """
    no_turn = set()
    for _dst, a, b, res in results:
        no_turn |= _no_turn_servers(res, a, b)
    for server in no_turn:
        database.set_cmcall_server_status(server, "n/a", "no TURN server announced")

    valid = [(dst, a, b, res) for dst, a, b, res in results if not ({a, b} & no_turn)]
    if not valid:
        return
    any_ok = any(res.get("ok") for *_x, res in valid)

    if not any_ok and len(valid) >= 2:
        sample = _pair_error(valid[0][3], valid[0][1], valid[0][2])
        database.set_cmcall_server_status(source, "down", f"all call checks failed, e.g. {sample}")
        return

    if any_ok:
        _set_ok_or_degraded(source, None)

    for dst, a, b, res in valid:
        if dst == source:
            if not res.get("ok"):
                database.set_cmcall_server_status(source, "down", _pair_error(res, a, b))
            else:
                _set_ok_or_degraded(source, _max_loss(res))
            continue
        if res.get("ok"):
            _set_ok_or_degraded(dst, _max_loss(res))
        else:
            database.set_cmcall_server_status(dst, "down", _pair_error(res, a, b))


def _set_ok_or_degraded(server: str, loss) -> None:
    """A working call: ok, or degraded after two lossy checks in a row.

    ``loss`` is None for the source of a cycle - call quality is attributed
    to the target only, so the source just recovers from down / n/a.
    """
    info = database.get_cmcall_server(server)
    if loss is None:
        if info.get("status") != "degraded":
            database.set_cmcall_server_status(server, "ok", None)
        return
    if loss < config.CMCALL_DEGRADED_LOSS_PCT:
        database.set_cmcall_server_status(server, "ok", None, degraded_streak=0)
        return
    streak = (info.get("degraded_streak") or 0) + 1
    if streak >= 2:
        database.set_cmcall_server_status(
            server, "degraded", f"packet loss {loss:.1f}% in {streak} checks in a row", degraded_streak=streak
        )
    else:
        database.set_cmcall_server_status(server, "ok", None, degraded_streak=streak)


# --------------------------------------------------------------------------
# alerts
# --------------------------------------------------------------------------


def _gmt(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M GMT")


def format_event_message(event: dict, now: float) -> str:
    server, kind = event["server"], event["kind"]
    if event.get("ended_at"):
        took = cmping._format_duration(event["ended_at"] - event["started_at"])
        what = "failing" if kind == "down" else "degraded"
        lines = [
            f"📞✅ **Calls restored: {server}**",
            f"Calls were {what} for {took} ({_gmt(event['started_at'])} – {_gmt(event['ended_at'])}).",
        ]
        if event.get("error"):
            lines.append(f"Last problem: {event['error']}")
        return "\n".join(lines)
    duration = cmping._format_duration(max(0, now - event["started_at"]))
    title = f"📞🚨 **Calls failing: {server}**" if kind == "down" else f"📞⚠️ **Calls degraded: {server}**"
    return "\n".join([
        title,
        f"Since {_gmt(event['started_at'])} ({duration}).",
        event.get("error") or "",
        "Details: /cmcallstatus " + server,
    ]).strip()


def sync_alerts(bot, accid, servers: list[str], now=None) -> None:
    """Open, update and resolve call incidents to match the relays' health."""
    now = time.time() if now is None else now
    report_chats = database.get_all_cmping_report_chats()
    open_events = {e["server"]: e for e in database.get_open_cmcall_events()}
    statuses = {s: database.get_cmcall_server(s) for s in set(servers) | set(open_events)}

    for server, info in statuses.items():
        status = info.get("status") if server in servers else None
        event = open_events.get(server)
        alerting = status in ("down", "degraded")

        if event and (not alerting or event["kind"] != status):
            database.close_cmcall_event(event["id"], now)
            event = dict(event, ended_at=now)
            _publish(bot, accid, event, report_chats, now)
            event = None
        if alerting and event is None:
            event_id = database.open_cmcall_event(server, status, info.get("error"), now)
            _publish(bot, accid, database.get_cmcall_event(event_id), report_chats, now)
        elif alerting and event is not None and event.get("error") != info.get("error"):
            database.update_cmcall_event_error(event["id"], info.get("error"))
            _publish(bot, accid, database.get_cmcall_event(event["id"]), report_chats, now)


def _publish(bot, accid, event: dict, report_chats: list[int], now: float) -> None:
    text = format_event_message(event, now)
    msg_ids = database.get_cmcall_event_msg_ids(event["id"])
    for chat_id in report_chats:
        msg_id = msg_ids.get(chat_id)
        if msg_id:
            try:
                bot.rpc.send_edit_request(accid, msg_id, text)
                continue
            except Exception as e:
                config.logger.warning(f"CMCall monitor: editing alert {msg_id} in chat {chat_id} failed: {e}")
        new_id = dc_helpers._send(bot, accid, chat_id, text)
        if new_id:
            database.set_cmcall_event_msg_id(event["id"], chat_id, new_id)


# --------------------------------------------------------------------------
# loop
# --------------------------------------------------------------------------


def cmcall_monitor_cycle(bot, accid, run=run_monitor_single) -> None:
    if state._cmcall_monitor_running:
        config.logger.warning("CMCall monitor: previous cycle still running, skipping.")
        return
    state._cmcall_monitor_running = True
    try:
        servers = monitored_servers(bot, accid)
        if not servers:
            return
        idx = state._cmcall_monitor_index % len(servers)
        source = servers[idx]
        state._cmcall_monitor_index = (idx + 1) % len(servers)
        database.set_config("cmcall_monitor_index", str(state._cmcall_monitor_index))
        cycle_no = int(database.get_config("cmcall_monitor_cycle") or 0)
        database.set_config("cmcall_monitor_cycle", str(cycle_no + 1))

        targets = [s for s in servers if s != source] or [source]
        config.logger.info(f"CMCall monitor cycle {cycle_no}: source={source}, targets={targets}")
        results = []
        for dst in targets:
            a, b = (source, dst) if cycle_no % 2 == 0 else (dst, source)
            res = run(a, b)
            _remember_turn(res, a, b)
            database.save_cmcall_result(a, b, res)
            results.append((dst, a, b, res))
        evaluate_cycle(source, results)
        sync_alerts(bot, accid, servers)
    finally:
        state._cmcall_monitor_running = False


def cmcall_monitor_loop(bot, accid) -> None:
    if config.CMCALL_MONITOR_INTERVAL <= 0:
        config.logger.info("CMCall monitor disabled (CMCALL_MONITOR_INTERVAL=0).")
        return
    if calls.rtc is None:
        config.logger.warning("CMCall monitor disabled: cmcall is not installed.")
        return
    # Start a quarter interval after boot so cycles don't coincide with cmping's.
    delay = min(900, max(60, config.CMCALL_MONITOR_INTERVAL // 4))
    config.logger.info(f"CMCall monitor loop started (interval={config.CMCALL_MONITOR_INTERVAL}s, first run in {delay}s).")
    time.sleep(delay)
    while True:
        try:
            cmcall_monitor_cycle(bot, accid)
        except Exception as e:
            config.logger.error(f"CMCall monitor cycle error: {e}")
        time.sleep(config.CMCALL_MONITOR_INTERVAL)


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------


def server_badge(server: str) -> str:
    """Short call-health badge for /cmpinglist."""
    if server in database.get_cmcall_skipped():
        return "📞⏭"
    status = database.get_cmcall_server(server).get("status")
    if status == "n/a":
        return "📞➖ no TURN"
    return f"📞{STATUS_ICONS.get(status, '⚪️')}"


def format_result_line(row: dict) -> str:
    when = datetime.fromtimestamp(row["checked_at"], timezone.utc).strftime("%m-%d %H:%M")
    pair = row["src"] if row["src"] == row["dst"] else f"{row['src']} → {row['dst']}"
    if not row["success"]:
        return f"❌ {pair} · {when} · {row.get('stage') or '?'}: {row.get('error') or 'unknown error'}"
    parts = [f"✅ {pair} · {when}"]
    if row.get("rtt_ms") is not None:
        parts.append(f"rtt {row['rtt_ms']:.0f} ms")
    if row.get("loss_pct") is not None:
        parts.append(f"loss {row['loss_pct']:.1f}%")
    if row.get("signaling_ms") is not None:
        parts.append(f"signaling {row['signaling_ms'] / 1000:.1f}s")
    return " · ".join(parts)


def format_status(servers: list[str], skipped: list[str], results: list[dict], server_filter: str = "",
                  now=None) -> str:
    now = time.time() if now is None else now
    lines = ["📞 **CMCall Monitor Status:**", ""]
    shown = [s for s in servers if not server_filter or server_filter in s]
    for s in shown:
        info = database.get_cmcall_server(s)
        status = info.get("status")
        line = f"{STATUS_ICONS.get(status, '⚪️')} {s}: {status or 'not checked yet'}"
        if status in ("down", "degraded") and info.get("since"):
            line += f" for {cmping._format_duration(now - info['since'])}"
        if info.get("error") and status != "ok":
            line += f" — {info['error']}"
        if info.get("turn") and status != "n/a":
            line += f"\n    TURN {info['turn']}"
        lines.append(line)
    for s in skipped:
        if not server_filter or server_filter in s:
            lines.append(f"⏭ {s}: skipped (/cmcallunskip {s})")
    if not shown and not skipped:
        lines.append("_No servers match._")
    rows = [r for r in results if not server_filter or server_filter in r["src"] or server_filter in r["dst"]]
    if rows:
        lines += ["", "**Last checks:**"] + [format_result_line(r) for r in rows]
    if config.CMCALL_MONITOR_INTERVAL > 0:
        lines.append("")
        lines.append(f"Interval: {config.CMCALL_MONITOR_INTERVAL // 60} min, one call per pair, direction alternates.")
    else:
        lines.append("\nCall monitoring is disabled (CMCALL_MONITOR_INTERVAL=0).")
    return "\n".join(lines)


@config.dc_cli.on(events.NewMessage(command="/cmcallstatus"))
def cmcallstatus_command(bot, accid, event):
    msg = event.msg
    server_filter = (event.payload or "").strip().lower()
    all_servers = list(dc_helpers._get_bot_domains(bot, accid))
    for d in database.get_all_cmping_monitors():
        if d not in all_servers:
            all_servers.append(d)
    skipped = [s for s in database.get_cmcall_skipped() if s in all_servers]
    servers = [s for s in all_servers if s not in skipped]
    results = [r for r in database.get_cmcall_results(limit=30) if r["src"] in servers and r["dst"] in servers]
    dc_helpers._send(bot, accid, msg.chat_id, format_status(servers, skipped, results, server_filter))


def format_history(events_list: list[dict], now=None) -> str:
    now = time.time() if now is None else now
    lines = ["📞 **CMCall Incident History:**", ""]
    if not events_list:
        lines.append("_No call incidents recorded._")
        return "\n".join(lines)
    for e in events_list:
        icon = "🚨" if e["kind"] == "down" else "⚠️"
        if e.get("ended_at"):
            took = cmping._format_duration(e["ended_at"] - e["started_at"])
            state_txt = f"{took}, resolved"
        else:
            state_txt = f"ongoing for {cmping._format_duration(now - e['started_at'])}"
        lines.append(f"{icon} {e['server']} {e['kind']} · {_gmt(e['started_at'])} · {state_txt}")
        if e.get("error"):
            lines.append(f"    {e['error']}")
    return "\n".join(lines)


@config.dc_cli.on(events.NewMessage(command="/cmcallhistory"))
def cmcallhistory_command(bot, accid, event):
    server_filter = (event.payload or "").strip().lower()
    rows = database.get_cmcall_events(limit=20, server_filter=server_filter or None)
    dc_helpers._send(bot, accid, event.msg.chat_id, format_history(rows))


@config.dc_cli.on(events.NewMessage(command="/cmcallskip"))
def cmcallskip_command(bot, accid, event):
    msg = event.msg
    server = (event.payload or "").strip().lower()
    if not server:
        skipped = database.get_cmcall_skipped()
        text = ("⏭ Skipped in call monitoring: " + ", ".join(skipped)) if skipped else \
            "No servers are skipped in call monitoring. Usage: /cmcallskip <server>"
        dc_helpers._send(bot, accid, msg.chat_id, text)
        return
    if not dc_helpers._is_dc_admin(bot, accid, msg.from_id):
        dc_helpers._send(bot, accid, msg.chat_id, "❌ Only the bot administrator can use /cmcallskip.")
        return
    if not config.DOMAIN_REGEX.match(server):
        dc_helpers._send(bot, accid, msg.chat_id, f"❌ Invalid server domain: `{server}`.")
        return
    database.add_cmcall_skip(server)
    sync_alerts(bot, accid, monitored_servers(bot, accid))
    dc_helpers._send(bot, accid, msg.chat_id, f"⏭ {server} is excluded from call monitoring (message monitoring is unchanged).")


@config.dc_cli.on(events.NewMessage(command="/cmcallunskip"))
def cmcallunskip_command(bot, accid, event):
    msg = event.msg
    server = (event.payload or "").strip().lower()
    if not dc_helpers._is_dc_admin(bot, accid, msg.from_id):
        dc_helpers._send(bot, accid, msg.chat_id, "❌ Only the bot administrator can use /cmcallunskip.")
        return
    if not server:
        dc_helpers._send(bot, accid, msg.chat_id, "Usage: /cmcallunskip <server>")
        return
    if database.remove_cmcall_skip(server):
        dc_helpers._send(bot, accid, msg.chat_id, f"✅ {server} is back in call monitoring.")
    else:
        dc_helpers._send(bot, accid, msg.chat_id, f"ℹ️ {server} was not skipped.")
