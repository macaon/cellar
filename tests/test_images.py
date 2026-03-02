"""Tests for cellar/utils/images.py."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from cellar.utils.images import load_and_crop, load_and_fit, optimize_image


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def png_100x200(tmp_path: Path) -> Path:
    """Create a 100×200 red PNG."""
    img = Image.new("RGBA", (100, 200), (255, 0, 0, 255))
    p = tmp_path / "tall.png"
    img.save(p)
    return p


@pytest.fixture()
def png_200x100(tmp_path: Path) -> Path:
    """Create a 200×100 blue PNG."""
    img = Image.new("RGBA", (200, 100), (0, 0, 255, 255))
    p = tmp_path / "wide.png"
    img.save(p)
    return p


@pytest.fixture()
def ico_file(tmp_path: Path) -> Path:
    """Create a real ICO file with a 64×64 frame (BMP-encoded)."""
    img = Image.new("RGBA", (64, 64), (0, 255, 0, 255))
    p = tmp_path / "icon.ico"
    img.save(p, format="ICO")
    return p


@pytest.fixture()
def large_png(tmp_path: Path) -> Path:
    """Create a 3000×2000 PNG (exceeds most role max sizes)."""
    img = Image.new("RGB", (3000, 2000), (128, 128, 128))
    p = tmp_path / "large.png"
    img.save(p)
    return p


@pytest.fixture()
def svg_file(tmp_path: Path) -> Path:
    """Create a minimal valid SVG (64×64 red square)."""
    p = tmp_path / "icon.svg"
    p.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64">'
        '<rect width="64" height="64" fill="red"/>'
        "</svg>"
    )
    return p


# ---------------------------------------------------------------------------
# load_and_crop
# ---------------------------------------------------------------------------

def test_load_and_crop_returns_exact_size(png_100x200):
    data = load_and_crop(str(png_100x200), 50, 50)
    assert data is not None
    img = Image.open(BytesIO(data))
    assert img.size == (50, 50)


def test_load_and_crop_wide_image(png_200x100):
    data = load_and_crop(str(png_200x100), 75, 96)
    assert data is not None
    img = Image.open(BytesIO(data))
    assert img.size == (75, 96)


def test_load_and_crop_missing_file():
    assert load_and_crop("/nonexistent/file.png", 50, 50) is None


# ---------------------------------------------------------------------------
# load_and_fit
# ---------------------------------------------------------------------------

def test_load_and_fit_returns_square(png_100x200):
    data = load_and_fit(str(png_100x200), 48)
    assert data is not None
    img = Image.open(BytesIO(data))
    assert img.size == (48, 48)


def test_load_and_fit_non_square_not_distorted(png_100x200):
    """A tall image (100×200) fitted into 48×48 should be pillarboxed, not cropped/squished."""
    data = load_and_fit(str(png_100x200), 48)
    assert data is not None
    img = Image.open(BytesIO(data)).convert("RGBA")
    assert img.size == (48, 48)
    # The image is red (255,0,0,255); top-left corner should be transparent padding.
    assert img.getpixel((0, 0))[3] == 0, "corner pixel should be transparent padding"
    # Centre pixel should be opaque red content.
    cx, cy = img.size[0] // 2, img.size[1] // 2
    r, g, b, a = img.getpixel((cx, cy))
    assert a == 255 and r > 200, "centre pixel should be opaque red content"


def test_load_and_fit_ico(ico_file):
    data = load_and_fit(str(ico_file), 52)
    assert data is not None
    img = Image.open(BytesIO(data))
    assert img.size == (52, 52)


def test_load_and_fit_missing_file():
    assert load_and_fit("/nonexistent/file.png", 48) is None


def test_load_and_fit_missing_svg():
    assert load_and_fit("/nonexistent/icon.svg", 48) is None


def test_load_and_fit_svg(svg_file):
    pytest.importorskip("gi.repository.GdkPixbuf")
    data = load_and_fit(str(svg_file), 48)
    assert data is not None
    img = Image.open(BytesIO(data))
    assert img.size == (48, 48)


def test_load_and_crop_svg(svg_file):
    pytest.importorskip("gi.repository.GdkPixbuf")
    data = load_and_crop(str(svg_file), 75, 96)
    assert data is not None
    img = Image.open(BytesIO(data))
    assert img.size == (75, 96)


# ---------------------------------------------------------------------------
# optimize_image — ICO conversion
# ---------------------------------------------------------------------------

def test_optimize_ico_to_png(ico_file, tmp_path):
    dest = tmp_path / "out" / "icon.png"
    dest.parent.mkdir()
    optimize_image(ico_file, dest, "icon")
    assert dest.exists()
    img = Image.open(dest)
    assert img.format == "PNG"


# ---------------------------------------------------------------------------
# optimize_image — downscaling
# ---------------------------------------------------------------------------

def test_optimize_large_hero_becomes_jpeg(large_png, tmp_path):
    dest = tmp_path / "out" / "hero.png"
    dest.parent.mkdir()
    optimize_image(large_png, dest, "hero")
    # Should have been resized and converted to JPEG, then renamed to dest
    assert dest.exists()
    img = Image.open(dest)
    assert img.size[0] <= 1920
    assert img.size[1] <= 620


def test_optimize_small_icon_copied_as_is(tmp_path):
    small = Image.new("RGBA", (32, 32), (255, 0, 0, 255))
    src = tmp_path / "small.png"
    small.save(src)
    dest = tmp_path / "out" / "icon.png"
    dest.parent.mkdir()
    optimize_image(src, dest, "icon")
    assert dest.exists()
    assert dest.read_bytes() == src.read_bytes()


def test_optimize_small_cover_copied_as_is(tmp_path):
    """Cover within limits should be copied verbatim."""
    small = Image.new("RGB", (200, 300), (0, 128, 0))
    src = tmp_path / "cover.png"
    small.save(src)
    dest = tmp_path / "out" / "cover.png"
    dest.parent.mkdir()
    optimize_image(src, dest, "cover")
    assert dest.exists()
    assert dest.read_bytes() == src.read_bytes()
