"""Telegram-facing CMPing commands: the background test worker
(/cmping) plus /cmpingadd, /cmpingdel, /cmpinglist, /cmreport,
/cmpingstatus, /cmpingfail, /cmfaillist, /cmpingevents, /cmpinghistory.

Execution primitives and incident formatting live in cmping.py."""
import concurrent.futures
import os
import shutil
import subprocess
import threading
import time
from datetime import datetime, timezone

from deltachat2 import events

import cmping
import config
import database
import dc_helpers
import state

def bg_cmping_worker(bot, accid, chat_id, msg_id, bot_domains, specified_servers):
    # Acquire global lock — only one cmping test at a time
    if not state._cmping_global_lock.acquire(blocking=False):
        dc_helpers._send(bot, accid, chat_id, "⏳ Another CMPing test is running, your request is queued...")
        state._cmping_global_lock.acquire()  # block until available

    try:
        _bg_cmping_worker_inner(bot, accid, chat_id, msg_id, bot_domains, specified_servers)
    finally:
        state._cmping_global_lock.release()

def _bg_cmping_worker_inner(bot, accid, chat_id, msg_id, bot_domains, specified_servers):
    cmping_path = shutil.which("cmping") or "cmping"
    
    # Handle 2 servers case: ping between them (there and back)
    if len(specified_servers) == 2:
        s1, s2 = specified_servers[0], specified_servers[1]
        
        def get_index_emoji_two(idx: int) -> str:
            emojis = ["1️⃣", "2️⃣"]
            if 1 <= idx <= 2:
                return emojis[idx - 1]
            return f"[{idx}]"

        emoji1 = get_index_emoji_two(1)
        emoji2 = get_index_emoji_two(2)

        legend_lines = [f"{emoji1} {s1}", f"{emoji2} {s2}"]

        def is_general_error_two(err_str):
            err_lower = err_str.lower()
            keywords = [
                "dns", "configure profile", "setup receiver", "setup sender", 
                "failed to configure", "imap failed", "smtp failed", "connect",
                "no such file", "invalid", "usage", "failed to setup"
            ]
            return any(kw in err_lower for kw in keywords)

        # Ping s1 -> s2
        forward_res = None
        forward_general = False
        try:
            cmd = [cmping_path, "-c", "3", s1, s2]
            config.logger.info(f"Running: {' '.join(cmd)}")
            stdout, stderr, rc = cmping._run_cmping_subprocess(cmd, timeout=60)
            res = cmping._parse_single_cmping(stdout, stderr, rc)
            if res.get("success"):
                forward_res = res
            else:
                forward_res = res
                forward_general = is_general_error_two(res.get("error", ""))
        except subprocess.TimeoutExpired:
            forward_res = {"success": False, "error": "Timeout expired (60s)"}
            forward_general = False
        except Exception as e:
            forward_res = {"success": False, "error": str(e)}
            forward_general = is_general_error_two(str(e))

        # Ping s2 -> s1
        backward_res = None
        try:
            cmd = [cmping_path, "-c", "3", s2, s1]
            config.logger.info(f"Running: {' '.join(cmd)}")
            stdout, stderr, rc = cmping._run_cmping_subprocess(cmd, timeout=60)
            res = cmping._parse_single_cmping(stdout, stderr, rc)
            if res.get("success"):
                backward_res = res
            else:
                backward_res = res
        except subprocess.TimeoutExpired:
            backward_res = {"success": False, "error": "Timeout expired (60s)"}
        except Exception as e:
            backward_res = {"success": False, "error": str(e)}

        report_body_lines = []
        all_failed = True
        
        # Forward line s1 -> s2
        if forward_res and forward_res.get("success"):
            forward_str = f"{forward_res['avg']:.1f} ms"
            report_body_lines.append(f"{emoji1}→{emoji2} {forward_str}")
            all_failed = False
        else:
            err_msg = forward_res.get("error", "Unknown error") if forward_res else "Unknown error"
            err_suffix = f" {err_msg}" if (err_msg and not forward_general) else ""
            report_body_lines.append(f"{emoji1}→{emoji2} ❌{err_suffix}")
            
        # Backward line s2 -> s1
        if backward_res:
            err_msg = backward_res.get("error", "")
            if backward_res.get("success"):
                backward_str = f"{backward_res['avg']:.1f} ms"
                report_body_lines.append(f"{emoji1}←{emoji2} {backward_str}")
                all_failed = False
            else:
                err_suffix = f" {err_msg}" if err_msg else ""
                report_body_lines.append(f"{emoji1}←{emoji2} ❌{err_suffix}")

        from datetime import datetime, timezone
        gmt_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M GMT")
        
        report_parts = [
            "🏓 **CMPing Report:**",
            "\n".join(legend_lines)
        ]
        
        body_content = "\n".join(report_body_lines).strip()
        if body_content:
            report_parts.append(body_content)
            
        bot_name = os.environ.get("DISPLAY_NAME", "Bouncer Bot")
        report_parts.append(f"Generated {gmt_time} by {bot_name}")

        if all_failed:
            dc_helpers._react(bot, accid, msg_id, "❌")
        else:
            dc_helpers._react(bot, accid, msg_id, "☑️")
            
        dc_helpers._send(bot, accid, chat_id, "\n\n".join(report_parts).strip())
        return

    all_domains = list(bot_domains)
    for s in specified_servers:
        if s not in all_domains:
            all_domains.append(s)
            
    def get_index_emoji(idx: int) -> str:
        emojis = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
        if 1 <= idx <= 10:
            return emojis[idx - 1]
        return f"[{idx}]"

    domain_to_emoji = {}
    for idx, d in enumerate(all_domains, 1):
        domain_to_emoji[d] = get_index_emoji(idx)

    legend_lines = []
    for d in all_domains:
        legend_lines.append(f"{domain_to_emoji[d]} {d}")

    def is_general_error(err_str):
        err_lower = err_str.lower()
        keywords = [
            "dns", "configure profile", "setup receiver", "setup sender", 
            "failed to configure", "imap failed", "smtp failed", "connect",
            "no such file", "invalid", "usage", "failed to setup"
        ]
        return any(kw in err_lower for kw in keywords)

    def run_pair_ping(h1, h2):
        unique_domains = sorted(list(set([h1, h2])))
        locks = [dc_helpers._get_domain_lock(d) for d in unique_domains]
        for lock in locks:
            lock.acquire()
        try:
            forward_res = None
            forward_general = False
            try:
                cmd = [cmping_path, "-c", "3", h1, h2]
                config.logger.info(f"Running: {' '.join(cmd)}")
                stdout, stderr, rc = cmping._run_cmping_subprocess(cmd, timeout=60)
                res = cmping._parse_single_cmping(stdout, stderr, rc)
                if res.get("success"):
                    forward_res = res
                else:
                    forward_res = res
                    forward_general = is_general_error(res.get("error", ""))
            except subprocess.TimeoutExpired:
                forward_res = {"success": False, "error": "Timeout expired (60s)"}
                forward_general = False
            except Exception as e:
                forward_res = {"success": False, "error": str(e)}
                forward_general = is_general_error(str(e))
                
            if forward_res and not forward_res.get("success") and forward_general:
                return (h1, h2, forward_res, forward_general, None)
                
            backward_res = None
            try:
                cmd = [cmping_path, "-c", "3", h2, h1]
                config.logger.info(f"Running: {' '.join(cmd)}")
                stdout, stderr, rc = cmping._run_cmping_subprocess(cmd, timeout=60)
                res = cmping._parse_single_cmping(stdout, stderr, rc)
                if res.get("success"):
                    backward_res = res
                else:
                    backward_res = res
            except subprocess.TimeoutExpired:
                backward_res = {"success": False, "error": "Timeout expired (60s)"}
            except Exception as e:
                backward_res = {"success": False, "error": str(e)}
                
            return (h1, h2, forward_res, forward_general, backward_res)
        finally:
            for lock in reversed(locks):
                lock.release()

    results_map = {}
    def run_relay_pings(h1):
        relay_results = []
        for h2 in specified_servers:
            res = run_pair_ping(h1, h2)
            relay_results.append((h2, res))
        return h1, relay_results

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(4, len(bot_domains)) if bot_domains else 1) as executor:
        futures = {executor.submit(run_relay_pings, host1): host1 for host1 in bot_domains}
        
        for f in concurrent.futures.as_completed(futures):
            host1 = futures[f]
            try:
                h1, relay_results = f.result()
                for host2, res in relay_results:
                    results_map[(h1, host2)] = res
            except Exception as e:
                config.logger.error(f"Error in relay pings for {host1}: {e}")
                for host2 in specified_servers:
                    results_map[(host1, host2)] = (host1, host2, {"success": False, "error": str(e)}, False, None)

    report_body_lines = []
    all_failed = True

    for host2 in specified_servers:
        group_lines = []
        emoji2 = domain_to_emoji[host2]
        
        for host1 in bot_domains:
            emoji1 = domain_to_emoji[host1]
            res_tuple = results_map.get((host1, host2))
            if not res_tuple:
                continue
                
            _, _, forward_res, forward_general, backward_res = res_tuple
            
            if forward_res and not forward_res.get("success") and forward_general:
                err_msg = forward_res.get("error", "General setup error")
                group_lines.append(f"{emoji1}→{emoji2} ❌ {err_msg}")
                continue
                
            # 1. Forward line
            if forward_res and forward_res.get("success"):
                forward_str = f"{forward_res['avg']:.1f} ms"
                group_lines.append(f"{emoji1}→{emoji2} {forward_str}")
                all_failed = False
            else:
                err_msg = forward_res.get("error", "Unknown error") if forward_res else "Unknown error"
                err_suffix = f" {err_msg}" if err_msg else ""
                group_lines.append(f"{emoji1}→{emoji2} ❌{err_suffix}")
                
            # 2. Backward line
            if backward_res:
                err_msg = backward_res.get("error", "")
                if err_msg != "Skipped (forward failed)":
                    if backward_res.get("success"):
                        backward_str = f"{backward_res['avg']:.1f} ms"
                        group_lines.append(f"{emoji1}←{emoji2} {backward_str}")
                        all_failed = False
                    else:
                        err_suffix = f" {err_msg}" if err_msg else ""
                        group_lines.append(f"{emoji1}←{emoji2} ❌{err_suffix}")
            
        if group_lines:
            report_body_lines.extend(group_lines)
            report_body_lines.append("")

    from datetime import datetime, timezone
    gmt_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M GMT")
    
    report_parts = [
        "🏓 **CMPing Report:**",
        "\n".join(legend_lines)
    ]
    
    body_content = "\n".join(report_body_lines).strip()
    if body_content:
        report_parts.append(body_content)
        
    bot_name = os.environ.get("DISPLAY_NAME", "Bouncer Bot")
    report_parts.append(f"Generated {gmt_time} by {bot_name}")

    if all_failed:
        dc_helpers._react(bot, accid, msg_id, "❌")
    else:
        dc_helpers._react(bot, accid, msg_id, "☑️")
        
    dc_helpers._send(bot, accid, chat_id, "\n\n".join(report_parts).strip())

