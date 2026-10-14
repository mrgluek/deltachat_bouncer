"""Mail relay (transport) administration: the /transports, /addtransport and
/rmtransport commands, plus the deprecated /setprimary and /resilient.

Relay selection and failover are handled by the core since 2.61.
"""
from deltachat2 import events

import config
import database
import transports_view
import dc_helpers

@config.dc_cli.on(events.NewMessage(command="/transports"))
def transports_command(bot, accid, event):
    """Show configured transports (mail relays) and their status."""
    msg = event.msg
    if not dc_helpers._is_dc_admin(bot, accid, msg.from_id):
        dc_helpers._send(bot, accid, msg.chat_id, "❌ Only the bot administrator can use /transports.")
        return

    try:
        transports = bot.rpc.list_transports(accid)
    except Exception as e:
        dc_helpers._send(bot, accid, msg.chat_id, f"❌ Failed to list transports: {e}")
        return

    if not transports:
        dc_helpers._send(bot, accid, msg.chat_id, "No transports configured.")
        return

    # Which relay the core really sends through, its send queue, and per-relay
    # incoming status and storage (see transports_view.py).
    reply = transports_view.build_transports_report(bot.rpc, accid, transports)
    dc_helpers._send(bot, accid, msg.chat_id, reply)

@config.dc_cli.on(events.NewMessage(command="/addtransport"))
def addtransport_command(bot, accid, event):
    """Add a backup mail relay (transport). Admin only."""
    msg = event.msg
    if not dc_helpers._is_dc_admin(bot, accid, msg.from_id):
        dc_helpers._send(bot, accid, msg.chat_id, "❌ Only the bot administrator can use /addtransport.")
        return

    payload = event.payload.strip() if event.payload else ""
    # Require private chat for password protection
    try:
        chat = bot.rpc.get_basic_chat_info(accid, msg.chat_id)
        chat_type = chat.get('chat_type', 'Single') if isinstance(chat, dict) else getattr(chat, 'chat_type', 'Single')
    except Exception:
        chat_type = 'Single'

    if str(chat_type) != 'Single':
        dc_helpers._send(bot, accid, msg.chat_id, "🔒 For security reasons, /addtransport can only be used in a private 1-on-1 chat with the bot.")
        return

    if not payload:
        dc_helpers._send(bot, accid, msg.chat_id, 
            "Usage:\n"
            "/addtransport DCACCOUNT:server.example\n"
            "/addtransport user@example.com password123"
        )
        return

    try:
        if payload.startswith("DCACCOUNT:"):
            bot.rpc.add_transport_from_qr(accid, payload)
            dc_helpers._send(bot, accid, msg.chat_id, "✅ Backup transport added via chatmail URI.")
        else:
            parts = payload.split(None, 1)
            if len(parts) < 2:
                dc_helpers._send(bot, accid, msg.chat_id, 
                    "❌ For email accounts, provide both address and password:\n"
                    "/addtransport user@example.com password123"
                )
                return
            addr, password = parts[0], parts[1]
            bot.rpc.add_or_update_transport(accid, {"addr": addr, "password": password})
            dc_helpers._send(bot, accid, msg.chat_id, f"✅ Backup transport `{addr}` added.")
    except Exception as e:
        config.logger.error(f"Failed to add transport: {e}")
        dc_helpers._send(bot, accid, msg.chat_id, "❌ Failed to add transport. Please check your credentials and try again.")

RELAY_SELECTION_NOTE = (
    "Since core 2.61 the core picks the sending relay itself: it tries the newest relay first "
    "and falls back to the next one if a relay is unreachable. "
    "Use /addtransport and /rmtransport to control relays."
)


@config.dc_cli.on(events.NewMessage(command="/setprimary"))
def setprimary_command(bot, accid, event):
    """Deprecated: the core no longer uses configured_addr to pick the SMTP relay."""
    msg = event.msg
    if not dc_helpers._is_dc_admin(bot, accid, msg.from_id):
        dc_helpers._send(bot, accid, msg.chat_id, "❌ Only the bot administrator can use /setprimary.")
        return
    dc_helpers._send(bot, accid, msg.chat_id, f"⚠️ /setprimary is deprecated and disabled. {RELAY_SELECTION_NOTE}")

@config.dc_cli.on(events.NewMessage(command="/resilient"))
def resilient_command(bot, accid, event):
    """Deprecated: relay failover is handled by the core."""
    msg = event.msg
    if not dc_helpers._is_dc_admin(bot, accid, msg.from_id):
        dc_helpers._send(bot, accid, msg.chat_id, "❌ Only the bot administrator can use /resilient.")
        return
    dc_helpers._send(bot, accid, msg.chat_id, f"⚠️ /resilient is deprecated and disabled. {RELAY_SELECTION_NOTE}")

@config.dc_cli.on(events.NewMessage(command="/rmtransport"))
def rmtransport_command(bot, accid, event):
    """Remove a mail relay (transport). Admin only."""
    msg = event.msg
    if not dc_helpers._is_dc_admin(bot, accid, msg.from_id):
        dc_helpers._send(bot, accid, msg.chat_id, "❌ Only the bot administrator can use /rmtransport.")
        return

    addr = event.payload.strip() if event.payload else ""
    if not addr:
        dc_helpers._send(bot, accid, msg.chat_id, "Usage: /rmtransport user@example.com")
        return

    try:
        transports = bot.rpc.list_transports(accid)
        transport_addrs = []
        for t in transports:
            a = t.get('addr', '') if isinstance(t, dict) else getattr(t, 'addr', '')
            transport_addrs.append(a)
        if len(transport_addrs) <= 1:
            dc_helpers._send(bot, accid, msg.chat_id, "❌ Cannot remove the last transport.")
            return
        if addr not in transport_addrs:
            dc_helpers._send(bot, accid, msg.chat_id, f"❌ Transport `{addr}` not found.")
            return
    except Exception as e:
        dc_helpers._send(bot, accid, msg.chat_id, f"❌ Failed to check transports: {e}")
        return

    try:
        bot.rpc.delete_transport(accid, addr)
        dc_helpers._send(bot, accid, msg.chat_id, f"✅ Transport `{addr}` removed.")
    except Exception as e:
        dc_helpers._send(bot, accid, msg.chat_id, f"❌ Failed to remove transport: {e}")
