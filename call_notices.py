"""Paced webxdc service lines for call incidents in report chats."""
import hashlib
import json
import os
from pathlib import Path
import threading
import time
import zipfile

from deltachat2 import MsgData

import cmping
import database
from outbox import PRIO_ALERT, outbox

MAX_APP_AGE = 7 * 86400
MAX_APP_UPDATES = 900
_APP_SOURCE = Path(__file__).resolve().parent / "webxdc_calls"
_submit_lock = threading.Lock()


def format_notice(event: dict) -> str:
    if event.get("ended_at") is not None:
        duration = cmping._format_duration(max(0, event["ended_at"] - event["started_at"]))
        icon, status = "📞✅", f"calls restored after {duration}"
    elif event["kind"] == "down":
        icon, status = "📞🚨", "calls failing"
    else:
        icon, status = "📞⚠️", "calls degraded"

    server = event["server"]
    limit = 100 - len(f"{icon} : {status}")
    if len(server) > limit:
        server = server[:limit - 1] + "…"
    return f"{icon} {server}: {status}"


def _build_app() -> str:
    """Cache the attachment by source contents and replace it atomically."""
    data_dir = Path(os.getenv("DC_DB_DIR") or Path(database.DB_PATH).resolve().parent)
    files = {name: (_APP_SOURCE / name).read_bytes() for name in ("index.html", "manifest.toml", "icon.png")}
    digest = hashlib.sha256()
    for contents in files.values():
        digest.update(hashlib.sha256(contents).digest())

    data_dir.mkdir(parents=True, exist_ok=True)
    target = data_dir / "call-notices.xdc"
    cache = target.with_suffix(".xdc.sha256")
    signature = digest.hexdigest()
    if not target.exists() or not cache.exists() or cache.read_text() != signature:
        temporary = target.with_suffix(".xdc.tmp")
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, contents in files.items():
                archive.writestr(name, contents)
        temporary.replace(target)
        cache.write_text(signature)
    return str(target)


def _ensure_app(bot, accid, chat_id, key):
    saved = database.get_config(key)
    now = time.time()
    if saved:
        msg_id, posted_at, count = json.loads(saved)
        if (now - posted_at <= MAX_APP_AGE and count < MAX_APP_UPDATES
                and msg_id in bot.rpc.get_existing_msg_ids(accid, [msg_id])):
            return msg_id, posted_at, count

    msg_id = bot.rpc.send_msg(accid, chat_id, MsgData(file=_build_app()))
    app = (msg_id, time.time(), 0)
    database.set_config(key, json.dumps(app))
    return app


def queue_notice(bot, accid, chat_id: int, event: dict) -> None:
    line = format_notice(event)
    # An href keeps Core from overwriting the preceding info message from this app.
    update = json.dumps({"info": line, "payload": line, "href": "#notices"}, ensure_ascii=False)
    key = f"cmcall_notice_app:{chat_id}"

    def send_update():
        msg_id, posted_at, count = app.result()
        bot.rpc.send_webxdc_status_update(accid, msg_id, update, "Call Notices")
        database.set_config(key, json.dumps((msg_id, posted_at, count + 1)))

    # Posting the app and sending the update each take a pacing slot. Keep each
    # pair in queue order so later notices see the previous update's count.
    with _submit_lock:
        app = outbox.submit(lambda: _ensure_app(bot, accid, chat_id, key), priority=PRIO_ALERT,
                            label=f"call notice app in chat {chat_id}")
        outbox.submit(send_update, priority=PRIO_ALERT, label=f"call notice in chat {chat_id}")
