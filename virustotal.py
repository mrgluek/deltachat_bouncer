"""VirusTotal URL & File Inspection subsystem: the API v3 client, report
formatting, and the /virus command (with a FIFO-queued background worker so
only one scan runs against the VirusTotal rate limit at a time)."""
import base64
import hashlib
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone

from deltachat2 import events

import config
import dc_helpers
import state

def _format_file_size(size_bytes: int) -> str:
    """Format byte count into human-readable representation."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    else:
        return f"{size_bytes / (1024 * 1024 * 1024):.1f} GB"


def _format_vt_timestamp(ts: int | float | None) -> str:
    """Format UNIX epoch timestamp into UTC string."""
    if not ts:
        return "Unknown"
    try:
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M:%S UTC")
    except Exception:
        return "Unknown"


def _vt_wait_rate_limit():
    """Ensure at least 15 seconds have passed since the previous VirusTotal API call."""
    now = time.time()
    elapsed = now - state._vt_last_request_time
    if elapsed < config.VIRUSTOTAL_RATE_LIMIT_SECONDS:
        time.sleep(config.VIRUSTOTAL_RATE_LIMIT_SECONDS - elapsed)
    state._vt_last_request_time = time.time()


def _vt_api_request(method: str, endpoint: str, vt_key: str, data: dict | None = None) -> tuple[dict | None, int, str | None]:
    """Execute a request against the VirusTotal API v3.
    Returns (response_dict, status_code, error_message).
    """
    _vt_wait_rate_limit()
    url = f"https://www.virustotal.com/api/v3{endpoint}"
    headers = {
        "x-apikey": vt_key,
        "User-Agent": f"BouncerBot/{config.VERSION}",
        "Accept": "application/json",
    }
    req_body = None
    if data is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        req_body = urllib.parse.urlencode(data).encode("utf-8")

    req = urllib.request.Request(url, data=req_body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            content = resp.read().decode("utf-8")
            return json.loads(content) if content else {}, resp.status, None
    except urllib.error.HTTPError as e:
        err_msg = None
        try:
            err_json = json.loads(e.read().decode("utf-8"))
            err_msg = err_json.get("error", {}).get("message")
        except Exception:
            pass
        if e.code == 401:
            return None, e.code, "Invalid VirusTotal API key. Please check VIRUSTOTAL_API_KEY in .env."
        elif e.code == 429:
            return None, e.code, "VirusTotal API rate limit or quota exceeded. Please try again later."
        elif e.code == 404:
            return None, e.code, err_msg or "Resource not found on VirusTotal"
        else:
            return None, e.code, err_msg or f"VirusTotal HTTP {e.code}: {e.reason}"
    except Exception as e:
        return None, 0, f"Network error connecting to VirusTotal: {e}"


def _vt_upload_file(file_path: str, filename: str, vt_key: str) -> tuple[dict | None, int, str | None]:
    """Upload a file up to 32MB to VirusTotal API v3 via multipart/form-data.
    Returns (response_dict, status_code, error_message).
    """
    _vt_wait_rate_limit()
    url = "https://www.virustotal.com/api/v3/files"
    boundary = f"----WebKitFormBoundary{uuid.uuid4().hex}"

    try:
        with open(file_path, "rb") as f:
            file_bytes = f.read()
    except Exception as e:
        return None, 0, f"Failed to read file for upload: {e}"

    part_header = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: application/octet-stream\r\n\r\n"
    ).encode("utf-8")
    part_footer = f"\r\n--{boundary}--\r\n".encode("utf-8")
    body = part_header + file_bytes + part_footer

    headers = {
        "x-apikey": vt_key,
        "User-Agent": f"BouncerBot/{config.VERSION}",
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "Content-Length": str(len(body)),
        "Accept": "application/json",
    }
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            content = resp.read().decode("utf-8")
            return json.loads(content) if content else {}, resp.status, None
    except urllib.error.HTTPError as e:
        err_msg = None
        try:
            err_json = json.loads(e.read().decode("utf-8"))
            err_msg = err_json.get("error", {}).get("message")
        except Exception:
            pass
        if e.code == 401:
            return None, e.code, "Invalid VirusTotal API key. Please check VIRUSTOTAL_API_KEY in .env."
        elif e.code == 429:
            return None, e.code, "VirusTotal API rate limit or quota exceeded. Please try again later."
        else:
            return None, e.code, err_msg or f"VirusTotal HTTP {e.code}: {e.reason}"
    except Exception as e:
        return None, 0, f"Network error connecting to VirusTotal: {e}"


def _format_vt_url_report(url: str, url_id: str, attrs: dict) -> tuple[str, str]:
    """Format VirusTotal analysis result for a URL. Returns (report_text, reaction_emoji)."""
    stats = attrs.get("last_analysis_stats") or attrs.get("stats") or {}
    results = attrs.get("last_analysis_results") or attrs.get("results") or {}
    scan_date = attrs.get("last_analysis_date") or attrs.get("date")

    malicious = int(stats.get("malicious", 0))
    suspicious = int(stats.get("suspicious", 0))
    harmless = int(stats.get("harmless", 0))
    undetected = int(stats.get("undetected", 0))
    total = malicious + suspicious + harmless + undetected

    if malicious > 0:
        verdict_emoji = "🚨"
        verdict_label = "Malicious"
    elif suspicious > 0:
        verdict_emoji = "⚠️"
        verdict_label = "Suspicious"
    elif total == 0:
        verdict_emoji = "⚪️"
        verdict_label = "No Data"
    else:
        verdict_emoji = "🟢"
        verdict_label = "Clean"

    lines = [
        f"{verdict_emoji} **VirusTotal URL Report — {verdict_label}**\n",
        f"🔗 **URL:** {url}",
        f"🛡️ **Detections:** {malicious + suspicious}/{total} security vendors",
        f"📊 **Stats:** 🟢 {harmless} harmless | 🔴 {malicious} malicious | ⚠️ {suspicious} suspicious | ⚪️ {undetected} undetected",
    ]

    detections = []
    for engine, res in (results or {}).items():
        if isinstance(res, dict):
            cat = res.get("category")
            res_str = res.get("result") or cat
            if cat in ("malicious", "suspicious"):
                detections.append(f"• **{engine}**: `{res_str}`")

    if detections:
        lines.append("\n**Detections:**")
        for d in detections[:8]:
            lines.append(d)
        if len(detections) > 8:
            lines.append(f"• _... and {len(detections) - 8} more_")

    if scan_date:
        lines.append(f"\n🕒 **Analysis Date:** {_format_vt_timestamp(scan_date)}")

    gui_link = f"https://www.virustotal.com/gui/url/{url_id}"
    lines.append(f"\n🌐 [View full report on VirusTotal]({gui_link})")

    reaction = "🚨" if malicious > 0 else ("⚠️" if suspicious > 0 else ("☑️" if total > 0 else "⚪️"))
    return "\n".join(lines), reaction


def _format_vt_file_report(file_name: str, file_size: int, sha256: str, attrs: dict) -> tuple[str, str]:
    """Format VirusTotal analysis result for a file. Returns (report_text, reaction_emoji)."""
    stats = attrs.get("last_analysis_stats") or attrs.get("stats") or {}
    results = attrs.get("last_analysis_results") or attrs.get("results") or {}
    scan_date = attrs.get("last_analysis_date") or attrs.get("date")
    type_desc = attrs.get("type_description") or ""

    malicious = int(stats.get("malicious", 0))
    suspicious = int(stats.get("suspicious", 0))
    harmless = int(stats.get("harmless", 0))
    undetected = int(stats.get("undetected", 0))
    total = malicious + suspicious + harmless + undetected

    if malicious > 0:
        verdict_emoji = "🚨"
        verdict_label = "Malicious"
    elif suspicious > 0:
        verdict_emoji = "⚠️"
        verdict_label = "Suspicious"
    elif total == 0:
        verdict_emoji = "⚪️"
        verdict_label = "No Data"
    else:
        verdict_emoji = "🟢"
        verdict_label = "Clean"

    lines = [
        f"{verdict_emoji} **VirusTotal File Report — {verdict_label}**\n",
        f"📁 **File:** `{file_name}` ({_format_file_size(file_size)})",
    ]
    if type_desc:
        lines.append(f"ℹ️ **Type:** {type_desc}")
    lines.extend([
        f"🔑 **SHA-256:** `{sha256}`",
        f"🛡️ **Detections:** {malicious + suspicious}/{total} security vendors",
        f"📊 **Stats:** 🟢 {harmless} harmless | 🔴 {malicious} malicious | ⚠️ {suspicious} suspicious | ⚪️ {undetected} undetected",
    ])

    detections = []
    for engine, res in (results or {}).items():
        if isinstance(res, dict):
            cat = res.get("category")
            res_str = res.get("result") or cat
            if cat in ("malicious", "suspicious"):
                detections.append(f"• **{engine}**: `{res_str}`")

    if detections:
        lines.append("\n**Detections:**")
        for d in detections[:8]:
            lines.append(d)
        if len(detections) > 8:
            lines.append(f"• _... and {len(detections) - 8} more_")

    if scan_date:
        lines.append(f"\n🕒 **Analysis Date:** {_format_vt_timestamp(scan_date)}")

    gui_link = f"https://www.virustotal.com/gui/file/{sha256}"
    lines.append(f"\n🌐 [View full report on VirusTotal]({gui_link})")

    reaction = "🚨" if malicious > 0 else ("⚠️" if suspicious > 0 else ("☑️" if total > 0 else "⚪️"))
    return "\n".join(lines), reaction


def _scan_url_virustotal(bot, accid, chat_id, msg_id, url: str, vt_key: str, max_attempts: int = 12) -> tuple[str | None, str | None]:
    """Scan or lookup a URL on VirusTotal. Returns (report_text, reaction_emoji) or (None, None) if handled in-place."""
    url_id = base64.urlsafe_b64encode(url.encode("utf-8")).decode("ascii").rstrip("=")

    resp_data, status, err = _vt_api_request("GET", f"/urls/{url_id}", vt_key)
    if status == 200 and resp_data:
        data = resp_data.get("data", {})
        attrs = data.get("attributes", {})
        return _format_vt_url_report(url, url_id, attrs)

    if status == 404:
        # URL not found in VT database; submit for scan
        post_data, p_status, p_err = _vt_api_request("POST", "/urls", vt_key, data={"url": url})
        if p_status != 200 or not post_data:
            return f"❌ VirusTotal scan submission failed: {p_err or 'Unknown error'}", "❌"

        analysis_id = post_data.get("data", {}).get("id")
        if not analysis_id:
            return "❌ VirusTotal returned invalid analysis response.", "❌"

        gui_link = f"https://www.virustotal.com/gui/url/{url_id}"
        interim_text = (
            f"ℹ️ **VirusTotal URL Scan Submitted**\n\n"
            f"🔗 **URL:** {url}\n\n"
            f"Analysis is in progress. Please wait for the results...\n"
            f"🌐 [View report on VirusTotal]({gui_link})"
        )

        status_msg_id = None
        if bot and chat_id:
            status_msg_id = dc_helpers._send(bot, accid, chat_id, interim_text, reply_to_id=msg_id)
            if msg_id:
                dc_helpers._react(bot, accid, msg_id, "⏳")

        # Poll analysis
        for _ in range(max_attempts):
            a_data, a_status, a_err = _vt_api_request("GET", f"/analyses/{analysis_id}", vt_key)
            if a_status == 200 and a_data:
                attrs = a_data.get("data", {}).get("attributes", {})
                if attrs.get("status") == "completed":
                    report_text, reaction = _format_vt_url_report(url, url_id, attrs)
                    if bot and chat_id and status_msg_id:
                        try:
                            bot.rpc.send_edit_request(accid, status_msg_id, report_text)
                        except Exception as e:
                            config.logger.warning(f"Failed to edit status message {status_msg_id}: {e}")
                            dc_helpers._send(bot, accid, chat_id, report_text, reply_to_id=msg_id)
                        if msg_id:
                            dc_helpers._react(bot, accid, msg_id, reaction)
                        return None, None
                    return report_text, reaction

        timeout_text = (
            f"ℹ️ **VirusTotal URL Scan In Progress**\n\n"
            f"🔗 **URL:** {url}\n\n"
            f"Analysis is taking longer than usual on VirusTotal. You can view the live report here:\n"
            f"🌐 [View report on VirusTotal]({gui_link})"
        )
        if bot and chat_id and status_msg_id:
            try:
                bot.rpc.send_edit_request(accid, status_msg_id, timeout_text)
            except Exception as e:
                config.logger.warning(f"Failed to edit status message {status_msg_id}: {e}")
                dc_helpers._send(bot, accid, chat_id, timeout_text, reply_to_id=msg_id)
            if msg_id:
                dc_helpers._react(bot, accid, msg_id, "⚪️")
            return None, None
        return timeout_text, "⚪️"

    return f"❌ VirusTotal API error: {err or f'Status {status}'}", "❌"


def _scan_file_virustotal(bot, accid, chat_id, msg_id, file_info: dict, vt_key: str, max_attempts: int = 12) -> tuple[str | None, str | None]:
    """Scan or lookup a file on VirusTotal. Returns (report_text, reaction_emoji) or (None, None) if handled in-place."""
    file_path = file_info["path"]
    file_name = file_info.get("filename") or os.path.basename(file_path)

    if not os.path.exists(file_path):
        return f"❌ Attached file not found on disk: {file_name}", "❌"

    try:
        file_size = os.path.getsize(file_path)
    except Exception as e:
        return f"❌ Could not determine file size: {e}", "❌"

    # Compute SHA-256
    try:
        h = hashlib.sha256()
        with open(file_path, "rb") as f:
            while chunk := f.read(65536):
                h.update(chunk)
        sha256 = h.hexdigest()
    except Exception as e:
        return f"❌ Failed to compute file hash: {e}", "❌"

    # 1. Check if hash exists in VT
    resp_data, status, err = _vt_api_request("GET", f"/files/{sha256}", vt_key)
    if status == 200 and resp_data:
        data = resp_data.get("data", {})
        attrs = data.get("attributes", {})
        return _format_vt_file_report(file_name, file_size, sha256, attrs)

    if status == 404:
        # File not in VT database
        MAX_UPLOAD_SIZE = 32 * 1024 * 1024  # 32 MB
        if file_size > MAX_UPLOAD_SIZE:
            gui_link = f"https://www.virustotal.com/gui/file/{sha256}"
            return (
                f"ℹ️ **File Not in VirusTotal Database**\n\n"
                f"📁 **File:** `{file_name}` ({_format_file_size(file_size)})\n"
                f"🔑 **SHA-256:** `{sha256}`\n\n"
                f"⚠️ File size exceeds the VirusTotal free tier upload limit (32 MB).\n"
                f"🌐 [Search on VirusTotal]({gui_link})"
            ), "⚠️"

        # Upload file
        up_data, up_status, up_err = _vt_upload_file(file_path, file_name, vt_key)
        if up_status != 200 or not up_data:
            return f"❌ Failed to upload file to VirusTotal: {up_err or 'Unknown error'}", "❌"

        analysis_id = up_data.get("data", {}).get("id")
        if not analysis_id:
            return "❌ VirusTotal returned invalid upload response.", "❌"

        gui_link = f"https://www.virustotal.com/gui/file/{sha256}"
        interim_text = (
            f"ℹ️ **VirusTotal File Scan Submitted**\n\n"
            f"📁 **File:** `{file_name}` ({_format_file_size(file_size)})\n"
            f"🔑 **SHA-256:** `{sha256}`\n\n"
            f"Analysis is in progress. Please wait for the results...\n"
            f"🌐 [View report on VirusTotal]({gui_link})"
        )

        status_msg_id = None
        if bot and chat_id:
            status_msg_id = dc_helpers._send(bot, accid, chat_id, interim_text, reply_to_id=msg_id)
            if msg_id:
                dc_helpers._react(bot, accid, msg_id, "⏳")

        # Poll analysis
        for _ in range(max_attempts):
            a_data, a_status, a_err = _vt_api_request("GET", f"/analyses/{analysis_id}", vt_key)
            if a_status == 200 and a_data:
                attrs = a_data.get("data", {}).get("attributes", {})
                if attrs.get("status") == "completed":
                    report_text, reaction = _format_vt_file_report(file_name, file_size, sha256, attrs)
                    if bot and chat_id and status_msg_id:
                        try:
                            bot.rpc.send_edit_request(accid, status_msg_id, report_text)
                        except Exception as e:
                            config.logger.warning(f"Failed to edit status message {status_msg_id}: {e}")
                            dc_helpers._send(bot, accid, chat_id, report_text, reply_to_id=msg_id)
                        if msg_id:
                            dc_helpers._react(bot, accid, msg_id, reaction)
                        return None, None
                    return report_text, reaction

        timeout_text = (
            f"ℹ️ **VirusTotal File Scan In Progress**\n\n"
            f"📁 **File:** `{file_name}` ({_format_file_size(file_size)})\n"
            f"🔑 **SHA-256:** `{sha256}`\n\n"
            f"Analysis is taking longer than usual on VirusTotal. You can view the live report here:\n"
            f"🌐 [View report on VirusTotal]({gui_link})"
        )
        if bot and chat_id and status_msg_id:
            try:
                bot.rpc.send_edit_request(accid, status_msg_id, timeout_text)
            except Exception as e:
                config.logger.warning(f"Failed to edit status message {status_msg_id}: {e}")
                dc_helpers._send(bot, accid, chat_id, timeout_text, reply_to_id=msg_id)
            if msg_id:
                dc_helpers._react(bot, accid, msg_id, "⚪️")
            return None, None
        return timeout_text, "⚪️"

    return f"❌ VirusTotal API error: {err or f'Status {status}'}", "❌"


def bg_virus_worker(bot, accid, chat_id, msg_id, target_type, target_data, vt_key, msg=None, payload=None):
    """Background worker for VirusTotal checks with global lock and FIFO queueing."""
    if not state._vt_global_lock.acquire(blocking=False):
        dc_helpers._send(bot, accid, chat_id, "⏳ Another VirusTotal check is in progress, your request is queued...", reply_to_id=msg_id)
        state._vt_global_lock.acquire()

    try:
        # Resolve target if not already resolved synchronously
        if not target_type and msg:
            # Check quote / reply
            if hasattr(msg, "quote") and msg.quote and isinstance(msg.quote, dict):
                quote_msg_id = msg.quote.get("message_id") or msg.quote.get("messageId")
                if quote_msg_id:
                    try:
                        quoted_msg = bot.rpc.get_message(accid, quote_msg_id)
                        q_file = dc_helpers._get_msg_file_info(bot, accid, quoted_msg)
                        if q_file:
                            target_type = "file"
                            target_data = q_file
                        else:
                            raw_text = getattr(quoted_msg, "text", "") if not isinstance(quoted_msg, dict) else quoted_msg.get("text", "")
                            q_text = raw_text if isinstance(raw_text, str) else ""
                            if not q_text and isinstance(msg.quote, dict):
                                raw_qt = msg.quote.get("text", "")
                                q_text = raw_qt if isinstance(raw_qt, str) else ""
                            url = dc_helpers._extract_first_url(q_text)
                            if url:
                                target_type = "url"
                                target_data = url
                    except Exception as e:
                        config.logger.warning(f"Failed to fetch quoted message for /virus: {e}")

            # If still not found, check if message itself has an attached file
            if not target_type:
                m_file = dc_helpers._get_msg_file_info(bot, accid, msg)
                if m_file:
                    target_type = "file"
                    target_data = m_file

        if not target_type:
            dc_helpers._react(bot, accid, msg_id, "❓")
            dc_helpers._send(
                bot, accid, chat_id,
                "Usage:\n"
                "• `/virus <url>` — Scan a URL with VirusTotal\n"
                "• Reply to a message containing a link or attached file with `/virus`",
                reply_to_id=msg_id,
            )
            return

        if target_type == "url":
            report_text, reaction = _scan_url_virustotal(bot, accid, chat_id, msg_id, target_data, vt_key)
        else:
            report_text, reaction = _scan_file_virustotal(bot, accid, chat_id, msg_id, target_data, vt_key)

        if report_text is not None:
            if reaction:
                dc_helpers._react(bot, accid, msg_id, reaction)
            dc_helpers._send(bot, accid, chat_id, report_text, reply_to_id=msg_id)
    except Exception as e:
        config.logger.error(f"Error in bg_virus_worker: {e}", exc_info=True)
        dc_helpers._react(bot, accid, msg_id, "❌")
        dc_helpers._send(bot, accid, chat_id, f"❌ An error occurred during the VirusTotal check: {e}", reply_to_id=msg_id)
    finally:
        state._vt_global_lock.release()


@config.dc_cli.on(events.NewMessage(command="/virus"))
def virus_command(bot, accid, event):
    msg = event.msg

    vt_key = os.environ.get("VIRUSTOTAL_API_KEY", "").strip().strip("'\"")
    if not vt_key:
        dc_helpers._send(
            bot, accid, msg.chat_id,
            "❌ VirusTotal API key is not configured. Please set VIRUSTOTAL_API_KEY in the bot's .env file.",
            reply_to_id=msg.id,
        )
        return

    target_type = None
    target_data = None

    payload = (event.payload or "").strip()
    if payload:
        url = dc_helpers._extract_first_url(payload)
        if url:
            target_type = "url"
            target_data = url

    has_quote = bool(
        hasattr(msg, "quote")
        and msg.quote
        and isinstance(msg.quote, dict)
        and (msg.quote.get("message_id") or msg.quote.get("messageId") or msg.quote.get("text"))
    )
    raw_file = getattr(msg, "file", None) if not isinstance(msg, dict) else msg.get("file")
    raw_filename = getattr(msg, "filename", None) if not isinstance(msg, dict) else msg.get("filename")
    raw_bytes = getattr(msg, "file_bytes", None) if not isinstance(msg, dict) else msg.get("file_bytes")
    raw_vt = getattr(msg, "view_type", None) if not isinstance(msg, dict) else msg.get("view_type")
    raw_ds = getattr(msg, "download_state", None) if not isinstance(msg, dict) else msg.get("download_state")

    has_attachment_fast = bool(
        (isinstance(raw_file, str) and raw_file)
        or (isinstance(raw_filename, str) and raw_filename)
        or (isinstance(raw_bytes, int) and not isinstance(raw_bytes, bool) and raw_bytes > 0)
        or (isinstance(raw_vt, (str, int)) and any(t in str(raw_vt).lower() for t in ("file", "image", "audio", "video", "voice", "gif", "sticker")))
        or (isinstance(raw_ds, (str, int)) and any(s in str(raw_ds).lower() for s in ("available", "inprogress", "10", "100", "1000")))
    )

    if not target_type and not has_quote and not has_attachment_fast:
        dc_helpers._send(
            bot, accid, msg.chat_id,
            "Usage:\n"
            "• `/virus <url>` — Scan a URL with VirusTotal\n"
            "• Reply to a message containing a link or attached file with `/virus`",
            reply_to_id=msg.id,
        )
        return

    dc_helpers._react(bot, accid, msg.id, "⏳")

    threading.Thread(
        target=bg_virus_worker,
        args=(bot, accid, msg.chat_id, msg.id, target_type, target_data, vt_key, msg, payload),
        daemon=True,
    ).start()