@config.dc_cli.on(events.NewMessage(command="/cmping"))
def cmping_command(bot, accid, event, skip_cooldown: bool = False):
    msg = event.msg
    
    # 1. Check cooldown (debounce) - 15 seconds (admins are exempt)
    now = time.time()
    is_admin = dc_helpers._is_dc_admin(bot, accid, msg.from_id)
    if not is_admin and not skip_cooldown:
        last_run = state._chat_cmping_anti_spam.get(msg.chat_id, 0)
        diff = now - last_run
        if diff < config.CMPING_COOLDOWN_SECONDS:
            remaining_sec = max(1, int(config.CMPING_COOLDOWN_SECONDS - diff))
            dc_helpers._queue_delayed_command(bot, accid, msg, "cmping", remaining_sec, cmping_command, bot, accid, event, skip_cooldown=True)
            return
        
    # 2. Parse command arguments
    payload_str = event.payload.strip() if event.payload else ""
    specified_servers = [s.strip().lower() for s in payload_str.split() if s.strip()]
    
    if not specified_servers:
        dc_helpers._send(bot, accid, msg.chat_id, "Usage: /cmping <server> OR /cmping <server1> <server2>")
        return

    if len(specified_servers) > 2:
        dc_helpers._send(bot, accid, msg.chat_id, "❌ Only 1 or 2 server parameters are supported. Usage: /cmping <server> OR /cmping <server1> <server2>")
        return

    for s in specified_servers:
        if not config.DOMAIN_REGEX.match(s):
            dc_helpers._send(bot, accid, msg.chat_id, f"❌ Invalid server domain: `{s}`. Please specify a valid domain name.")
            return

    # Update cooldown
    state._chat_cmping_anti_spam[msg.chat_id] = now
    
    # 3. Add reaction ⏳
    dc_helpers._react(bot, accid, msg.id, "⏳")
    
    # 4. Get bot transport domains
    bot_domains = dc_helpers._get_bot_domains(bot, accid)

    if not bot_domains:
        dc_helpers._react(bot, accid, msg.id, "❌")
        dc_helpers._send(bot, accid, msg.chat_id, "❌ Error: Could not determine bot's own transport domain.")
        return

    # 5. Spawn background thread to run cmping
    threading.Thread(
        target=bg_cmping_worker,
        args=(bot, accid, msg.chat_id, msg.id, bot_domains, specified_servers),
        daemon=True
    ).start()

