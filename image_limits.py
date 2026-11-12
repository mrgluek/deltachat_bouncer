"""Shared input limits for image conversion, including the sticker subprocess."""
import os
import warnings
from contextlib import contextmanager

MAX_IMAGE_PIXELS = 24_000_000
MAX_IMAGE_BYTES = 25 * 1024 * 1024


@contextmanager
def open_image(path):
    from PIL import Image

    if os.path.getsize(path) > MAX_IMAGE_BYTES:
        raise ValueError("Image file is too large (maximum 25 MB).")
    Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(path) as img:
            w, h = img.size
            if w <= 0 or h <= 0 or w * h > MAX_IMAGE_PIXELS:
                raise ValueError("Image dimensions are too large or invalid (maximum 24 million pixels).")
            img.load()
            yield img
