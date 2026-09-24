#!/usr/bin/env python3
"""Bouncer Bot entry point: lifecycle hooks (on_init/on_start), the
custom-command-prefix / multi-bot-in-group parser, and __main__.

All command handlers, background workers, and the web server live in the
domain modules below - importing them registers their @dc_cli.on(...)
hooks on config.dc_cli. deltachat2's HookCollection stores hooks in a
plain `set`, so this import order does not control event-dispatch order;
it only needs to satisfy each module's own dependencies (see each
module's docstring/imports).
"""
import io
import json
import logging
import os
import threading

import qrcode

import activitypub
import channels
import cmping
import cmping_commands
import commands
import config
import database
import dc_helpers
import handlers
import moderation
import security
import state
import stickers
import transports
import virustotal
import web.routes


# ── Backward-compatible facade ──
# `import bot; bot.<name>` keeps working for anything that used to live directly
# in this file. Function/constant re-exports are safe (same object either way);
# a handful of rebound scalars (state.dc_bot_instance, state.dc_accid,
# state.index_page_html_cache, state._cmping_monitor_index/_running,
# state._vt_last_request_time) are snapshotted at import time here and will NOT
# reflect later rebinding - read them from `state` directly if you need the live
# value.
from channels import (
    _backfill_existing_catalog_channels,
    _ingest_channel_post,
    bg_channel_join_worker,
    chatadd_command,
    chatremove_command,
    chats_command,
    dchanneladd_command,
    dchannelremove_command,
    dchannels_command,
    private_command,
)
from cmping import (
    _clear_all_errors_for_server,
    _cmping_monitor_cycle,
    _cmping_monitor_loop,
    _format_cmping_incident_message,
    _format_duration,
    _get_cmping_incident_update_interval,
    _parse_single_cmping,
    _run_cmping_subprocess,
    _run_monitor_single,
    _sync_cmping_incident_alerts,
)
from cmping_commands import (
    _bg_cmping_worker_inner,
    _cmpingfail_impl,
    bg_cmping_worker,
    cmfaillist_command,
    cmping_command,
    cmpingadd_command,
    cmpingdel_command,
    cmpingevents_command,
    cmpingfail_command,
    cmpinghistory_command,
    cmpinglist_command,
    cmpingstatus_command,
    cmreport_command,
)
from commands import (
    away_command,
    back_command,
    bounce_command,
    donate_command,
    help_command,
    initadmin_command,
    invite_command,
    me_command,
    relays_command,
    search_command,
    slap_command,
    url_command,
    welcome_command,
)
from config import (
    BOUNCE_COOLDOWN_SECONDS,
    CMPING_COOLDOWN_SECONDS,
    CMPING_MONITOR_INTERVAL,
    DC_CONTACT_ID_SELF,
    DC_FALLBACK_PATTERN,
    DOMAIN_REGEX,
    INACTIVITY_DAYS_THRESHOLD,
    INACTIVITY_SECONDS_THRESHOLD,
    REGULAR_MAIL_DOMAINS,
    SEARCH_COOLDOWN_SECONDS,
    SLAP_COOLDOWN_SECONDS,
    STICKER_COOLDOWN_SECONDS,
    STICKER_NOBG_COOLDOWN_SECONDS,
    VERSION,
    VIRUSTOTAL_RATE_LIMIT_SECONDS,
    _AGE_CIRCLES,
    _AGE_SQUARES,
    _load_env_file,
    dc_cli,
    log_version_info,
    logger,
)
from dc_helpers import (
    _extract_first_url,
    _get_bot_domains,
    _get_chat_autokick_warn_threshold,
    _get_contact_age_indicator,
    _get_contact_fingerprint,
    _get_domain_lock,
    _get_msg_file_info,
    _get_user_badges,
    _is_contact_autokick_ignored,
    _is_dc_admin,
    _prune_anti_spam_dicts,
    _prune_domain_locks,
    _queue_delayed_command,
    _react,
    _send,
    clear_pending_delayed_commands,
    find_contact_in_chat,
    get_user_chat_count,
)
from formatting import (
    _autolink,
    _format_post_time,
    format_markdown_html,
)
from handlers import (
    handle_all_messages,
    handle_dc_info_message,
)
from moderation import (
    _background_monitor_loop,
    _check_chat_inactivity,
    _get_chat_autokick_candidates,
    _get_chat_autokick_overview,
    _get_top_posters,
    _get_top_posters_report,
    _perform_autokick_for_chat,
    _perform_autokick_warnings_for_chat,
    _refresh_catalog_member_counts,
    autokick_command,
    kick_command,
    top_command,
)
from security import (
    SAFE_HOST_REGEX,
    _EXTENSION_MIME_MAP,
    _TRUSTED_PROXIES,
    _get_base_url,
    _get_client_ip,
    _guess_media_content_type,
    _rate_limit_lock,
    _rate_limits,
    check_rate_limit,
    rate_limited,
)
from state import (
    CHANNEL_MEDIA_DIR,
    _channel_preview_cache,
    _channel_rss_cache,
    _chat_anti_spam,
    _chat_bounce_anti_spam,
    _chat_cmping_anti_spam,
    _chat_invite_anti_spam,
    _chat_relays_anti_spam,
    _chat_search_anti_spam,
    _chat_slap_anti_spam,
    _chat_sticker_anti_spam,
    _chat_stickernobg_anti_spam,
    _chat_top_anti_spam,
    _cmping_global_lock,
    _cmping_incident_last_edit_state,
    _cmping_last_results,
    _cmping_monitor_idx_db,
    _cmping_monitor_index,
    _cmping_monitor_running,
    _cmping_server_errors,
    _cmping_server_status,
    _domain_locks,
    _domain_locks_lock,
    _message_failover_attempts,
    _pending_delayed_commands,
    _pending_delayed_lock,
    _qr_cache,
    _rembg_global_lock,
    _set_cmping_server_status,
    _vt_global_lock,
    _vt_last_request_time,
    bot_invite_link_cache,
    dc_accid,
    dc_bot_instance,
    get_bot_invite_link,
    index_page_html_cache,
    resilient_lock,
)
from stickers import (
    _bg_sticker_worker,
    _optimize_image_to_webp,
    _send_sticker,
    convert_to_sticker_webp,
    handle_sticker_command,
    sticker_command,
    stickernobg_command,
)
from transports import (
    _setup_resilient_mode,
    addtransport_command,
    on_msg_failed,
    resilient_command,
    rmtransport_command,
    setprimary_command,
    transports_command,
)
from virustotal import (
    _format_file_size,
    _format_vt_file_report,
    _format_vt_timestamp,
    _format_vt_url_report,
    _scan_file_virustotal,
    _scan_url_virustotal,
    _vt_api_request,
    _vt_upload_file,
    _vt_wait_rate_limit,
    bg_virus_worker,
    virus_command,
)
from web.ap_routes import (
    _ap_backfill_semaphore,
    _channel_last_backfill,
    _extract_channel_token_from_activity,
    _get_ap_backfill_semaphore,
    handle_ap_actor,
    handle_ap_followers,
    handle_ap_following,
    handle_ap_inbox,
    handle_ap_outbox,
    handle_ap_post,
    handle_api_v1_instance,
    handle_nodeinfo,
    handle_nodeinfo_discovery,
    handle_webfinger,
)
from web.routes import (
    _ALLOWED_BG_FILENAMES,
    _DEFAULT_CHANNEL_AVATAR_SVG,
    _generate_qr_bytes,
    _get_allowed_icon_filenames,
    _run_web_server,
    get_bot_avatar_file_path,
    get_default_channel_avatar_response,
    handle_background,
    handle_channel_avatar,
    handle_channel_default_avatar,
    handle_channel_preview,
    handle_channel_qr_png,
    handle_channel_qr_svg,
    handle_channel_rss,
    handle_channel_rss_redirect,
    handle_health,
    handle_icon,
    handle_index,
    handle_media_file,
    handle_qr_png,
    handle_qr_svg,
    handle_robots_txt,
    invalidate_channel_cache,
    start_web_server_thread,
)
from web.templates.channel import (
    get_channel_preview_html,
)
from web.templates.errors import (
    get_404_html,
    get_tombstone_html,
)
from web.templates.feed import (
    _escape_cdata,
    get_channel_rss_xml,
)
from web.templates.landing import (
    get_landing_page_html,
)
from web.templates.theme import (
    _THEME_CONTROLLER_SCRIPT,
    _THEME_PRELOAD_SCRIPT,
    _THEME_SWITCHER_HTML,
)