@config.dc_cli.on(events.NewMessage(command="/cmpingadd"))
def cmpingadd_command(bot, accid, event):
    msg = event.msg
    if not dc_helpers._is_dc_admin(bot, accid, msg.from_id):
        dc_helpers._send(bot, accid, msg.chat_id, "❌ Only the bot administrator can use /cmpingadd.")
        return

    text = msg.text.strip()
    parts = text.split(None, 1)
    if len(parts) < 2 or not parts[1].strip():
        dc_helpers._send(bot, accid, msg.chat_id, "Usage: /cmpingadd <server>\nExample: /cmpingadd talklink.fun")
        return

    domain = parts[1].strip().lower()
    # Validate domain
    if not config.DOMAIN_REGEX.match(domain):
        dc_helpers._send(bot, accid, msg.chat_id, f"❌ Invalid domain: {domain}")
        return

    # Check if already a bot transport
    bot_domains = dc_helpers._get_bot_domains(bot, accid)
    if domain in bot_domains:
        dc_helpers._send(bot, accid, msg.chat_id, f"ℹ️ {domain} is already a bot transport, no need to add it.")
        return

    # Check if already monitored
    if database.is_cmping_monitor(domain):
        dc_helpers._send(bot, accid, msg.chat_id, f"ℹ️ {domain} is already in monitoring.")
        return

    database.add_cmping_monitor(domain)
    total_monitors = len(database.get_all_cmping_monitors())
    total = len(bot_domains) + total_monitors
    dc_helpers._send(bot, accid, msg.chat_id,
          f"✅ {domain} added to monitoring.\nTotal: {len(bot_domains)} transport + {total_monitors} monitored = {total} servers.")

