"""Sticker generation: WebP image optimization, the in-process/rembg-subprocess
sticker converter, sticker delivery, and the /sticker + /stickernobg commands."""
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time

from deltachat2 import events, MsgData

import config
import database
import dc_helpers
import state

def _optimize_image_to_webp(src_path: str, dest_path: str, max_dim: int = 1600, quality: int = 80) -> bool:
    """Optimize image to WebP with dimension scaling. Returns True on success, False on failure."""
    try:
        from PIL import Image, ImageOps
        with Image.open(src_path) as img:
            try:
                img = ImageOps.exif_transpose(img)
            except Exception:
                pass

            if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in getattr(img, "info", {})):
                if img.mode != "RGBA":
                    img = img.convert("RGBA")
            elif img.mode != "RGB":
                img = img.convert("RGB")

            w, h = img.size
            if max(w, h) > max_dim:
                scale = max_dim / max(w, h)
                new_size = (max(1, int(w * scale)), max(1, int(h * scale)))
                img = img.resize(new_size, Image.Resampling.LANCZOS)

            img.save(dest_path, format="WEBP", quality=quality, method=3)
            return True
    except Exception as e:
        config.logger.warning(f"Failed to convert image {src_path} to webp: {e}")
        return False


def convert_to_sticker_webp(src_path: str, dest_path: str, remove_bg: bool = False, max_dim: int = 512) -> tuple[bool, str]:
    """Convert an image to a WebP sticker with standard sizing (max 512px).
    Optionally removes background using rembg in an isolated subprocess if remove_bg=True.
    Returns (success: bool, error_message: str).
    """
    if remove_bg:
        enable_rembg = os.getenv("ENABLE_REMBG", "true").strip().lower()
        if enable_rembg in ("0", "false", "off", "no", "disabled"):
            return False, "Background removal is disabled on this server. Use /sticker to create a sticker with the background preserved."

        tool_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sticker_tool.py")
        if os.path.exists(tool_path):
            try:
                cmd = [
                    sys.executable,
                    tool_path,
                    src_path,
                    dest_path,
                    "--nobg",
                    f"--max-dim={max_dim}",
                ]
                res = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
                if res.returncode == 0:
                    return True, ""
                err = (res.stderr or res.stdout or "").strip()
                if not err:
                    err = f"Subprocess exited with code {res.returncode}"
                return False, err
            except subprocess.TimeoutExpired:
                return False, "Sticker generation timed out (90s)."
            except Exception as e:
                config.logger.error(f"Failed to run sticker_tool subprocess: {e}", exc_info=True)
                return False, f"Failed to generate sticker: {e}"

    # In-process handling (default for /sticker without bg removal)
    try:
        from PIL import Image, ImageOps
    except ImportError:
        return False, "Pillow library is not installed."

    try:
        with Image.open(src_path) as raw_img:
            try:
                img = ImageOps.exif_transpose(raw_img)
            except Exception:
                img = raw_img

            # Handle animated images (extract first frame)
            if getattr(img, "is_animated", False):
                try:
                    img.seek(0)
                except Exception:
                    pass

            w, h = img.size
            if w <= 0 or h <= 0:
                return False, "Invalid image dimensions."

            # Pre-scale so longest side is max_dim (standard sticker 512px)
            if max(w, h) != max_dim:
                scale = float(max_dim) / max(w, h)
                new_w = max(1, int(round(w * scale)))
                new_h = max(1, int(round(h * scale)))
                img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)

            if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in getattr(img, "info", {})):
                if img.mode != "RGBA":
                    img = img.convert("RGBA")
            elif img.mode != "RGB":
                img = img.convert("RGBA")

            # Final check to guarantee sticker dimensions do not exceed max_dim
            w, h = img.size
            if max(w, h) > max_dim:
                scale = float(max_dim) / max(w, h)
                new_w = max(1, int(round(w * scale)))
                new_h = max(1, int(round(h * scale)))
                img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)

            # Save as WebP
            try:
                img.save(dest_path, format="WEBP", quality=90, method=6)
            except Exception as e:
                config.logger.warning(f"Failed to save as WEBP ({e}), falling back to PNG...")
                png_path = os.path.splitext(dest_path)[0] + ".png"
                img.save(png_path, format="PNG")
                if os.path.exists(png_path):
                    shutil.move(png_path, dest_path)
            return True, ""
    except Exception as e:
        config.logger.warning(f"Failed to convert image {src_path} to sticker: {e}")
        return False, f"Invalid or unsupported image file: {e}"


def _send_sticker(bot, accid, chat_id, sticker_path, reply_to_id=None):
    """Send a sticker message to a chat, falling back to send_msg with viewtype=Sticker or send_sticker RPC."""
    sent_msg_id = None
    try:
        sent_msg_id = bot.rpc.send_msg(accid, chat_id, MsgData(file=sticker_path, viewtype="Sticker", quoted_message_id=reply_to_id))
    except Exception as e:
        config.logger.warning(f"Failed to send sticker via send_msg: {e}. Trying send_sticker RPC...")
        if hasattr(bot.rpc, "send_sticker"):
            try:
                sent_msg_id = bot.rpc.send_sticker(accid, chat_id, sticker_path)
            except Exception as e2:
                config.logger.error(f"Failed to send sticker via send_sticker RPC: {e2}")
                raise
        else:
            raise

    # Track transport stats
    try:
        addr = bot.rpc.get_config(accid, "configured_addr") or bot.rpc.get_config(accid, "addr") or "unknown"
        if addr and isinstance(addr, str) and addr != "unknown":
            database.increment_transport_sent(addr)
    except Exception:
        pass

    return sent_msg_id