@config.dc_cli.on_init
def on_init(bot, args):
    bot.logger.info(f"Initializing Bouncer Bot v{config.VERSION}...")
    state.dc_bot_instance = bot
    transports._setup_resilient_mode(bot)
    
    config.log_version_info(bot)
    
    accounts = bot.rpc.get_all_account_ids()
    if accounts:
        state.dc_accid = accounts[0]
        bot_name = os.environ.get("DISPLAY_NAME")
        if not bot_name and os.path.exists("/data/options.json"):
            try:
                with open("/data/options.json", "r", encoding="utf-8") as f:
                    opts = json.load(f)
                    bot_name = opts.get("display_name", "").strip()
            except Exception:
                pass
        if not bot_name:
            bot_name = "Bouncer Bot"
        bot.rpc.set_config(state.dc_accid, "displayname", bot_name)

        status_text = os.environ.get("STATUS_TEXT")
        if not status_text and os.path.exists("/data/options.json"):
            try:
                with open("/data/options.json", "r", encoding="utf-8") as f:
                    opts = json.load(f)
                    status_text = opts.get("status_text", "").strip()
            except Exception:
                pass
        if not status_text:
            status_text = "I monitor group activity, manage chat/channel catalogs, welcome new members, and search contacts. Send /help for commands."
        bot.rpc.set_config(state.dc_accid, "selfstatus", status_text)
        
        # Set icon if configured or fallback to default icon
        try:
            base_dir = os.path.dirname(os.path.abspath(__file__))
            avatar_env = os.environ.get("AVATAR_PATH")
            avatar_paths = []
            if avatar_env:
                if os.path.isabs(avatar_env):
                    avatar_paths.append(avatar_env)
                else:
                    avatar_paths.append(os.path.join(base_dir, avatar_env))
                    avatar_paths.append(os.path.abspath(avatar_env))
            
            # Default paths fallback
            avatar_paths.extend([
                os.path.join(base_dir, "icon.png"),
                os.path.join(base_dir, "data", "icon.png")
            ])
            
            for path in avatar_paths:
                if os.path.exists(path):
                    bot.rpc.set_config(state.dc_accid, "selfavatar", path)
                    bot.logger.info(f"Successfully configured bot avatar from {path}")
                    break
        except Exception as e:
            bot.logger.warning(f"Could not set avatar: {e}")
            
        # Optimize storage (disable auto-download of attachments, auto-delete messages after 36 hours to buffer /top 24h stats)
        try:
            bot.rpc.set_config(state.dc_accid, "download_limit", "1")
            bot.rpc.set_config(state.dc_accid, "delete_device_after", "129600") # 36 hours (1.5 days) to buffer /top 24h stats
            bot.logger.info("Configured auto-download limit (1 byte) and message deletion (36 hours) in on_init.")
        except Exception as e:
            bot.logger.warning(f"Could not configure storage optimization in on_init: {e}")