@config.dc_cli.on(events.NewMessage(command="/cmpingdel"))
def cmpingdel_command(bot, accid, event):
    msg = event.msg
    if not dc_helpers._is_dc_admin(bot, accid, msg.from_id):
        dc_helpers._send(bot, accid, msg.chat_id, "❌ Only the bot administrator can use /cmpingdel.")
        return

    text = msg.text.strip()
    parts = text.split(None, 1)
    if len(parts) < 2 or not parts[1].strip():
        dc_helpers._send(bot, accid, msg.chat_id, "Usage: /cmpingdel <server>\nExample: /cmpingdel talklink.fun")
        return

    domain = parts[1].strip().lower()
    if not config.DOMAIN_REGEX.match(domain):
        dc_helpers._send(bot, accid, msg.chat_id, f"❌ Invalid domain: {domain}")
        return
    removed = database.remove_cmping_monitor(domain)
    if removed:
        # Clean up last results for this domain
        keys_to_remove = [k for k in state._cmping_last_results if domain in k]
        for k in keys_to_remove:
            state._cmping_last_results.pop(k, None)
        state._cmping_server_status.pop(domain, None)
        state._cmping_server_errors.pop(domain, None)
        database.delete_cmping_results_for_domain(domain)
        database.delete_cmping_history_for_domain(domain)
        dc_helpers._send(bot, accid, msg.chat_id, f"✅ {domain} removed from monitoring.")


    else:
        dc_helpers._send(bot, accid, msg.chat_id, f"❌ {domain} is not in monitoring list.")

@config.dc_cli.on(events.NewMessage(command="/cmpinglist"))
def cmpinglist_command(bot, accid, event):
    msg = event.msg

    bot_domains = dc_helpers._get_bot_domains(bot, accid)
    monitor_domains = database.get_all_cmping_monitors()

    all_servers = list(bot_domains)
    for d in monitor_domains:
        if d not in all_servers:
            all_servers.append(d)

    lines = ["📡 **CMPing Monitor Servers:**\n"]

    def format_server_entry(idx, domain):
        avg_ping, count = database.get_average_ping_for_server(domain, limit=100)
        if avg_ping is not None:
            if avg_ping < 2000:
                circle = "🟢"
            elif avg_ping < 4000:
                circle = "🟡"
            elif avg_ping < 6000:
                circle = "🟠"
            else:
                circle = "🔴"

            return f"  {idx}. {domain}: {circle} {avg_ping:.1f} ms (🏓 {count})"

        else:
            return f"  {idx}. {domain}: ⚪️ no data"


    if bot_domains:
        lines.append("**Transport servers (auto):**")
        for i, d in enumerate(bot_domains, 1):
            lines.append(format_server_entry(i, d))
    else:
        lines.append("_No transport servers detected._")

    if monitor_domains:
        lines.append("\n**Monitored servers (manual):**")
        offset = len(bot_domains)
        for i, d in enumerate(monitor_domains, offset + 1):
            lines.append(format_server_entry(i, d))
    else:
        lines.append("\n_No manually added servers._")


    n = len(all_servers)
    pairs = n * (n - 1) // 2
    lines.append(f"\nTotal: {n} servers, {pairs} unique pairs")

    if n >= 2:
        source_idx = state._cmping_monitor_index % n
        next_source = all_servers[source_idx]
        interval_min = config.CMPING_MONITOR_INTERVAL // 60
        lines.append(f"Next check source: {next_source}")
        lines.append(f"Interval: {interval_min} min | Full rotation: ~{n * interval_min} min")

    dc_helpers._send(bot, accid, msg.chat_id, "\n".join(lines))

