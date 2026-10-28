"""CMPing connectivity-monitoring engine: subprocess execution, output
parsing, the background monitor loop/cycle, and incident detection +
alert-message formatting/syncing. The Telegram-facing /cmping* commands
live in cmping_commands.py, which calls into this module."""
import json
import os
import re
import shutil
import signal
import subprocess
import time

import config
import database
import dc_helpers
import state

def _format_duration(seconds: int | float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    minutes = s // 60
    rem_s = s % 60
    if minutes < 60:
        return f"{minutes}m {rem_s}s" if rem_s > 0 else f"{minutes}m"
    hours = minutes // 60
    rem_m = minutes % 60
    if hours < 24:
        return f"{hours}h {rem_m}m" if rem_m > 0 else f"{hours}h"
    days = hours // 24
    rem_h = hours % 24
    return f"{days}d {rem_h}h" if rem_h > 0 else f"{days}d"


def _parse_single_cmping(stdout, stderr, returncode):
    # Split by lines and remove carriage returns, comment lines
    lines = []
    combined = (stdout or "") + "\n" + (stderr or "")
    for raw_line in combined.splitlines():
        line = raw_line.split('\r')[-1].strip()
        if not line or line.startswith('#'):
            continue
        lines.append(line)
        
    rtt_line = None
    for line in lines:
        if "rtt min/avg/max/mdev" in line:
            rtt_line = line
            break
            
    if returncode == 0 and rtt_line:
        match = re.search(r'rtt min/avg/max/mdev\s*=\s*([0-9.]+)/([0-9.]+)/([0-9.]+)/([0-9.]+)', rtt_line)
        if match:
            try:
                return {
                    "success": True,
                    "min": float(match.group(1)),
                    "avg": float(match.group(2)),
                    "max": float(match.group(3)),
                    "mdev": float(match.group(4))
                }
            except Exception:
                pass
                
    err_lines = [l for l in lines if l.startswith('✗') or 'fail' in l.lower() or 'error' in l.lower() or 'connect' in l.lower()]
    if not err_lines:
        err_lines = [l for l in lines if l]
    clean_errs = []
    for el in err_lines:
        if "cmping" in el.lower() and "usage" in el.lower():
            continue
        if el.startswith('✗'):
            el = el.lstrip('✗').strip()
        clean_errs.append(el)
        
    err_msg = " / ".join(clean_errs[-2:]) or "Unknown failure"
    return {
        "success": False,
        "error": err_msg
    }

def cmping_cmd(cmping_path, count, src, dst, timeout=60, both=False):
    """cmping command line for a run that _run_cmping_subprocess() kills after `timeout` seconds.

    cmping's own limits stay below `timeout`, so a run ends with a JSON result instead of
    being killed with a bare "Timeout expired":
    - -w: half the time (a third with --both, which pings twice) for the profiles to come
      online; a profile that does not fails as "<address> not online after Ns".
    - --grace: a sixth (at least 10 s) for missing pings after the last one is sent; they
      then count as lost.
    The rest is left for setting up the profiles (configure alone may take 20 s) and the pings."""
    cmd = [cmping_path, "-c", str(count), "-w", str(timeout // (3 if both else 2)),
           "--grace", str(max(10, timeout // 6)), "--json"]
    if both:
        cmd.append("--both")
    return cmd + [src, dst]


def _direction_result(direction):
    """One direction of cmping --json in the format of _parse_single_cmping()."""
    delivery = direction.get("delivery_ms")
    if direction.get("received") == direction.get("expected") and delivery:
        return {"success": True, "min": delivery["min"], "avg": delivery["avg"],
                "max": delivery["max"], "mdev": delivery["mdev"]}
    lost = (direction.get("expected") or 0) - (direction.get("received") or 0)
    return {"success": False, "error": f"{lost} of {direction.get('expected')} pings lost"}


def run_cmping(cmping_path, src, dst, count, timeout=60, both=False):
    """Run cmping --json from `src` to `dst` (and back with `both`).

    Returns (results, setup_error): one _parse_single_cmping()-style dict per direction
    (two with `both`), and whether cmping stopped before pinging (profiles could not be
    set up or did not come online), i.e. the error is about a relay, not a direction.
    When the output is not JSON (killed, crashed) the text is parsed as before."""
    n = 2 if both else 1
    try:
        stdout, stderr, rc = _run_cmping_subprocess(
            cmping_cmd(cmping_path, count, src, dst, timeout, both), timeout=timeout)
    except subprocess.TimeoutExpired:
        return [{"success": False, "error": f"Timeout expired ({timeout}s)"}] * n, False
    try:
        data = json.loads(stdout)
    except (TypeError, ValueError):
        return [_parse_single_cmping(stdout, stderr, rc)] * n, False
    if data.get("error"):
        return [{"success": False, "error": data["error"]}] * n, True
    results = [_direction_result(d) for d in data.get("directions", [])]
    while len(results) < n:
        results.append({"success": False, "error": "no result"})
    return results[:n], False


def _run_cmping_subprocess(cmd, timeout=60):
    """Run cmping as a subprocess with proper cleanup on timeout.
    Returns (stdout, stderr, returncode)."""
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, start_new_session=True
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        return stdout, stderr, proc.returncode
    except subprocess.TimeoutExpired:
        # Kill entire process group (cmping + deltachat-rpc-server children)
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, OSError):
            proc.kill()
        proc.communicate()  # drain pipes
        raise


def _cmping_monitor_loop(bot, accid):
    """Background loop: periodically checks server connectivity via cmping."""
    if config.CMPING_MONITOR_INTERVAL <= 0:
        config.logger.info("CMPing monitor disabled (CMPING_MONITOR_INTERVAL=0).")
        return

    config.logger.info(f"CMPing monitor loop started (interval={config.CMPING_MONITOR_INTERVAL}s).")
    time.sleep(60)  # Initial delay — let bot fully start up

    cmping_path = shutil.which("cmping") or "cmping"

    while True:
        try:
            _cmping_monitor_cycle(bot, accid, cmping_path)
        except Exception as e:
            config.logger.error(f"CMPing monitor cycle error: {e}")
        finally:
            state._cmping_monitor_running = False

        time.sleep(config.CMPING_MONITOR_INTERVAL)


def _cmping_monitor_cycle(bot, accid, cmping_path):
    """Execute one monitoring cycle: pick source, check all pairs, report changes."""
    if state._cmping_monitor_running:
        config.logger.warning("CMPing monitor: previous cycle still running, skipping.")
        return
    state._cmping_monitor_running = True

    # Build server list: bot transports + manually monitored
    bot_domains = dc_helpers._get_bot_domains(bot, accid)
    monitor_domains = database.get_all_cmping_monitors()
    all_servers = list(bot_domains)
    for d in monitor_domains:
        if d not in all_servers:
            all_servers.append(d)

    if len(all_servers) < 2:
        config.logger.debug("CMPing monitor: fewer than 2 servers, skipping cycle.")
        state._cmping_monitor_running = False
        return

    # Initialize server status map from DB if not done yet
    if not state._cmping_server_status:
        for srv in all_servers:
            state._set_cmping_server_status(srv, True)
        for (src, dst), res in state._cmping_last_results.items():
            if not res.get("success"):
                err = res.get("error", "Check failed")
                if src in all_servers:
                    state._set_cmping_server_status(src, False, f"Outgoing to {dst} failed: {err}")
                if dst in all_servers:
                    state._set_cmping_server_status(dst, False, f"Incoming from {src} failed: {err}")

    # Pick source via round-robin
    source_idx = state._cmping_monitor_index % len(all_servers)
    source = all_servers[source_idx]
    state._cmping_monitor_index = (state._cmping_monitor_index + 1) % len(all_servers)
    database.set_config("cmping_monitor_index", str(state._cmping_monitor_index))
    targets = [s for s in all_servers if s != source]

    config.logger.info(f"CMPing monitor cycle: source={source}, targets={targets}")

    # Track results of this cycle for health computation
    cycle_results = {}  # (dst) -> (fwd_result, bwd_result)
    any_source_success = False

    for dst in targets:
        # source -> dst and dst -> source in one cmping run (--both)
        fwd_result, bwd_result = _run_monitor_pair(cmping_path, source, dst)

        fwd_success = fwd_result.get("success")
        bwd_success = bwd_result.get("success")

        if fwd_success or bwd_success:
            any_source_success = True

        cycle_results[dst] = (fwd_result, bwd_result)

        # Update last results and database
        for direction, result, src_d, dst_d in [
            ("fwd", fwd_result, source, dst),
            ("bwd", bwd_result, dst, source),
        ]:
            key = (src_d, dst_d)
            state._cmping_last_results[key] = result
            if len(state._cmping_last_results) > 500:
                oldest_keys = sorted(state._cmping_last_results.keys(), key=lambda k: state._cmping_last_results[k].get("checked_at", 0))[:50]
                for ok in oldest_keys:
                    state._cmping_last_results.pop(ok, None)
            database.save_cmping_result(
                src=src_d,
                dst=dst_d,
                success=result.get("success"),
                error=result.get("error", ""),
                avg=result.get("avg", 0.0),
                checked_at=result.get("checked_at", 0.0)
            )
            if result.get("success"):
                database.add_cmping_history(
                    src=src_d,
                    dst=dst_d,
                    avg=result.get("avg", 0.0),
                    checked_at=result.get("checked_at", 0.0)
                )

    # Compute new health states and detect changes
    def _is_system_thread_error(err_str):
        if not err_str:
            return False
        err_lower = err_str.lower()
        return (
            "can't start new thread" in err_lower or
            "cannot allocate memory" in err_lower or
            "resource temporarily unavailable" in err_lower
        )

    now = int(time.time())

    # Did source fail with ALL targets? (and we checked at least 2 targets)
    if not any_source_success and len(targets) >= 2:
        # The source itself is the failing node (e.g. host down, broken mail/dns, network partition)
        old_source_healthy = state._cmping_server_status.get(source, True)
        sample_err = "All peer checks failed (node unreachable or broken mail delivery)"
        state._set_cmping_server_status(source, False, sample_err)
        if old_source_healthy:
            database.record_cmping_server_down(source, now, sample_err)
    else:
        # Source has at least one working connection or there's only 1 target
        old_source_healthy = state._cmping_server_status.get(source, True)
        source_healthy = True if (any_source_success or not targets) else False
        if source_healthy:
            state._set_cmping_server_status(source, True)
            if not old_source_healthy:
                database.record_cmping_server_up(source, now)
                _clear_all_errors_for_server(source)

        # Evaluate each target dst with this working source
        for dst in targets:
            fwd_res, bwd_res = cycle_results[dst]
            fwd_ok = fwd_res.get("success")
            bwd_ok = bwd_res.get("success")

            fwd_thread_err = not fwd_ok and _is_system_thread_error(fwd_res.get("error"))
            bwd_thread_err = not bwd_ok and _is_system_thread_error(bwd_res.get("error"))
            if fwd_thread_err or bwd_thread_err:
                continue

            old_dst_healthy = state._cmping_server_status.get(dst, True)
            dst_is_healthy = bool(fwd_ok and bwd_ok)

            if dst_is_healthy:
                state._set_cmping_server_status(dst, True)
                if not old_dst_healthy:
                    database.record_cmping_server_up(dst, now)
                    _clear_all_errors_for_server(dst)
            else:
                fwd_err = fwd_res.get("error", "Unknown error")
                bwd_err = bwd_res.get("error", "Unknown error")
                if not fwd_ok and not bwd_ok:
                    if fwd_err == bwd_err:
                        sample_err = f"both directions failed with {source}: {fwd_err}"
                    else:
                        sample_err = f"both directions failed with {source} (Incoming: {fwd_err}, Outgoing: {bwd_err})"
                elif not fwd_ok:
                    sample_err = f"incoming from {source} failed: {fwd_err}"
                else:
                    sample_err = f"outgoing to {source} failed: {bwd_err}"

                state._set_cmping_server_status(dst, False, sample_err)
                if old_dst_healthy:
                    database.record_cmping_server_down(dst, now, sample_err)

    # Sync incidents across subscribed report chats
    _sync_cmping_incident_alerts(bot, accid, all_servers)


def _run_monitor_pair(cmping_path, src, dst):
    """Check src -> dst and dst -> src in one cmping run (--both), with retry on failure.

    One run sets the profiles up once for both directions; two runs per pair did that
    twice. Uses the global lock to avoid conflicts with user /cmping commands.
    Returns (fwd, bwd) dicts with 'success', 'avg' (if success), 'error' (if failed),
    'checked_at'."""

    def do_check(count, timeout):
        try:
            state._cmping_global_lock.acquire()
            try:
                results, _setup_error = run_cmping(cmping_path, src, dst, count, timeout, both=True)
            finally:
                state._cmping_global_lock.release()
            return results
        except Exception as e:
            return [{"success": False, "error": str(e)}] * 2

    results = do_check(count=1, timeout=90)

    # If a direction failed, retry once with 5 pings after 10 seconds to filter transient
    # issues; only failed directions take the retry's result, a passed one keeps its own
    failed = [i for i, r in enumerate(results) if not r.get("success")]
    if failed:
        names = {0: f"{src} -> {dst}", 1: f"{dst} -> {src}"}
        details = "; ".join(f"{names[i]}: {results[i].get('error')}" for i in failed)
        config.logger.info(f"CMPing monitor: {details} failed, retrying with -c 5 in 10s...")
        time.sleep(10)
        retry = do_check(count=5, timeout=120)
        results = [retry[i] if i in failed else r for i, r in enumerate(results)]

    now = time.time()
    return tuple(dict(r, checked_at=now) for r in results)


def _clear_all_errors_for_server(server: str):
    """Clear all errors involving this server from results."""
    keys_to_remove = []
    for (src, dst), res in state._cmping_last_results.items():
        if src == server or dst == server:
            if not res.get("success"):
                keys_to_remove.append((src, dst))
                
    for key in keys_to_remove:
        src, dst = key
        state._cmping_last_results.pop(key, None)
        database.delete_cmping_result(src, dst)


def _format_cmping_incident_message(
    incident_id: int,
    started_at: int,
    all_servers: list[str],
    unhealthy_servers: dict[str, str],
    is_resolved: bool = False,
    resolved_at: int = None
) -> str:
    from datetime import datetime, timezone
    now = int(time.time())
    start_dt = datetime.fromtimestamp(started_at, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    bot_name = os.environ.get("DISPLAY_NAME", "Bouncer Bot")
    total_servers = len(all_servers)
    if is_resolved:
        resolved_ts = resolved_at or now
        resolved_dt = datetime.fromtimestamp(resolved_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        duration = max(1, resolved_ts - started_at)
        duration_str = _format_duration(duration)

        lines = [
            f"✅ **CMPing Incident #{incident_id}** — `Resolved`",
            f"⏱ **Duration:** `{duration_str}` (`{start_dt}` → `{resolved_dt} UTC`)",
            f"📊 **All {total_servers} monitored servers operational**"
        ]

        # Collect servers that experienced downtime during this incident
        affected_srvs = database.get_cmping_incident_affected_servers(incident_id, fallback_started_at=started_at)
        affected_srvs = [s for s in affected_srvs if s in all_servers]
        if affected_srvs:
            lines.append("\n**Recovered Servers:**")
            for srv in affected_srvs:
                lines.append(f"• ✅ **{srv}**")

        lines.append(f"\n_Generated by {bot_name}_")
        return "\n".join(lines)
    else:
        duration = max(1, now - started_at)
        duration_str = _format_duration(duration)
        unhealthy_count = len(unhealthy_servers)

        partially_recovered = False
        events = database.get_cmping_incident_downtime_events(incident_id)
        recent_ups = [
            ev for ev in events
            if ev.get("went_up_at") and ev["server"] not in unhealthy_servers
        ]
        if recent_ups and unhealthy_count > 0:
            partially_recovered = True

        if partially_recovered:
            status_tag = "Ongoing (Partial Recovery)"
            header_icon = "⚠️"
        else:
            status_tag = "Ongoing"
            header_icon = "🚨"

        lines = [
            f"{header_icon} **CMPing Incident #{incident_id}** — `{status_tag}`",
            f"⏱ **Started:** `{start_dt} UTC` (active for `{duration_str}`)",
            f"📊 **Affected:** {unhealthy_count} / {total_servers} servers unhealthy\n",
            "**Unhealthy Servers:**"
        ]

        for srv, err in unhealthy_servers.items():
            lines.append(f"• ❌ **{srv}**")
            if err:
                lines.append(f"  └─ Error: {err}")

        if partially_recovered:
            lines.append("\n**Recovered Servers:**")
            seen_recovered = set()
            for ev in recent_ups:
                srv = ev["server"]
                if srv not in seen_recovered and srv in all_servers and srv not in unhealthy_servers:
                    seen_recovered.add(srv)
                    rec_dt = datetime.fromtimestamp(ev["went_up_at"], tz=timezone.utc).strftime("%H:%M:%S")
                    lines.append(f"• ✅ **{srv}** (restored at `{rec_dt} UTC`)")

        lines.append(f"\n_Generated by {bot_name}_")
        return "\n".join(lines)


state._cmping_incident_last_edit_state = {}


def _get_cmping_incident_update_interval(duration_seconds: int) -> int:
    """Return the minimum seconds required between live ticker edits based on CMPing incident age."""
    if duration_seconds < 60:
        return 15       # First minute: update every 15 seconds
    elif duration_seconds < 300:
        return 30       # 1 to 5 minutes: update every 30 seconds
    elif duration_seconds < 3600:
        return 60       # 5 minutes to 1 hour: update every 1 minute
    elif duration_seconds < 86400:
        return 300      # 1 to 24 hours: update every 5 minutes
    else:
        return 3600     # After 24 hours: update once an hour


def _queue_cmping_incident_message(bot, accid, inc_id, chat_id, msg_text, store_new_id: bool):
    """One outbox job per incident and report chat: edits its message, or sends one.

    The message id is read when the job runs, so a first message still waiting in the outbox is
    merged with later updates and goes out once, with the latest text."""
    dc_helpers._edit_or_send(
        bot, accid, chat_id,
        lambda: database.get_cmping_incident_msg_ids(inc_id).get(chat_id),
        msg_text, priority=dc_helpers.PRIO_ALERT, key=("cmping_incident", inc_id, chat_id),
        on_new_id=(lambda new_id: database.set_cmping_incident_msg_id(inc_id, chat_id, new_id)) if store_new_id else None)


def _sync_cmping_incident_alerts(bot, accid, all_servers, force_update: bool = False):
    report_chats = database.get_all_cmping_report_chats()
    if not report_chats:
        return

    now = int(time.time())
    for srv, is_healthy in state._cmping_server_status.items():
        if not is_healthy and srv in all_servers:
            err = state._cmping_server_errors.get(srv, "Connectivity check failed")
            database.record_cmping_server_down(srv, now, err)

    active_incidents = database.get_all_active_cmping_incidents()
    if not active_incidents:
        return

    for inc in active_incidents:
        inc_id = inc["id"]
        events = database.get_cmping_incident_downtime_events(inc_id)
        open_events = [ev for ev in events if ev.get("went_up_at") is None]

        if open_events:
            # Ongoing incident
            started_at = inc["started_at"]
            duration = max(0, now - started_at)
            
            inc_unhealthy = {
                ev["server"]: ev.get("error_msg") or state._cmping_server_errors.get(ev["server"], "Connectivity check failed")
                for ev in open_events
            }

            status_sig = tuple(sorted(inc_unhealthy.items()))
            last_edit_time, last_sig = state._cmping_incident_last_edit_state.get(inc_id, (0.0, None))
            throttle_interval = _get_cmping_incident_update_interval(duration)

            chat_msg_ids = database.get_cmping_incident_msg_ids(inc_id)
            has_missing_msg = any(cid not in chat_msg_ids for cid in report_chats)

            should_edit = (status_sig != last_sig) or ((now - last_edit_time) >= throttle_interval) or has_missing_msg
            if not should_edit:
                continue

            msg_text = _format_cmping_incident_message(
                inc_id,
                started_at,
                all_servers,
                inc_unhealthy,
                is_resolved=False
            )

            state._cmping_incident_last_edit_state[inc_id] = (now, status_sig)
            for chat_id in report_chats:
                _queue_cmping_incident_message(bot, accid, inc_id, chat_id, msg_text, store_new_id=True)

        else:
            # Resolved incident
            state._cmping_incident_last_edit_state.pop(inc_id, None)

            affected_srvs = database.get_cmping_incident_affected_servers(inc_id, fallback_started_at=inc["started_at"])
            if affected_srvs:
                summary = f"Recovered: {', '.join(affected_srvs)}"
            else:
                summary = f"All {len(all_servers)} servers operational"
            database.resolve_cmping_incident(inc_id, now, summary)

            msg_text = _format_cmping_incident_message(
                inc_id,
                inc["started_at"],
                all_servers,
                {},
                is_resolved=True,
                resolved_at=now
            )

            for chat_id in report_chats:
                _queue_cmping_incident_message(bot, accid, inc_id, chat_id, msg_text, store_new_id=False)