def setup_custom_command_parser(bot, allowed_prefixes):
    original_parse_command = bot._parse_command

    def custom_parse_command(accid: int, event) -> None:
        text = event.msg.text
        if not text:
            original_parse_command(accid, event)
            return

        parts = text.split(maxsplit=1)
        cmd = parts[0]
        
        if "@" in cmd:
            cmd_name, suffix = cmd.split("@", 1)
            suffix_lower = suffix.lower()
            
            if suffix_lower:
                try:
                    self_address = bot.rpc.get_contact(accid, 1).address.lower()
                except Exception:
                    self_address = ""
                
                matched = False
                for p in allowed_prefixes:
                    if suffix_lower.startswith(p.lower()) or p.lower().startswith(suffix_lower):
                        matched = True
                        break
                if not matched and self_address and suffix_lower == self_address:
                    matched = True
                
                if matched:
                    new_text = cmd_name
                    if len(parts) > 1:
                        new_text += " " + parts[1]
                    
                    original_text = event.msg.text
                    event.msg["text"] = new_text
                    try:
                        original_parse_command(accid, event)
                    finally:
                        event.msg["text"] = original_text
                else:
                    event.command = ""
                    event.payload = ""
            else:
                original_parse_command(accid, event)
        else:
            original_parse_command(accid, event)
            
            # /help is not suppressed: plain /help in a group is answered privately (see help_command)
            if event.command == "/stats":
                try:
                    chat = bot.rpc.get_chat(accid, event.msg.chat_id)
                    is_group = getattr(chat, "chat_type", "Single") != "Single"
                except Exception:
                    is_group = False
                
                if is_group:
                    try:
                        contacts = bot.rpc.get_chat_contacts(accid, event.msg.chat_id)
                        bot_count = 0
                        for contact_id in contacts:
                            if contact_id == 1:
                                bot_count += 1
                                continue
                            c = bot.rpc.get_contact(accid, contact_id)
                            if getattr(c, "is_bot", False):
                                bot_count += 1
                                if bot_count > 1:
                                    break
                        if bot_count > 1:
                            event.command = ""
                            event.payload = ""
                    except Exception:
                        pass

    bot._parse_command = custom_parse_command