@config.dc_cli.on(events.NewMessage(command="/cmreport"))
def cmreport_command(bot, accid, event):
    msg = event.msg
    if not dc_helpers._is_dc_admin(bot, accid, msg.from_id):
        dc_helpers._send(bot, accid, msg.chat_id, "❌ Only the bot administrator can use /cmreport.")
        return

    text = msg.text.strip()
    parts = text.split(None, 1)
    arg = parts[1].strip().lower() if len(parts) > 1 else ""

    if arg == "on":
        database.add_cmping_report_chat(msg.chat_id)
        dc_helpers._send(bot, accid, msg.chat_id, "✅ CMPing monitoring alerts **enabled** for this chat.")
    elif arg == "off":
        removed = database.remove_cmping_report_chat(msg.chat_id)
        if removed:
            dc_helpers._send(bot, accid, msg.chat_id, "✅ CMPing monitoring alerts **disabled** for this chat.")
        else:
            dc_helpers._send(bot, accid, msg.chat_id, "ℹ️ CMPing monitoring alerts were not enabled for this chat.")
    else:
        # Show status
        from datetime import datetime, timezone
        is_enabled = database.is_cmping_report_chat(msg.chat_id)
        if is_enabled:
            enabled_at = database.get_cmping_report_chat_enabled_at(msg.chat_id)
            if enabled_at:
                dt = datetime.fromtimestamp(enabled_at, tz=timezone.utc).strftime("%Y-%m-%d %H:%M GMT")
                dc_helpers._send(bot, accid, msg.chat_id, f"📊 CMPing monitoring alerts: ✅ enabled (since {dt})\n\nUse `/cmreport off` to disable.")
            else:
                dc_helpers._send(bot, accid, msg.chat_id, "📊 CMPing monitoring alerts: ✅ enabled\n\nUse `/cmreport off` to disable.")
        else:
            dc_helpers._send(bot, accid, msg.chat_id, "📊 CMPing monitoring alerts: ❌ disabled\n\nUse `/cmreport on` to enable.")

@config.dc_cli.on(events.NewMessage(command="/cmpingstatus"))
def cmpingstatus_command(bot, accid, event):
    msg = event.msg

    from datetime import datetime, timezone

    bot_domains = dc_helpers._get_bot_domains(bot, accid)
    monitor_domains = database.get_all_cmping_monitors()
    all_servers = list(bot_domains)
    for d in monitor_domains:
        if d not in all_servers:
            all_servers.append(d)

    if len(all_servers) < 2:
        dc_helpers._send(bot, accid, msg.chat_id, "ℹ️ Fewer than 2 servers configured for monitoring.")
        return

    now = time.time()

    # Parse optional filter argument
    text = (msg.text or "").strip()
    parts = text.split(None, 1)
    filter_domain = parts[1].strip().lower() if len(parts) > 1 else None

    if filter_domain:
        matched_servers = [srv for srv in all_servers if filter_domain in srv]
        if not matched_servers:
            dc_helpers._send(bot, accid, msg.chat_id, f"❌ No monitored servers match **{filter_domain}**.")
            return
        lines = [f"📊 **CMPing Monitor Status for '{filter_domain}':**\n"]
    else:
        matched_servers = None
        lines = ["📊 **CMPing Monitor Status:**\n"]

    # Collect relevant results
    results_to_show = []
    for (src, dst), result in state._cmping_last_results.items():
        if src not in all_servers or dst not in all_servers or src == dst:
            continue
        if matched_servers is not None:
            if src not in matched_servers and dst not in matched_servers:
                continue
        results_to_show.append((src, dst, result))

    # Sort from newest to oldest: checked_at descending.
    # If checked_at is missing, default to 0.
    results_to_show.sort(key=lambda x: x[2].get("checked_at", 0), reverse=True)

    has_any = len(results_to_show) > 0
    for src, dst, result in results_to_show:
        checked_at = result.get("checked_at", 0)
        age_min = int((now - checked_at) / 60) if checked_at else 0

        if result.get("success"):
            avg = result.get("avg", 0.0)
            if avg < 2000:
                circle = "🟢"
            elif avg < 4000:
                circle = "🟡"
            elif avg < 6000:
                circle = "🟠"
            else:
                circle = "🔴"

            lines.append(f"✅ {src} → {dst}: {circle} {avg:.1f} ms ({age_min} min ago)")

        else:
            err = result.get("error", "Unknown")
            lines.append(f"❌ {src} → {dst}: {err} ({age_min} min ago)")

    if not has_any:
        if filter_domain:
            lines.append("_No checks performed yet for the matching servers._")
        else:
            lines.append("_No checks performed yet. Monitoring starts after bot startup._")

    # Add rotation info
    n = len(all_servers)
    source_idx = state._cmping_monitor_index % n
    next_source = all_servers[source_idx]
    interval_min = config.CMPING_MONITOR_INTERVAL // 60
    lines.append(f"\nSource rotation: {source_idx + 1}/{n} (next: {next_source})")
    lines.append(f"Interval: {interval_min} min")

    dc_helpers._send(bot, accid, msg.chat_id, "\n".join(lines))

