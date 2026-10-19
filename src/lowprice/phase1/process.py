"""Download, free pre-filter, JPEG-bytes-for-vision, and normalize-to-square.

Every image lands as JPEG regardless of source format (webp/png/etc) — that's
what makes "webp downloaded instead" stop being a category of failure.
"""
from __future__ import annotations

import io
from pathlib import Path
from urllib.parse import urlparse

import requests
from PIL import Image, UnidentifiedImageError

from .. import config
from ..http import session
from .normalize import domain_in_set, domain_of
from .search import Candidate

DOWNLOAD_TIMEOUT_SECONDS = 15


class ProcessError(Exception):
    pass


def prefilter(candidates: list[Candidate]) -> list[Candidate]:
    """Drop stock-agency domains only — their images are watermarked by definition.

    Nothing else is filtered here. Resolution and aspect ratio used to be gates
    and were silently killing correct products (a 200x200 "Coke Classic 12PK of
    12oz Cans" never reached the model), so every other candidate goes through
    for the model to judge visually.
    """
    kept = []
    for c in candidates:
        domain = domain_of(c.source or c.url)
        if domain_in_set(domain, config.WATERMARK_DOMAINS):
            continue
        kept.append(c)
    return kept


def download_image(candidate: Candidate) -> bytes:
    """Fetch raw image bytes. Local mock paths are read directly."""
    parsed = urlparse(candidate.url)
    if parsed.scheme in ("", "file") or Path(candidate.url).exists():
        path = Path(parsed.path if parsed.scheme == "file" else candidate.url)
        return path.read_bytes()

    try:
        resp = session().get(candidate.url, timeout=DOWNLOAD_TIMEOUT_SECONDS)
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise ProcessError(f"download failed for {candidate.url}: {exc}") from exc
    return resp.content


def to_jpeg_bytes(raw: bytes) -> bytes:
    """Convert arbitrary image bytes (webp/png/gif/etc) to in-memory JPEG bytes."""
    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
    except UnidentifiedImageError as exc:
        raise ProcessError(f"unreadable image data: {exc}") from exc

    if img.mode != "RGB":
        img = img.convert("RGB")

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=config.JPEG_QUALITY)
    return buf.getvalue()


def resize_to_square(raw: bytes, output_path: Path) -> None:
    """Fit the image onto a centered OUTPUT_SIZE x OUTPUT_SIZE white canvas, save as JPEG."""
    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
    except UnidentifiedImageError as exc:
        raise ProcessError(f"unreadable image data: {exc}") from exc

    if img.mode != "RGB":
        img = img.convert("RGB")

    size = config.OUTPUT_SIZE
    img.thumbnail((size, size), Image.LANCZOS)

    canvas = Image.new("RGB", (size, size), config.CANVAS_BACKGROUND)
    offset = ((size - img.width) // 2, (size - img.height) // 2)
    canvas.paste(img, offset)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output_path, format="JPEG", quality=config.JPEG_QUALITY)
