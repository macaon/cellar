"""Pillow-based image helpers.

Replaces all GdkPixbuf usage for image loading, resizing, cropping, and
format conversion.  Pillow handles ICO files (all frame types, including
BMP-encoded frames) out of the box — no hand-rolled struct parsing needed.
"""

from __future__ import annotations

import logging
import shutil
from io import BytesIO
from pathlib import Path

from PIL import Image

log = logging.getLogger(__name__)

# Maximum dimensions per image role for import-time optimisation.
_IMAGE_MAX_SIZE: dict[str, tuple[int, int]] = {
    "icon":       (256, 256),
    "cover":      (300, 400),
    "hero":       (1920, 620),
    "screenshot": (1920, 1080),
}

_JPEG_QUALITY = 85


# ---------------------------------------------------------------------------
# Public helpers for runtime image display (browse / detail views)
# ---------------------------------------------------------------------------

def _svg_rasterise(path: str, w: int, h: int) -> bytes | None:
    """Rasterise an SVG to PNG bytes at *w*×*h* via GdkPixbuf (librsvg).

    Called as a fast-path for ``.svg`` files before Pillow is tried, since
    Pillow has no SVG support.  Returns ``None`` on any failure.
    """
    try:
        import gi
        gi.require_version("GdkPixbuf", "2.0")
        from gi.repository import GdkPixbuf
        pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(path, w, h, False)
        ok, buf = pb.save_to_bufferv("png", [], [])
        return bytes(buf) if ok else None
    except Exception:
        return None


def load_and_crop(path: str, w: int, h: int) -> bytes | None:
    """Scale-to-cover and center-crop to exactly *w* × *h*.

    Returns PNG bytes suitable for :func:`to_texture`, or ``None`` on error.
    """
    if path.lower().endswith(".svg"):
        return _svg_rasterise(path, w, h)
    try:
        with Image.open(path) as img:
            img = img.convert("RGBA")
            src_w, src_h = img.size
            scale = max(w / src_w, h / src_h)
            scaled_w = max(int(src_w * scale), w)
            scaled_h = max(int(src_h * scale), h)
            img = img.resize((scaled_w, scaled_h), Image.LANCZOS)
            x_off = (scaled_w - w) // 2
            y_off = (scaled_h - h) // 2
            img = img.crop((x_off, y_off, x_off + w, y_off + h))
            buf = BytesIO()
            img.save(buf, format="PNG")
            return buf.getvalue()
    except Exception:
        return None


def load_and_fit(path: str, size: int) -> bytes | None:
    """Scale image to fit within *size* × *size*, preserving aspect ratio.

    Non-square images are scaled down uniformly and centered on a transparent
    *size* × *size* canvas — no cropping, no distortion.
    Returns PNG bytes suitable for :func:`to_texture`, or ``None`` on error.
    """
    if path.lower().endswith(".svg"):
        return _svg_rasterise(path, size, size)
    try:
        with Image.open(path) as img:
            img = img.convert("RGBA")
            src_w, src_h = img.size
            scale = min(size / src_w, size / src_h)
            new_w = max(1, int(src_w * scale))
            new_h = max(1, int(src_h * scale))
            img = img.resize((new_w, new_h), Image.LANCZOS)
            canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
            canvas.paste(img, ((size - new_w) // 2, (size - new_h) // 2))
            buf = BytesIO()
            canvas.save(buf, format="PNG")
            return buf.getvalue()
    except Exception:
        return None


def to_texture(png_bytes: bytes):
    """Convert raw PNG bytes to a ``Gdk.Texture``.

    Keeps the GTK/GDK import isolated so callers don't need to import
    ``gi.repository`` themselves.
    """
    from gi.repository import Gdk, GLib
    return Gdk.Texture.new_from_bytes(GLib.Bytes.new(png_bytes))


# ---------------------------------------------------------------------------
# Import-time image optimisation (packager)
# ---------------------------------------------------------------------------

def optimize_image(src: str | Path, dest: Path, role: str) -> None:
    """Copy *src* to *dest*, converting/downscaling as needed.

    - ICO files are converted to PNG (Pillow handles all ICO frame types).
    - Images exceeding the role's max dimensions are downscaled and saved
      as JPEG at 85 % quality.
    - Images already within limits are copied as-is.
    """
    src = Path(src)

    # ICO → PNG conversion (any role).
    if src.suffix.lower() == ".ico":
        try:
            with Image.open(src) as img:
                # ICO files may contain multiple sizes; Pillow loads the
                # largest by default.
                img = img.convert("RGBA")
                png_dest = dest.with_suffix(".png")
                img.save(png_dest, format="PNG")
                if png_dest != dest:
                    png_dest.rename(dest)
                return
        except Exception:
            # Last resort: copy the raw ICO file.
            shutil.copyfile(src, dest)
            return

    max_dims = _IMAGE_MAX_SIZE.get(role)
    if not max_dims or role == "icon":
        shutil.copyfile(src, dest)
        return

    try:
        with Image.open(src) as img:
            orig_w, orig_h = img.size
            max_w, max_h = max_dims
            if orig_w <= max_w and orig_h <= max_h:
                shutil.copyfile(src, dest)
                return
            img.thumbnail((max_w, max_h), Image.LANCZOS)
            jpeg_dest = dest.with_suffix(".jpg")
            img.convert("RGB").save(jpeg_dest, format="JPEG", quality=_JPEG_QUALITY)
            if jpeg_dest != dest:
                jpeg_dest.rename(dest)
    except Exception:
        shutil.copyfile(src, dest)