@config.dc_cli.on_start
def on_start(bot, args):
    setup_custom_command_parser(bot, ["boun", "stew"])
    state.dc_bot_instance = bot
    
    # Monkey-patch bot._process_messages and bot._process_message to allow processing of system/info messages
    from deltachat2 import SpecialContactId, Bot, EventType
    from deltachat2.transport import JsonRpcError

    from collections import OrderedDict
    _processed_msg_ids = OrderedDict()

    def _is_and_mark_processed(msgid: int) -> bool:
        if msgid in _processed_msg_ids:
            return True
        _processed_msg_ids[msgid] = True
        while len(_processed_msg_ids) > 2000:
            _processed_msg_ids.popitem(last=False)
        return False

    def custom_process_messages(accid: int, retry=True) -> None:
        try:
            for msgid in bot.rpc.get_next_msgs(accid):
                if _is_and_mark_processed(msgid):
                    continue
                msg = bot.rpc.get_message(accid, msgid)
                outgoing = msg.from_id == SpecialContactId.SELF
                if config.logger.isEnabledFor(logging.DEBUG):
                    config.logger.debug(f"custom_process_messages: msgid={msgid}, from_id={msg.from_id}, is_info={msg.is_info}, text={msg.text!r}")
                # Process the message if it's outgoing, from a contact > LAST_SPECIAL, or a system/info message
                if outgoing or msg.from_id > SpecialContactId.LAST_SPECIAL or msg.is_info:
                    if config.logger.isEnabledFor(logging.DEBUG):
                        config.logger.debug(f"custom_process_messages: calling _on_new_msg for msgid={msgid}")
                    bot._on_new_msg(accid, msg)
                bot.rpc.set_config(accid, "last_msg_id", str(msgid))
        except JsonRpcError as err:
            config.logger.exception(err)
            if retry:
                custom_process_messages(accid, False)

    def custom_process_message(accid: int, msgid: int) -> None:
        if _is_and_mark_processed(msgid):
            return
        try:
            msg = bot.rpc.get_message(accid, msgid)
            outgoing = msg.from_id == SpecialContactId.SELF
            if config.logger.isEnabledFor(logging.DEBUG):
                config.logger.debug(f"custom_process_message: msgid={msgid}, from_id={msg.from_id}, is_info={msg.is_info}, text={msg.text!r}")
            # Process the message if it's outgoing, from a contact > LAST_SPECIAL, or a system/info message
            if outgoing or msg.from_id > SpecialContactId.LAST_SPECIAL or msg.is_info:
                if config.logger.isEnabledFor(logging.DEBUG):
                    config.logger.debug(f"custom_process_message: calling _on_new_msg or dispatching for msgid={msgid}")
                if hasattr(bot, "_on_new_msg"):
                    bot._on_new_msg(accid, msg)
                else:
                    from deltachat2 import NewMsgEvent, Event, events
                    event = NewMsgEvent(command="", payload="", msg=msg)
                    if not msg.is_info and msg.text.startswith(bot.command_prefix):
                        bot._parse_command(accid, event)
                    bot._on_event(Event(accid, event), events.NewMessage)
        except JsonRpcError as err:
            config.logger.exception(err)

    def custom_run_until(self, func, account_id=0):
        if account_id:
            if self.rpc.is_configured(account_id):
                self.rpc.start_io(account_id)
        else:
            self.rpc.start_io_for_all_accounts()

        def _wrapper(event):
            if event.event.kind == EventType.INCOMING_MSG:
                if hasattr(self, "_process_message"):
                    self._process_message(event.account_id, event.event.msg_id)
                else:
                    self._process_messages(event.account_id)
            elif event.event.kind == EventType.MSGS_CHANGED:
                if hasattr(self, "_process_message"):
                    msg_id = getattr(event.event, "msg_id", None)
                    if msg_id and msg_id > 0:
                        self._process_message(event.account_id, msg_id)
                else:
                    self._process_messages(event.account_id)
            return func(event)

        return super(Bot, self).run_until(_wrapper, account_id)

    bot._process_messages = custom_process_messages
    bot._process_message = custom_process_message
    bot.run_until = lambda func, account_id=0: custom_run_until(bot, func, account_id)

    accounts = bot.rpc.get_all_account_ids()
    if not accounts:
        config.logger.error("No accounts found.")
        return
        
    accid = accounts[0]
    state.dc_accid = accid
    
    config.logger.info(f"Bouncer bot started with accid {accid}.")
    
    # Ensure storage optimization settings are active
    try:
        bot.rpc.set_config(accid, "download_limit", "1")
        bot.rpc.set_config(accid, "delete_device_after", "129600") # 36 hours (1.5 days) to buffer /top 24h stats
        config.logger.info("Successfully set auto-download limit to 1 byte and delete_device_after to 36 hours to optimize storage.")
    except Exception as e:
        config.logger.error(f"Failed to set storage optimization settings in on_start: {e}")
    
    # Show configured admin and transports
    admin_email = database.get_config("admin_dc_email")
    admin_fp = database.get_admin_fingerprint()
    if admin_email:
        fp_suffix = f" ({admin_fp[-8:].upper()})" if admin_fp else ""
        print(f"Bot Administrator: {admin_email}{fp_suffix}")
    
    try:
        transports = bot.rpc.list_transports(accid)
        print("\n" + "=" * 50)
        print("Configured Bot Transports (Relays):")
        for t in transports:
            a = t.get('addr', '') if isinstance(t, dict) else getattr(t, 'addr', '')
            print(f" - {a}")
        
        qrdata = bot.rpc.get_chat_securejoin_qr_code(accid, None)
        print("\nTo add this bot, scan the QR code or copy the link:\n")
        
        qr = qrcode.QRCode(version=1, box_size=1, border=2)
        qr.add_data(qrdata)
        qr.make(fit=True)
        f = io.StringIO()
        qr.print_ascii(out=f)
        print(f.getvalue())
        
        print(qrdata)
        print("\n" + "=" * 50 + "\n")
    except Exception as e:
        config.logger.error(f"Failed to generate QR code: {e}")
    
    t = threading.Thread(target=moderation._background_monitor_loop, args=(bot, accid), daemon=True)
    t.start()

    t2 = threading.Thread(target=cmping._cmping_monitor_loop, args=(bot, accid), daemon=True)
    t2.start()

    t_web = threading.Thread(target=web.routes.start_web_server_thread, daemon=True)
    t_web.start()

    t_backfill = threading.Thread(target=channels._backfill_existing_catalog_channels, args=(bot, accid), daemon=True)
    t_backfill.start()