def _cmpingfail_impl(bot, accid, event, cmd_name="/cmpingfail"):
    msg = event.msg

    bot_domains = dc_helpers._get_bot_domains(bot, accid)
    monitor_domains = database.get_all_cmping_monitors()
    all_servers = list(bot_domains)
    for d in monitor_domains:
        if d not in all_servers:
            all_servers.append(d)

    if len(all_servers) < 2:
        dc_helpers._send(bot, accid, msg.chat_id, "ℹ️ Fewer than 2 servers configured for monitoring.")
        return

    now = time.time()
    
    if not state._cmping_server_status:
        for srv in all_servers:
            state._set_cmping_server_status(srv, True)
        for (src, dst), res in state._cmping_last_results.items():
            if not res.get("success"):
                if src in all_servers:
                    state._set_cmping_server_status(src, False)
                if dst in all_servers:
                    state._set_cmping_server_status(dst, False)

    unhealthy_servers = [srv for srv in all_servers if not state._cmping_server_status.get(srv, True)]

    # Parse optional filter argument
    text = (msg.text or "").strip()
    parts = text.split(None, 1)
    filter_domain = parts[1].strip().lower() if len(parts) > 1 else None

    if filter_domain:
        matched_unhealthy = [srv for srv in unhealthy_servers if filter_domain in srv]
        if not matched_unhealthy:
            matched_all = [srv for srv in all_servers if filter_domain in srv]
            if matched_all:
                matched_names = ", ".join(matched_all)
                dc_helpers._send(bot, accid, msg.chat_id, f"✅ Server(s) matching **{filter_domain}** ({matched_names}) are currently **HEALTHY**.")
            else:
                dc_helpers._send(bot, accid, msg.chat_id, f"❌ No monitored servers match **{filter_domain}**.")
            return
        unhealthy_servers = matched_unhealthy
        lines = [f"🔴 **CMPing Monitor Failures for '{filter_domain}' (Grouped by Server):**\n"]
    else:
        if not unhealthy_servers:
            dc_helpers._send(bot, accid, msg.chat_id, "✅ **All monitored links are healthy.**\nNo failures detected at the moment.")
            return
        lines = ["🔴 **Current CMPing Monitor Failures (Grouped by Server):**\n"]

    for srv in unhealthy_servers:
        lines.append(f"❌ **{srv}**")
        
        # Collect outgoing failures (srv -> dst)
        out_fails = []
        # Collect incoming failures (src -> srv)
        in_fails = []
        
        for (src, dst), res in state._cmping_last_results.items():
            if not res.get("success"):
                checked_at = res.get("checked_at", 0)
                age_min = int((now - checked_at) / 60) if checked_at else 0
                err = res.get("error", "Unknown")
                
                if src == srv:
                    out_fails.append(f"  ├─ Outgoing to **{dst}**: {err} ({age_min} min ago)")
                elif dst == srv:
                    in_fails.append(f"  ├─ Incoming from **{src}**: {err} ({age_min} min ago)")
                    
        if out_fails:
            lines.append("  *Outgoing:*")
            lines.extend(out_fails)
        if in_fails:
            lines.append("  *Incoming:*")
            lines.extend(in_fails)
        if not out_fails and not in_fails:
            lines.append("  └─ No active failure details found (waiting for next cycle)")
        lines.append("")  # Empty line separator

    dc_helpers._send(bot, accid, msg.chat_id, "\n".join(lines).strip())


@config.dc_cli.on(events.NewMessage(command="/cmpingfail"))
def cmpingfail_command(bot, accid, event):
    _cmpingfail_impl(bot, accid, event, cmd_name="/cmpingfail")


