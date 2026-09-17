#!/usr/bin/env python3
"""
sticker_tool.py - Standalone CLI helper for Delta Chat sticker generation.

Executing background removal in an isolated process guarantees that 100% of the
memory allocated by heavy scientific/ML libraries (onnxruntime, scipy, pymatting, rembg)
is completely reclaimed by the operating system kernel immediately upon process exit.
"""
import os
import sys
import argparse


def main():
    parser = argparse.ArgumentParser(description="Delta Chat Sticker Generator")
    parser.add_argument("src", help="Path to input image file")
    parser.add_argument("dest", help="Path to output WebP sticker file")
    parser.add_argument("--nobg", action="store_true", help="Remove background using rembg")
    parser.add_argument("--max-dim", type=int, default=512, help="Maximum dimension in pixels (default: 512)")
    args = parser.parse_args()

    try:
        from PIL import Image, ImageOps
    except ImportError:
        sys.stderr.write("Pillow library is not installed.\n")
        sys.exit(2)

    try:
        with Image.open(args.src) as raw_img:
            try:
                img = ImageOps.exif_transpose(raw_img)
            except Exception:
                img = raw_img

            # Extract first frame for animated images
            if getattr(img, "is_animated", False):
                try:
                    img.seek(0)
                except Exception:
                    pass

            w, h = img.size
            if w <= 0 or h <= 0:
                sys.stderr.write("Invalid image dimensions.\n")
                sys.exit(3)

            # Pre-scale so longest side is max_dim BEFORE background removal (drastically saves CPU & RAM)
            max_dim = args.max_dim
            if max(w, h) != max_dim:
                scale = float(max_dim) / max(w, h)
                new_w = max(1, int(round(w * scale)))
                new_h = max(1, int(round(h * scale)))
                img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)

            if args.nobg:
                enable_rembg = os.getenv("ENABLE_REMBG", "true").strip().lower()
                if enable_rembg in ("0", "false", "off", "no", "disabled"):
                    sys.stderr.write("Background removal is disabled on this server. Use /sticker to create a sticker with the background preserved.\n")
                    sys.exit(4)

                try:
                    import onnxruntime as ort
                    import rembg
                except ImportError:
                    sys.stderr.write("Background removal requires the 'rembg' package. Use /sticker to create a sticker with the background preserved.\n")
                    sys.exit(5)

                # Configure ONNX Runtime to avoid persistent memory arena bloat
                opts = ort.SessionOptions()
                opts.enable_cpu_mem_arena = False
                opts.enable_mem_pattern = False
                opts.intra_op_num_threads = 2
                opts.inter_op_num_threads = 1

                model_name = os.getenv("REMBG_MODEL", "u2netp").strip() or "u2netp"
                u2net_dir = os.getenv("U2NET_HOME") or os.getenv("REMBG_HOME")
                if u2net_dir:
                    try:
                        os.makedirs(u2net_dir, exist_ok=True)
                    except Exception:
                        pass

                session = rembg.new_session(model_name, sess_opts=opts)
                if img.mode not in ("RGB", "RGBA"):
                    img = img.convert("RGBA")
                img = rembg.remove(img, session=session)
            else:
                if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in getattr(img, "info", {})):
                    if img.mode != "RGBA":
                        img = img.convert("RGBA")
                elif img.mode != "RGB":
                    img = img.convert("RGBA")

            # Final check to ensure sticker dimensions do not exceed max_dim
            w, h = img.size
            if max(w, h) > max_dim:
                scale = float(max_dim) / max(w, h)
                new_w = max(1, int(round(w * scale)))
                new_h = max(1, int(round(h * scale)))
                img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)

            img.save(args.dest, format="WEBP", quality=90, method=6)
            sys.exit(0)
    except Exception as e:
        sys.stderr.write(f"Failed to process sticker: {e}\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
