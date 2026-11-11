"""Storage limits and safe removal for cached channel attachments."""
import logging
import os
import tempfile
import threading
from pathlib import Path

import config
import state

logger = logging.getLogger("bouncer_bot")
_lock = threading.RLock()


def inside_media_dir(path):
    root = Path(state.CHANNEL_MEDIA_DIR).resolve()
    target = Path(path).resolve()
    return target != root and root in target.parents


def delete_file(path):
    if path and inside_media_dir(path):
        try:
            Path(path).unlink(missing_ok=True)
        except OSError as e:
            logger.warning(f"Failed to remove channel media: {e}")


def remove_channel_files(token, retained_paths=()):
    with _lock:
        folder = Path(state.CHANNEL_MEDIA_DIR) / token
        if not inside_media_dir(folder) or not folder.is_dir():
            return
        retained = {Path(path).resolve() for path in retained_paths if path}
        for path in folder.rglob('*'):
            if (path.is_file() or path.is_symlink()) and path.resolve() not in retained:
                delete_file(path)


def _total_bytes():
    total = 0
    for folder, _, files in os.walk(state.CHANNEL_MEDIA_DIR, followlinks=False):
        for name in files:
            path = os.path.join(folder, name)
            if inside_media_dir(path) and os.path.isfile(path):
                total += os.path.getsize(path)
    return total


def store_file(source, destination, optimize=False):
    """Serialize quota checks and writes; return False when media is skipped."""
    temporary = None
    with _lock:
        try:
            if not inside_media_dir(destination):
                return False
            size = os.path.getsize(source)
            previous = os.path.getsize(destination) if os.path.isfile(destination) else 0
            available = config.CHANNEL_MEDIA_MAX_TOTAL_BYTES - _total_bytes() + previous
            limit = min(config.CHANNEL_MEDIA_MAX_BYTES, available)
            if size > limit:
                logger.warning("Channel media skipped: attachment or storage size limit exceeded")
                return False
            os.makedirs(os.path.dirname(destination), exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=os.path.dirname(destination), suffix=".webp", delete=False) as output:
                temporary = output.name
                if not optimize:
                    with open(source, 'rb') as input_file:
                        remaining = limit
                        while chunk := input_file.read(min(65536, remaining + 1)):
                            if len(chunk) > remaining:
                                logger.warning("Channel media skipped: attachment grew beyond size limit")
                                return False
                            output.write(chunk)
                            remaining -= len(chunk)
            if optimize:
                import stickers
                if not stickers._optimize_image_to_webp(source, temporary):
                    return False
            if os.path.getsize(temporary) > limit:
                logger.warning("Channel media skipped: converted attachment exceeds size limit")
                return False
            os.replace(temporary, destination)
            return True
        except OSError as e:
            logger.warning(f"Failed to store channel media: {e}")
            return False
        finally:
            if temporary:
                delete_file(temporary)
