"""High-quality image-loading helpers for display in the UI.

Both helpers use ``GdkPixbuf.InterpType.HYPER`` (a high-quality bicubic/
Gaussian resampling algorithm) which is significantly sharper than the
default ``BILINEAR`` for the large downscale ratios typical of cover art
and screenshots.
"""

from __future__ import annotations


def load_cover_texture(path: str, target_w: int, target_h: int):
    """Load *path*, scale-to-cover, and center-crop to target_w × target_h.

    Returns a ``Gdk.Texture`` whose pixel dimensions equal the target so that
    ``Gtk.Picture`` reports the correct natural size for layout.
    Returns ``None`` on any error.
    """
    try:
        from gi.repository import Gdk, GdkPixbuf

        src = GdkPixbuf.Pixbuf.new_from_file(path)
        src_w, src_h = src.get_width(), src.get_height()
        scale = max(target_w / src_w, target_h / src_h)
        scaled_w = max(int(src_w * scale), target_w)
        scaled_h = max(int(src_h * scale), target_h)
        scaled = src.scale_simple(scaled_w, scaled_h, GdkPixbuf.InterpType.HYPER)
        x_off = (scaled_w - target_w) // 2
        y_off = (scaled_h - target_h) // 2
        cropped = scaled.new_subpixbuf(x_off, y_off, target_w, target_h)
        return Gdk.Texture.new_for_pixbuf(cropped)
    except Exception:
        return None


def load_fit_texture(path: str, target_h: int):
    """Load *path* and scale to *target_h* pixels, preserving aspect ratio.

    Returns a ``Gdk.Texture`` or ``None`` on any error.
    """
    try:
        from gi.repository import Gdk, GdkPixbuf

        src = GdkPixbuf.Pixbuf.new_from_file(path)
        src_w, src_h = src.get_width(), src.get_height()
        scaled_w = max(1, round(src_w * target_h / src_h))
        scaled = src.scale_simple(scaled_w, target_h, GdkPixbuf.InterpType.HYPER)
        return Gdk.Texture.new_for_pixbuf(scaled)
    except Exception:
        return None