@config.dc_cli.on(events.NewMessage(command="/cmfaillist"))
def cmfaillist_command(bot, accid, event):
    _cmpingfail_impl(bot, accid, event, cmd_name="/cmfaillist")


@config.dc_cli.on(events.NewMessage(command="/cmpingevents"))
@config.dc_cli.on(events.NewMessage(command="/cmpingincidents"))
@config.dc_cli.on(events.NewMessage(command="/cmevents"))
def cmpingevents_command(bot, accid, event):
    from datetime import datetime, timezone
    msg = event.msg
    text = (msg.text or "").strip()
    parts = text.split(None, 1)
    arg = parts[1].strip() if len(parts) > 1 else ""

    bot_domains = dc_helpers._get_bot_domains(bot, accid)
    monitor_domains = database.get_all_cmping_monitors()
    all_servers = list(dict.fromkeys(list(bot_domains) + monitor_domains))

    if arg:
        try:
            inc_id = int(arg.lstrip("#"))
        except ValueError:
            dc_helpers._send(bot, accid, msg.chat_id, "❌ Invalid incident ID. Usage: `/cmpingevents <id>`")
            return

        inc = database.get_cmping_incident_by_id(inc_id)
        if not inc:
            dc_helpers._send(bot, accid, msg.chat_id, f"❌ Incident #{inc_id} not found.")
            return

        if inc["status"] == "ongoing":
            unhealthy_servers = {
                srv: state._cmping_server_errors.get(srv, "Connectivity check failed")
                for srv, is_healthy in state._cmping_server_status.items()
                if not is_healthy and srv in all_servers
            }
            msg_text = cmping._format_cmping_incident_message(
                inc["id"],
                inc["started_at"],
                all_servers,
                unhealthy_servers,
                is_resolved=False
            )
            dc_helpers._send(bot, accid, msg.chat_id, msg_text)
        else:
            started_at = inc["started_at"]
            resolved_at = inc["resolved_at"] or int(time.time())
            start_dt = datetime.fromtimestamp(started_at, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            resolved_dt = datetime.fromtimestamp(resolved_at, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            duration_str = cmping._format_duration(max(1, resolved_at - started_at))

            inc_events = [
                ev for ev in database.get_all_cmping_downtime_events(limit=50)
                if ev["went_down_at"] >= started_at - 60 and ev["went_down_at"] <= resolved_at + 60
            ]

            lines = [
                f"✅ **CMPing Incident #{inc['id']} Details** — `Resolved`",
                f"⏱ **Duration:** `{duration_str}` (`{start_dt}` → `{resolved_dt} UTC`)",
                f"📋 **Summary:** {inc.get('summary') or 'All monitored servers operational'}\n"
            ]

            if inc_events:
                lines.append("**Outages Recorded During Incident:**")
                for ev in inc_events:
                    srv = ev["server"]
                    down_dt = datetime.fromtimestamp(ev["went_down_at"], tz=timezone.utc).strftime("%H:%M:%S")
                    up_dt = datetime.fromtimestamp(ev["went_up_at"], tz=timezone.utc).strftime("%H:%M:%S") if ev.get("went_up_at") else "ongoing"
                    down_dur = cmping._format_duration(max(1, (ev.get("went_up_at") or resolved_at) - ev["went_down_at"]))
                    err_reason = ev.get("error_msg") or "Connectivity check failed"
                    lines.append(f"• ❌ **{srv}** (`{down_dt}` → `{up_dt} UTC`, `{down_dur}`)\n  └─ Reason: {err_reason}")
            else:
                lines.append("✨ All monitored servers returned to healthy state.")

            dc_helpers._send(bot, accid, msg.chat_id, "\n".join(lines))
        return

    # List view
    recent_incs = database.get_recent_cmping_incidents(limit=10)
    if not recent_incs:
        dc_helpers._send(bot, accid, msg.chat_id, "✨ **No CMPing incidents recorded!**\nAll monitored chatmail relays and servers are healthy.")
        return

    lines = [f"📋 **CMPing Incident Log ({len(recent_incs)} most recent):**\n"]
    for inc in recent_incs:
        inc_id = inc["id"]
        status = inc["status"]
        started_at = inc["started_at"]
        resolved_at = inc["resolved_at"]

        start_str = datetime.fromtimestamp(started_at, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        if status == "ongoing":
            duration_str = cmping._format_duration(int(time.time()) - started_at)
            unhealthy_servers = {
                srv: state._cmping_server_errors.get(srv, "Connectivity check failed")
                for srv, is_healthy in state._cmping_server_status.items()
                if not is_healthy and srv in all_servers
            }
            unhealthy_count = len(unhealthy_servers)
            lines.append(
                f"• 🚨 **Incident #{inc_id}** — `Ongoing`\n"
                f"  Started: `{start_str} UTC` (active for `{duration_str}`)"
            )
            if unhealthy_servers:
                lines.append(f"  **Affected ({unhealthy_count}):**")
                for srv, err in unhealthy_servers.items():
                    err_short = (err[:70] + "...") if len(err) > 70 else err
                    lines.append(f"  • ❌ **{srv}** — `{err_short}`")
            lines.append("")
        else:
            resolved_str = datetime.fromtimestamp(resolved_at, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S") if resolved_at else "Resolved"
            duration = max(1, (resolved_at or int(time.time())) - started_at)
            duration_str = cmping._format_duration(duration)
            summary_txt = inc.get("summary")
            summary_line = f"\n  Status: `{summary_txt}`" if summary_txt else ""
            lines.append(
                f"• ✅ **Incident #{inc_id}** — `Resolved`\n"
                f"  Duration: `{duration_str}` (`{start_str}` → `{resolved_str} UTC`){summary_line}\n"
            )

    lines.append("💡 Tip: Use `/cmpingevents <id>` for full incident details or `/cmpinghistory <server>` for server downtime history.")
    dc_helpers._send(bot, accid, msg.chat_id, "\n".join(lines).strip())


@config.dc_cli.on(events.NewMessage(command="/cmpinghistory"))
@config.dc_cli.on(events.NewMessage(command="/cmhistory"))
def cmpinghistory_command(bot, accid, event):
    from datetime import datetime, timezone
    msg = event.msg
    text = (msg.text or "").strip()
    parts = text.split(None, 1)
    server_arg = parts[1].strip().lower() if len(parts) > 1 else ""

    bot_domains = dc_helpers._get_bot_domains(bot, accid)
    monitor_domains = database.get_all_cmping_monitors()
    all_servers = list(bot_domains)
    for d in monitor_domains:
        if d not in all_servers:
            all_servers.append(d)

    if not server_arg:
        if not all_servers:
            dc_helpers._send(bot, accid, msg.chat_id, "ℹ️ No servers currently monitored.")
            return

        lines = [
            "📜 **CMPing Downtime History Guide**\n",
            "To view downtime records for a specific server, use `/cmpinghistory <server>`.\n",
            "**Monitored servers:**"
        ]
        for srv in all_servers:
            is_healthy = state._cmping_server_status.get(srv, True)
            icon = "🟢" if is_healthy else "🔴"
            status_txt = "HEALTHY" if is_healthy else "UNHEALTHY"
            lines.append(f"• {icon} **{srv}** — `{status_txt}`")

        lines.append("\n💡 Tip: Use `/cmpingevents` to view the incident history log.")
        dc_helpers._send(bot, accid, msg.chat_id, "\n".join(lines))
        return

    # Match server (exact or partial)
    matching_server = next((s for s in all_servers if s == server_arg), None)
    if not matching_server:
        matching_server = next((s for s in all_servers if server_arg in s), None)

    target_server = matching_server or server_arg
    events_list = database.get_server_cmping_downtime_events(target_server, limit=10)

    is_healthy = state._cmping_server_status.get(target_server, True)
    icon = "🟢" if is_healthy else "🔴"
    status_txt = "HEALTHY" if is_healthy else "UNHEALTHY"

    lines = [
        f"📜 **CMPing Downtime History for {target_server}**",
        f"Current Status: {icon} `{status_txt}`\n"
    ]

    if not events_list:
        lines.append("✨ No downtime records found for this server!")
    else:
        lines.append(f"**Recorded Outages ({len(events_list)} most recent):**")
        for ev in events_list:
            went_down = ev["went_down_at"]
            went_up = ev["went_up_at"]
            error_reason = ev.get("error_msg") or "Connectivity check failed"

            down_str = datetime.fromtimestamp(went_down, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            if went_up:
                up_str = datetime.fromtimestamp(went_up, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                duration_str = cmping._format_duration(max(1, went_up - went_down))
                lines.append(f"• `{down_str}` → `{up_str} UTC` (`{duration_str}`) — `{error_reason}`")
            else:
                duration_str = cmping._format_duration(max(1, int(time.time()) - went_down))
                lines.append(f"• 🔴 `{down_str} UTC` → `Ongoing` (`{duration_str}`) — `{error_reason}`")

    dc_helpers._send(bot, accid, msg.chat_id, "\n".join(lines))


# ==========================================
# VirusTotal URL & File Inspection Subsystem
# ==========================================
