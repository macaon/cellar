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
    "logo":       (300, 300),   # Steam-style transparent logo; output always PNG
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


def load_logo(path: str, target_height: int, max_width: int = 300) -> bytes | None:
    """Crop transparent logo to content bounds, then scale to *target_height*.

    Steps:
    1. Open the image and convert to RGBA.
    2. ``getbbox()`` → crop to the tight non-transparent bounds.
    3. Scale so the height is exactly *target_height* (preserving aspect ratio).
    4. If the resulting width exceeds *max_width*, clamp with ``thumbnail()``.

    Returns PNG bytes suitable for :func:`to_texture`, or ``None`` on error.
    """
    try:
        with Image.open(path) as img:
            img = img.convert("RGBA")
            bbox = img.getbbox()
            if bbox:
                img = img.crop(bbox)
            src_w, src_h = img.size
            if src_h == 0:
                return None
            new_w = max(1, int(src_w * target_height / src_h))
            img = img.resize((new_w, target_height), Image.LANCZOS)
            if new_w > max_width:
                img.thumbnail((max_width, target_height), Image.LANCZOS)
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

def optimize_image(src: str | Path, dest, role: str) -> None:
    """Copy *src* to *dest*, converting/downscaling as needed.

    *dest* may be a :class:`pathlib.Path` or any object with ``.write_bytes()``
    (e.g. :class:`~cellar.utils.smb.SmbPath`).  Non-``Path`` destinations are
    handled by writing to a temporary local file first, then uploading.

    - ICO files are converted to PNG (Pillow handles all ICO frame types).
    - Images exceeding the role's max dimensions are downscaled and saved
      as JPEG at 85 % quality.
    - Images already within limits are copied as-is.
    """
    if not isinstance(dest, Path):
        # Non-local destination (e.g. SmbPath): use a temp file then upload.
        import tempfile
        suffix = getattr(dest, "suffix", "") or Path(str(src)).suffix or ".png"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tf:
            tmp = Path(tf.name)
        try:
            optimize_image(src, tmp, role)
            dest.write_bytes(tmp.read_bytes())
        finally:
            tmp.unlink(missing_ok=True)
        return

    src = Path(src)

    # ICO / BMP → PNG conversion (any role).
    if src.suffix.lower() in (".ico", ".bmp"):
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
            # Last resort: copy the raw file.
            shutil.copyfile(src, dest)
            return

    max_dims = _IMAGE_MAX_SIZE.get(role)

    # Logo: always convert to PNG to preserve transparency; always run through
    # Pillow so the dest extension (.png) is valid regardless of source format.
    if role == "logo" and max_dims:
        try:
            with Image.open(src) as img:
                img = img.convert("RGBA")
                img.thumbnail(max_dims, Image.LANCZOS)
                png_dest = dest.with_suffix(".png")
                img.save(png_dest, format="PNG", optimize=True)
                if png_dest != dest:
                    png_dest.rename(dest)
        except Exception:
            shutil.copyfile(src, dest)
        return

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
