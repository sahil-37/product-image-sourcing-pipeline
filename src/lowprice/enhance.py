"""Phase 1.5 (optional, off by default): background regeneration + logo watermark.

Runs AFTER Phase 1 has produced an accepted library/<id>.jpg. Never touches
the sourced product photo's own pixels for the product itself:

1. Segment the product (foreground) out of the accepted image with rembg.
2. Ask Gemini's native image-output model for a NEW background only (no
   product in the generation request) — flavour-driven set dressing via
   FLAVOR_BACKGROUND_HINTS.
3. Composite the ORIGINAL, unmodified foreground cutout back on top of the
   generated background, programmatically (Pillow) — the model never sees or
   regenerates the product itself.
4. Composite the company logo at a fixed, deterministic position/opacity
   (Pillow) — never left to a generative model's pixel placement.

This is deliberately a separate stage from pipeline.py's core run: it costs
real money per image and regenerates pixels around (not of) copyrighted
product photography, so it stays opt-in (ENHANCE_ENABLED / --enhance) and
never modifies library/*.jpg in place — output goes to library_enhanced/.
"""
from __future__ import annotations

import base64
import io
import logging
import re
from pathlib import Path

from google import genai
from PIL import Image, ImageDraw, ImageFilter
from rembg import new_session, remove

from . import config

# The logo composite lives in watermark.py so it can be imported without rembg
# (see that module's docstring). Re-exported here so existing callers of
# enhance.apply_watermark keep working unchanged.
from .watermark import (  # noqa: F401
    _emboss_tile,
    _load_logo,
    _scaled_logo,
    _tile_across,
    apply_watermark,
)

log = logging.getLogger("enhance")


class EnhanceError(Exception):
    pass


# Product-category hints — used only when nothing more specific matches, so a
# "POLAR SELTZER LEMON" gets lemons rather than generic bubbles.
_CATEGORY_HINTS = {
    "seltzer", "water", "club soda", "cola", "coke", "pepsi",
    "energy", "coffee", "tea", "tonic",
}


_client: genai.Client | None = None
_rembg_session = None


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        if not config.GEMINI_API_KEY:
            raise EnhanceError("GEMINI_API_KEY not set. Configure credentials per README.")
        _client = genai.Client(api_key=config.GEMINI_API_KEY)
    return _client


def _get_rembg_session():
    global _rembg_session
    if _rembg_session is None:
        _rembg_session = new_session(config.FOREGROUND_MODEL)
    return _rembg_session


def segment_foreground(image_path: Path) -> Image.Image:
    """Return an RGBA cutout of the product with the background made transparent."""
    raw = image_path.read_bytes()
    try:
        cut = remove(raw, session=_get_rembg_session())
    except Exception as exc:  # rembg/onnxruntime raise assorted backend errors
        raise EnhanceError(f"foreground segmentation failed for {image_path}: {exc}") from exc
    return Image.open(io.BytesIO(cut)).convert("RGBA")


def _background_prompt(product_name: str) -> str:
    """Build the background prompt for one product.

    Flavour matching has two wrinkles worth knowing about:

    * The trailing parenthetical in these catalogue names is a VENDOR code, not
      a flavour — "12PK SPRITE CAN (COKE)" is Sprite, distributed by Coke. It is
      stripped before matching so Sprite doesn't get a cola background.
    * Keys are ranked by specificity, not length: a real flavour ("lemon") beats
      a product-category fallback ("seltzer", "water", "cola"), and a compound
      flavour ("black cherry") beats the simple token inside it ("cherry").
    """
    lowered = re.sub(r"\([^)]*\)", " ", product_name.lower())

    def rank(key: str) -> tuple[int, int]:
        # category fallbacks lose to any real flavour; then prefer the longer key
        return (0 if key in _CATEGORY_HINTS else 1, len(key))

    hint = ""
    for flavor in sorted(config.FLAVOR_BACKGROUND_HINTS, key=rank, reverse=True):
        if flavor in lowered:
            hint = f" {config.FLAVOR_BACKGROUND_HINTS[flavor]}"
            break

    return (
        f"{config.BACKGROUND_STYLE}{hint}. "
        "The centre of the frame must stay clear and uncluttered — all set "
        "dressing belongs around the edges, well away from the middle. "
        "Do not include any product, bottle, can, box, or container in the "
        "image — background and surface only, ready for a product to be "
        "composited on top afterward."
    )