if __name__ == "__main__":
    import sys
    
    # Handle 'init transport' CLI command
    if len(sys.argv) > 2 and sys.argv[1] == "init" and sys.argv[2] == "transport":
        if len(sys.argv) < 5:
            print("Usage: python bot.py init transport <email> <password>")
            sys.exit(1)
            
        addr, password = sys.argv[3], sys.argv[4]
        
        # We need to manually initialize RPC to add transport without starting the bot
        from deltachat2 import Rpc, IOTransport
        from appdirs import user_config_dir
        
        config_dir = user_config_dir("bouncer")
        accounts_dir = os.path.join(config_dir, "accounts")
        
        try:
            with IOTransport(accounts_dir=accounts_dir) as trans:
                rpc = Rpc(trans)
                accids = rpc.get_all_account_ids()
                if not accids:
                    print("Error: No accounts configured. Run 'python bot.py init addr password' first.")
                    sys.exit(1)
                    
                rpc.add_or_update_transport(accids[0], {"addr": addr, "password": password})
                print(f"Success: Backup transport {addr} added.")
        except Exception as e:
            print(f"Error adding transport: {e}")
            sys.exit(1)
        sys.exit(0)

    if len(sys.argv) == 1:
        sys.argv.append("serve")
    config.dc_cli.start()
