"""Logo watermarking — pure Pillow, no model calls, no heavy dependencies.

Split out of enhance.py so watermarking can be imported without dragging in
rembg/onnxruntime. enhance.py imports rembg at module scope for its background
regeneration feature, which meant that anything wanting only the logo composite
paid a multi-second import and a ~200MB dependency it never touched.

That mattered once the review app arrived: the local worker that watermarks a
stakeholder-supplied image needs `apply_watermark` and nothing else. It is also
the reason this lives in the package rather than being reimplemented on the web
side — the watermark is defined by a dozen config constants
(WATERMARK_STYLE/_TILE_OPACITY/_TILE_ANGLE/_TILE_GAP/...), and a second copy
would drift and give some products a visibly different logo from the rest.

One implementation, imported everywhere.
"""
from __future__ import annotations

import io
import math

from PIL import Image

from . import config


class WatermarkError(Exception):
    pass


def _load_logo() -> Image.Image:
    if not config.LOGO_PATH.exists():
        raise WatermarkError(
            f"logo file not found at {config.LOGO_PATH}. Add it before watermarking."
        )
    return Image.open(config.LOGO_PATH).convert("RGBA")


def _scaled_logo(canvas_w: int, width_frac: float, opacity: float) -> Image.Image:
    """Logo resized to a fraction of the canvas width, at the given opacity."""
    logo = _load_logo()
    target_w = max(1, int(canvas_w * width_frac))
    scale = target_w / logo.width
    logo = logo.resize((target_w, max(1, int(logo.height * scale))), Image.LANCZOS)
    if opacity < 1.0:
        logo.putalpha(logo.getchannel("A").point(lambda a: int(a * opacity)))
    return logo


def _emboss_tile(tile: Image.Image) -> Image.Image:
    """Recolour a logo tile into a two-tone relief.

    The logo's own colours are discarded and replaced with a dark copy offset
    down-right and a light copy offset up-left, so the shape reads as pressed
    into the surface rather than printed on it. Both copies come from the same
    alpha mask, which keeps the relief aligned with the artwork.
    """
    mask = tile.getchannel("A")
    w, h = tile.size
    off = max(1, w // 200)

    relief = Image.new("RGBA", (w + off, h + off), (0, 0, 0, 0))
    for colour, dest in (((0, 0, 0), (off, off)), ((255, 255, 255), (0, 0))):
        layer = Image.new("RGBA", relief.size, (0, 0, 0, 0))
        layer.paste(Image.new("RGBA", tile.size, (*colour, 255)), dest, mask)
        relief.alpha_composite(layer)
    return relief


def _tile_across(canvas: Image.Image, tile: Image.Image, angle: float, gap: float) -> Image.Image:
    """Repeat `tile` on a rotated, brick-offset lattice covering the whole canvas."""
    tile = tile.rotate(angle, expand=True, resample=Image.BICUBIC)
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))

    step_x = max(1, int(tile.width * gap))
    step_y = max(1, int(tile.height * gap))
    # Start outside the frame so rotated tiles still cover the corners.
    overhang = int(math.hypot(canvas.width, canvas.height)) // 2

    for row, y in enumerate(range(-overhang, canvas.height + overhang, step_y)):
        x_offset = step_x // 2 if row % 2 else 0  # brick pattern, avoids visible columns
        for x in range(-overhang + x_offset, canvas.width + overhang, step_x):
            layer.alpha_composite(tile, (x, y))

    out = canvas.copy()
    out.alpha_composite(layer)
    return out


def apply_watermark(image: Image.Image) -> Image.Image:
    """Composite the company logo. Deterministic — no model decides placement.

    Style comes from config.WATERMARK_STYLE: corner | center | tiled | emboss.
    """
    canvas = image.convert("RGBA")
    style = config.WATERMARK_STYLE

    if style == "center":
        logo = _scaled_logo(canvas.width, config.WATERMARK_CENTER_WIDTH_FRAC,
                            config.WATERMARK_CENTER_OPACITY)
        canvas.alpha_composite(
            logo,
            ((canvas.width - logo.width) // 2, (canvas.height - logo.height) // 2),
        )
        return canvas.convert("RGB")

    if style in ("tiled", "emboss"):
        if style == "emboss":
            tile = _emboss_tile(_scaled_logo(canvas.width, config.WATERMARK_TILE_WIDTH_FRAC, 1.0))
            tile.putalpha(tile.getchannel("A").point(
                lambda a: int(a * config.WATERMARK_EMBOSS_OPACITY)))
        else:
            tile = _scaled_logo(canvas.width, config.WATERMARK_TILE_WIDTH_FRAC,
                                config.WATERMARK_TILE_OPACITY)
        canvas = _tile_across(canvas, tile, config.WATERMARK_TILE_ANGLE, config.WATERMARK_TILE_GAP)
        return canvas.convert("RGB")

    # default: "corner"
    logo = _scaled_logo(canvas.width, config.WATERMARK_WIDTH_FRAC, config.WATERMARK_OPACITY)
    margin = int(canvas.width * config.WATERMARK_MARGIN_FRAC)
    positions = {
        "bottom-right": (canvas.width - logo.width - margin, canvas.height - logo.height - margin),
        "bottom-left": (margin, canvas.height - logo.height - margin),
        "top-right": (canvas.width - logo.width - margin, margin),
        "top-left": (margin, margin),
    }
    canvas.alpha_composite(logo, dest=positions.get(config.WATERMARK_POSITION,
                                                    positions["bottom-right"]))
    return canvas.convert("RGB")


def canonicalize(raw: bytes) -> Image.Image:
    """Fit arbitrary image bytes onto the standard square canvas.

    Mirrors process.resize_to_square, but returns an in-memory image instead of
    writing a file — the worker needs to watermark the result before it touches
    disk, and resize_to_square only has a path-writing form.
    """
    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
    except Exception as exc:
        raise WatermarkError(f"unreadable image data: {exc}") from exc

    if img.mode != "RGB":
        img = img.convert("RGB")

    size = config.OUTPUT_SIZE
    img.thumbnail((size, size), Image.LANCZOS)
    canvas = Image.new("RGB", (size, size), config.CANVAS_BACKGROUND)
    canvas.paste(img, ((size - img.width) // 2, (size - img.height) // 2))
    return canvas


def watermark_bytes(raw: bytes, canonicalize_first: bool = True) -> bytes:
    """Image bytes in, watermarked JPEG bytes out.

    The bytes-in/bytes-out form the worker needs: watermark_product_image()
    hardcodes its output to ENHANCED_DIR/<id>.jpg with no path parameter, so it
    cannot be used to produce an image destined for blob storage.

    canonicalize_first squares and resizes to OUTPUT_SIZE before watermarking,
    which is what a stakeholder-supplied URL needs — the watermark fractions are
    relative to canvas width, so watermarking an un-normalised image would scale
    the logo differently from every other product.
    """
    img = canonicalize(raw) if canonicalize_first else Image.open(io.BytesIO(raw)).convert("RGB")
    out = apply_watermark(img)
    buf = io.BytesIO()
    out.save(buf, format="JPEG", quality=config.JPEG_QUALITY)
    return buf.getvalue()