def generate_background(product_name: str, size_px: int) -> Image.Image:
    """Ask Gemini's native image-output model (via the interactions API) for a
    background-only image (no product)."""
    client = _get_client()
    try:
        interaction = client.interactions.create(
            model=f"models/{config.GEMINI_IMAGE_MODEL}",
            input=_background_prompt(product_name),
            generation_config={
                "temperature": 1,
                "max_output_tokens": 65536,
                "top_p": 0.95,
                # thinking_level is deliberately omitted: this model rejects both
                # "minimal" and "low" with a 400 (and its error message
                # contradicts itself about which are allowed), so we let it use
                # its own default rather than pin a value that may stop working.
                "image_config": {"image_size": "1K"},
            },
            response_modalities=["image", "text"],
        )
    except Exception as exc:  # google-genai raises assorted API/transport errors
        raise EnhanceError(f"Gemini background generation failed: {exc}") from exc

    image_bytes = None

    # Simplest path: the top-level convenience field.
    if interaction.output_image and interaction.output_image.data:
        image_bytes = base64.b64decode(interaction.output_image.data)

    # Fallback: walk steps for a model_output step carrying an image part.
    if not image_bytes:
        for step in interaction.steps or []:
            if getattr(step, "type", None) != "model_output" or not step.content:
                continue
            for part in step.content:
                if getattr(part, "type", None) == "image" and getattr(part, "data", None):
                    image_bytes = base64.b64decode(part.data)
                    break
            if image_bytes:
                break

    if not image_bytes:
        raise EnhanceError("Gemini background generation returned no image data")

    bg = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    if bg.size != (size_px, size_px):
        bg = bg.resize((size_px, size_px), Image.LANCZOS)
    return bg


def _contact_shadow(canvas_size: tuple[int, int], fg_box: tuple[int, int, int, int]) -> Image.Image:
    """A soft elliptical shadow under the product, as an RGBA layer.

    The generated background has no idea where the cutout will land, so the
    shadow that visually seats the product on the surface is drawn here instead
    — otherwise the product reads as floating.
    """
    left, top, right, bottom = fg_box
    fg_w, fg_h = right - left, bottom - top

    layer = Image.new("RGBA", canvas_size, (0, 0, 0, 0))
    ell_w = int(fg_w * config.SHADOW_WIDTH_FRAC)
    ell_h = max(4, int(fg_h * config.SHADOW_HEIGHT_FRAC))
    cx = left + fg_w // 2
    cy = bottom  # sit the ellipse on the product's base

    draw = ImageDraw.Draw(layer)
    draw.ellipse(
        [cx - ell_w // 2, cy - ell_h // 2, cx + ell_w // 2, cy + ell_h // 2],
        fill=(0, 0, 0, int(255 * config.SHADOW_OPACITY)),
    )
    blur = max(1, int(canvas_size[0] * config.SHADOW_BLUR_FRAC))
    return layer.filter(ImageFilter.GaussianBlur(blur))


def composite_foreground_on_background(foreground: Image.Image, background: Image.Image) -> Image.Image:
    """Seat the untouched product cutout on the generated background.

    The cutout is trimmed to its own content, scaled to a consistent share of
    the frame, and stood on the lower third where the prompt asks for a surface
    — with a soft contact shadow so it doesn't float.
    """
    canvas = background.convert("RGBA").copy()

    fg = foreground.copy()
    bbox = fg.getbbox()  # drop transparent margin so scaling is about the product
    if bbox:
        fg = fg.crop(bbox)

    target_h = int(canvas.height * config.PRODUCT_SCALE_FRAC)
    scale = target_h / fg.height
    if int(fg.width * scale) > canvas.width * config.PRODUCT_SCALE_FRAC:
        scale = (canvas.width * config.PRODUCT_SCALE_FRAC) / fg.width
    fg = fg.resize((max(1, int(fg.width * scale)), max(1, int(fg.height * scale))), Image.LANCZOS)

    # Stand it slightly below centre, on the surface the prompt puts there.
    x = (canvas.width - fg.width) // 2
    y = int(canvas.height * 0.94) - fg.height

    if config.SHADOW_ENABLED:
        canvas.alpha_composite(
            _contact_shadow(canvas.size, (x, y, x + fg.width, y + fg.height))
        )

    canvas.paste(fg, (x, y), fg)  # fg's own alpha channel as the paste mask
    return canvas.convert("RGB")


def watermark_product_image(item_id: str, source_path: Path) -> Path:
    """Logo-only variant: composite the logo onto the accepted image as-is.

    No segmentation and no generative call, so it costs nothing and cannot
    damage the product pixels. This is the right choice when the sourced photo
    already sits on a clean white background (which most catalogue product
    photography does) — there is no background worth replacing, and rembg
    tends to chew the edges of transparent bottles.
    """
    image = Image.open(source_path).convert("RGB")
    watermarked = apply_watermark(image)

    output_path = config.ENHANCED_DIR / f"{item_id}.jpg"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    watermarked.save(output_path, format="JPEG", quality=config.JPEG_QUALITY)
    return output_path


def enhance_product_image(item_id: str, product_name: str, source_path: Path) -> Path:
    """Full Phase 1.5 flow for one already-accepted library image. Returns the
    output path under library_enhanced/."""
    foreground = segment_foreground(source_path)
    background = generate_background(product_name, config.OUTPUT_SIZE)
    composited = composite_foreground_on_background(foreground, background)
    watermarked = apply_watermark(composited)

    output_path = config.ENHANCED_DIR / f"{item_id}.jpg"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    watermarked.save(output_path, format="JPEG", quality=config.JPEG_QUALITY)
    return output_path