def _bg_sticker_worker(bot, accid, chat_id, msg_id, file_info, remove_bg):
    """Background worker for sticker creation."""
    temp_dir = tempfile.mkdtemp(prefix="dc_sticker_")
    try:
        src_path = file_info["path"]
        dest_path = os.path.join(temp_dir, "sticker.webp")
        if remove_bg:
            with state._rembg_global_lock:
                success, err_msg = convert_to_sticker_webp(src_path, dest_path, remove_bg=True)
        else:
            success, err_msg = convert_to_sticker_webp(src_path, dest_path, remove_bg=False)
        if not success:
            dc_helpers._react(bot, accid, msg_id, "❌")
            dc_helpers._send(bot, accid, chat_id, f"❌ {err_msg}", reply_to_id=msg_id)
            return

        _send_sticker(bot, accid, chat_id, dest_path, reply_to_id=msg_id)
        dc_helpers._react(bot, accid, msg_id, "☑️")
    except Exception as e:
        config.logger.error(f"Error in _bg_sticker_worker: {e}", exc_info=True)
        dc_helpers._react(bot, accid, msg_id, "❌")
        dc_helpers._send(bot, accid, chat_id, f"❌ Failed to create sticker: {e}", reply_to_id=msg_id)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def handle_sticker_command(bot, accid, event, remove_bg: bool = False, skip_cooldown: bool = False):
    """Handle /sticker and /stickernobg commands (reply or caption)."""
    msg = event.msg
    payload = (event.payload or "").strip().lower()
    if payload in ("nobg", "--nobg", "-nobg", "removebg", "no-bg"):
        remove_bg = True

    cmd_key = "stickernobg" if remove_bg else "sticker"
    cooldown_sec = config.STICKER_NOBG_COOLDOWN_SECONDS if remove_bg else config.STICKER_COOLDOWN_SECONDS
    anti_spam_dict = state._chat_stickernobg_anti_spam if remove_bg else state._chat_sticker_anti_spam

    now = time.time()
    if not dc_helpers._is_dc_admin(bot, accid, msg.from_id) and not skip_cooldown:
        last_sticker = anti_spam_dict.get(msg.chat_id, 0)
        diff = now - last_sticker
        if diff < cooldown_sec:
            remaining_sec = max(1, int(cooldown_sec - diff))
            dc_helpers._queue_delayed_command(bot, accid, msg, cmd_key, remaining_sec, handle_sticker_command, bot, accid, event, remove_bg=remove_bg, skip_cooldown=True)
            return

    anti_spam_dict[msg.chat_id] = now

    # 1. Check if message itself has an attached file (e.g. image sent with caption /sticker)
    file_info = dc_helpers._get_msg_file_info(bot, accid, msg)

    # 2. Check if this is a reply to another message with an image
    if not file_info and hasattr(msg, "quote") and msg.quote and isinstance(msg.quote, dict):
        quote_msg_id = msg.quote.get("message_id") or msg.quote.get("messageId")
        if quote_msg_id:
            try:
                quoted_msg = bot.rpc.get_message(accid, quote_msg_id)
                file_info = dc_helpers._get_msg_file_info(bot, accid, quoted_msg)
            except Exception as e:
                config.logger.warning(f"Failed to fetch quoted message for sticker: {e}")

    if not file_info:
        usage = (
            "ℹ️ **Sticker Generator Usage**:\n"
            "• Reply to any image with `/sticker` or `/stickernobg`\n"
            "• Or send an image with `/sticker` or `/stickernobg` in the caption\n\n"
            "Commands:\n"
            "• `/sticker` — Convert image to a sticker (preserves original image/background)\n"
            "• `/stickernobg` — Convert image to a sticker with background removed"
        )
        dc_helpers._send(bot, accid, msg.chat_id, usage, reply_to_id=msg.id)
        return

    dc_helpers._react(bot, accid, msg.id, "⏳")

    threading.Thread(
        target=_bg_sticker_worker,
        args=(bot, accid, msg.chat_id, msg.id, file_info, remove_bg),
        daemon=True,
    ).start()


@config.dc_cli.on(events.NewMessage(command="/sticker"))
def sticker_command(bot, accid, event, skip_cooldown: bool = False):
    handle_sticker_command(bot, accid, event, remove_bg=False, skip_cooldown=skip_cooldown)


@config.dc_cli.on(events.NewMessage(command="/stickernobg"))
def stickernobg_command(bot, accid, event, skip_cooldown: bool = False):
    handle_sticker_command(bot, accid, event, remove_bg=True, skip_cooldown=skip_cooldown)
