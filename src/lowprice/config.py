"""Every knob for Phase 1 (and shared knobs for Phase 2) lives here.

Nothing in normalize.py / search.py / process.py / verify.py / pipeline.py /
review.py should hardcode a threshold, weight, domain, path, or model name —
import it from here instead.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------
# Project root — three levels up from src/lowprice/config.py. Overridable so the
# package can run against a different working tree (tests use this).
BASE_DIR = Path(
    os.environ.get("LOWPRICE_BASE_DIR", Path(__file__).resolve().parents[2])
)

DATA_DIR = BASE_DIR / "data"          # inputs and static assets, version-controlled
WORK_DIR = BASE_DIR / "work"          # generated state, gitignored
MOCK_IMAGES_DIR = WORK_DIR / "mock_images"
LIBRARY_DIR = BASE_DIR / "library"    # verified source images
MANIFEST_PATH = WORK_DIR / "manifest.json"
REVIEW_HTML_PATH = BASE_DIR / "review.html"

# .csv or .xlsx both supported. Override with INPUT_CSV=data/foo.xlsx (relative
# to BASE_DIR, or an absolute path) to run against a different catalogue file
# without editing this module.
if _input_csv_env := os.environ.get("INPUT_CSV"):
    _input_csv_path = Path(_input_csv_env)
    INPUT_CSV = _input_csv_path if _input_csv_path.is_absolute() else BASE_DIR / _input_csv_path
else:
    INPUT_CSV = DATA_DIR / "drinklist.xlsx"

# --------------------------------------------------------------------------
# Input columns (same names in the .csv and .xlsx exports)
# --------------------------------------------------------------------------
NAME_COL = "Description"
ID_COL = "Item Number"  # optional; if absent or blank per-row, id falls back to a slug of the name
PACK_COL = "Pack"  # optional; column holding a numeric pack count
SIZE_COL = "Size / UOM"  # optional; per-unit size, e.g. "1 LITER", "12 OZ"

# --------------------------------------------------------------------------
# Credentials / API config (values only; never put real keys here)
# --------------------------------------------------------------------------
SERPER_API_KEY = os.environ.get("SERPER_API_KEY", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

# Phase 1 candidate-selection (verify.py) — vision + strict JSON output.
# gemini-2.5-flash is gated off new accounts ("no longer available to new
# users") despite still appearing in the model catalog — gemini-3.1-flash-lite
# is confirmed working on this account.
GEMINI_VISION_MODEL = os.environ.get("GEMINI_VISION_MODEL", "gemini-3.1-flash-lite")

# Phase 1.5 background generation (enhance.py) — native image-output model,
# called via the interactions API (client.interactions.create).
GEMINI_IMAGE_MODEL = os.environ.get("GEMINI_IMAGE_MODEL", "gemini-3.1-flash-lite-image")

# --------------------------------------------------------------------------
# Search
# --------------------------------------------------------------------------
CANDIDATES_PER_PRODUCT = 10  # fetched from Serper; all of them go to the model
SEARCH_TIMEOUT_SECONDS = 15
SLEEP_BETWEEN = 0.5  # seconds paced between real network calls (search + verify)

# --------------------------------------------------------------------------
# Pre-filter (process.py, free)
# --------------------------------------------------------------------------
# Resolution is deliberately NOT a filter. A small-but-correct product photo is
# more useful than a large wrong one, and resize_to_square() shrinks (never
# crops) to OUTPUT_SIZE afterwards. The model can see for itself when an image
# is too soft to use.
#
# Stock-agency domains are dropped because their images are watermarked by
# definition — that is the one hard, free disqualifier.
WATERMARK_DOMAINS = {
    "shutterstock.com",
    "istockphoto.com",
    "gettyimages.com",
    "alamy.com",
    "dreamstime.com",
    "123rf.com",
    "depositphotos.com",
    "adobe.com",
    "stock.adobe.com",
    "vecteezy.com",
    "pinterest.com",
    "pinimg.com",
    "facebook.com",
    "instagram.com",
    "twitter.com",
    "x.com",
    "tiktok.com",
}

# --------------------------------------------------------------------------
# Scoring weights (normalize.score_candidate — TEXT SIGNALS ONLY)
# --------------------------------------------------------------------------
W_TITLE = 40      # product-name token overlap, scaled by overlap fraction
# `title` is the richest field in the image-search JSON (brand + variant + size
# + pack). Tokens found only in snippet/source/url count for this fraction of a
# title match.
W_SECONDARY_FIELD_FRAC = 0.35
W_SIZE = 10       # per matched size token (e.g. "330ml")
W_PACK = 20       # per matched pack token (e.g. "24 pack")
W_TRUSTED = 20    # bonus if source domain is in TRUSTED_DOMAINS
W_JUNK = -50      # penalty if source domain is in JUNK_DOMAINS

TRUSTED_DOMAINS = {
    "walmart.com",
    "target.com",
    "amazon.com",
    "kroger.com",
    "costco.com",
    "samsclub.com",
    "bjs.com",
    "instacart.com",
    "wholefoodsmarket.com",
    "webstaurantstore.com",
    "officedepot.com",
    "cvs.com",
    "walgreens.com",
    "ebay.com",
}

JUNK_DOMAINS = {
    "pinterest.com",
    "pinimg.com",
    "facebook.com",
    "instagram.com",
    "twitter.com",
    "x.com",
    "tiktok.com",
    "reddit.com",
}

# All search results are sent together to the vision model in one call, in the
# order the search engine returned them. Set to CANDIDATES_PER_PRODUCT so
# nothing is held back; lower it only to cap image tokens per call.
MAX_VERIFY = 10

# Text signals in a candidate's title/snippet/url that suggest it's a mixed/
# variety pack rather than a single-flavour product — penalized unless the
# product's own name says "variety"/"assorted".
VARIETY_PACK_SIGNALS = {"variety pack", "assorted flavors", "assorted flavours", "mixed pack", "variety 12"}
W_VARIETY_MISMATCH = -30  # penalty when product isn't a variety pack but candidate looks like one

# --------------------------------------------------------------------------
# Abbreviation expansion (normalize.py)
# --------------------------------------------------------------------------
ABBREV = {
    "chy": "cherry",
    "strwb": "strawberry",
    "strw": "strawberry",
    "van": "vanilla",
    "choc": "chocolate",
    "chkn": "chicken",
    "bev": "beverage",
    "btl": "bottle",
    "cn": "can",
    "pk": "pack",
    "pkg": "package",
    "ct": "count",
    "oz": "oz",
    "org": "organic",
    "reg": "regular",
    "lg": "large",
    "sm": "small",
    "med": "medium",
    "asst": "assorted",
    "w/": "with",
}

# --------------------------------------------------------------------------
# Verify tiers
# --------------------------------------------------------------------------
AUTO_ACCEPT = 0.80
REVIEW_LOW = 0.50

# Temperature 0 for repeatability — the same product + candidates should give
# the same verdict on a re-run.
VERIFY_TEMPERATURE = 0.0

# Transient-failure retries around the vision call. A 292-row run reliably meets
# 503 "Deadline expired" and local socket exhaustion; without retries a single
# blip turns that product into a permanent `error` row.
VERIFY_MAX_ATTEMPTS = 4
VERIFY_RETRY_BACKOFF = 2.0  # seconds; doubles each attempt (2, 4, 8)

# Per-request deadline for the vision call (milliseconds, google-genai's
# HttpOptions.timeout unit). Without this, a request that never errors --
# just hangs on a dead connection with no response and no exception -- blocks
# the whole run forever: confirmed live on 2026-10-05, twice, each a 3-4 hour
# freeze on a single product with "running" status the only visible symptom.
# The retry loop above only ever helps once something actually raises.
VERIFY_TIMEOUT_MS = 60_000

# Representation tier (verify.py Step 3) mapped to a status ceiling.
#
# All three tiers auto-accept. Tier 3 ("case only") used to be held for review,
# which was wrong for a wholesale catalogue: the buyer is purchasing the case, so
# a clean official photo of it is a perfectly good listing image. That setting
# parked 15 correct, high-confidence photos in the review queue for no reason.
#
# Confidence still gates everything — a weak tier-3 match lands in review on its
# score, the same as any other tier.
TIER_MAX_STATUS = {1: "accepted", 2: "accepted", 3: "accepted"}

# --------------------------------------------------------------------------
# Output image
# --------------------------------------------------------------------------
OUTPUT_SIZE = 1000  # square canvas, px
CANVAS_BACKGROUND = (255, 255, 255)  # white
JPEG_QUALITY = 90

# --------------------------------------------------------------------------
# Background generation + logo watermark (enhance.py)
# --------------------------------------------------------------------------
ENHANCE_ENABLED = os.environ.get("ENHANCE_ENABLED", "false").lower() == "true"

# NOTE: generated backgrounds are not used. Tested across 34 products and the
# result was inconsistent — some backgrounds were good, but many overpowered the
# product or carton they were meant to showcase, and rembg leaves a visible
# fringe on bottles whose label tone is close to the source's white background
# (Snapple Mango Madness is the clearest case). The library ships plain product
# photos on white with a logo watermark instead: `--watermark-only`.
# The code below still works if you want to revisit it.

# Foreground is cut out with rembg BEFORE any generative call and pasted back
# untouched after — the model only ever sees/produces the background.
FOREGROUND_MODEL = "u2net"  # rembg model name

BACKGROUND_STYLE = (
    "a beverage advertisement backdrop, in the style of a printed drinks "
    "campaign: a colourful gradient wash, soft bokeh highlights, gentle "
    "condensation mist and a few water droplets in the air, dynamic but "
    "uncluttered. A clean horizontal surface across the lower third for the "
    "product to stand on, lit from the upper left so the surface picks up a "
    "soft reflection. Vivid and appetising, shallow depth of field. Absolutely "
    "no text, no lettering, no logos, no brand marks, no watermarks"
)

# Per-flavour set dressing appended to BACKGROUND_STYLE. Matched against tokens
# in the product name; the FIRST hit wins, so order matters where names overlap
# ("cran-lime" should read as cranberry, not lime).
#
# Each fragment describes ingredients ARRANGED AROUND THE EDGES and blurred, so
# the real product photo composited on top stays the unambiguous subject.
FLAVOR_BACKGROUND_HINTS = {
    # --- specific compounds first: they contain the simple tokens below ---
    "ruby red": "with grapefruit halves and a splash of pink juice around the edges of the frame, softly blurred",
    "black cherry": "with dark cherries and glossy cherry halves scattered around the edges of the frame, softly blurred",
    "raspberry lime": "with fresh raspberries and lime wedges tumbling around the edges of the frame, softly blurred",
    "cran-lime": "with cranberries and lime wedges scattered around the edges of the frame, softly blurred",
    "cranberry": "with fresh cranberries and a splash of deep red juice around the edges of the frame, softly blurred",
    "kiwi strawberry": "with kiwi slices and strawberries around the edges of the frame, softly blurred",
    "cool blue": "with a cool blue liquid splash and ice shards around the edges of the frame, softly blurred",
    "ginger ale": "with fresh ginger root and citrus slices around the edges of the frame, softly blurred",
    "root beer": "with sassafras bark and a caramel-coloured splash around the edges of the frame, softly blurred",
    "tonic": "with quinine botanicals, juniper berries and a lime wedge around the edges, softly blurred",
    "club soda": "with sparkling bubbles and ice cubes around the edges of the frame, softly blurred",
    "mandarin": "with mandarin orange segments around the edges of the frame, softly blurred",
    # --- simple flavours ---
    "lime": "with fresh lime wedges and a splash of juice around the edges of the frame, softly blurred",
    "lemon": "with fresh lemon wedges and a splash of juice around the edges of the frame, softly blurred",
    "cherry": "with fresh cherries around the edges of the frame, softly blurred",
    "watermelon": "with watermelon slices around the edges of the frame, softly blurred",
    "strawberry": "with fresh strawberries around the edges of the frame, softly blurred",
    "raspberry": "with fresh raspberries around the edges of the frame, softly blurred",
    "blueberry": "with fresh blueberries around the edges of the frame, softly blurred",
    "blackberry": "with fresh blackberries around the edges of the frame, softly blurred",
    "orange": "with orange slices and a splash of juice around the edges of the frame, softly blurred",
    "grapefruit": "with grapefruit slices around the edges of the frame, softly blurred",
    "grape": "with clusters of grapes around the edges of the frame, softly blurred",
    "peach": "with peach slices around the edges of the frame, softly blurred",
    "mango": "with mango slices around the edges of the frame, softly blurred",
    "pineapple": "with pineapple slices around the edges of the frame, softly blurred",
    "coconut": "with coconut halves around the edges of the frame, softly blurred",
    "vanilla": "with vanilla bean pods and a cream swirl around the edges, softly blurred",
    "mint": "with fresh mint leaves around the edges of the frame, softly blurred",
    "apple": "with apple slices around the edges of the frame, softly blurred",
    "cola": "with ice cubes and a dark caramel splash around the edges of the frame, softly blurred",
    "coke": "with ice cubes and a dark caramel splash around the edges of the frame, softly blurred",
    "pepsi": "with ice cubes and a dark cola splash around the edges of the frame, softly blurred",
    "seltzer": "with sparkling bubbles and ice around the edges of the frame, softly blurred",
    "energy": "with an electric colour burst and ice shards around the edges of the frame, softly blurred",
    "coffee": "with roasted coffee beans around the edges of the frame, softly blurred",
    "tea": "with tea leaves and a lemon slice around the edges of the frame, softly blurred",
    "water": "with clear water droplets and ripples around the edges of the frame, softly blurred",
}

# Contact shadow drawn under the product in Pillow AFTER compositing. The
# generative model cannot know where the cutout will land, so the shadow that
# anchors the product to the surface is deterministic.
SHADOW_ENABLED = True
SHADOW_OPACITY = 0.35
SHADOW_BLUR_FRAC = 0.02    # blur radius as a fraction of canvas width
SHADOW_HEIGHT_FRAC = 0.06  # ellipse height as a fraction of the product's height
SHADOW_WIDTH_FRAC = 0.85   # ellipse width as a fraction of the product's width

# How much of the canvas the product occupies. The old code pasted the cutout
# at its own size, which left small sources floating in a large frame.
PRODUCT_SCALE_FRAC = 0.72

ENHANCED_DIR = BASE_DIR / "library_enhanced"

# --------------------------------------------------------------------------
# Logo watermark (deterministic placement — no generative model involved)
# --------------------------------------------------------------------------
LOGO_PATH = DATA_DIR / "logo.png"

# corner  — logo once in a corner (WATERMARK_POSITION), most subtle
# center  — logo once, large, centered over the product
# tiled   — logo repeated on a diagonal lattice across the whole frame
# emboss  — tiled, but rendered as a grey relief so it reads as pressed into
#           the image rather than printed on top (colour is discarded)
WATERMARK_STYLE = "tiled"

WATERMARK_POSITION = "bottom-right"  # bottom-right | bottom-left | top-right | top-left
WATERMARK_MARGIN_FRAC = 0.03   # margin from canvas edge, as a fraction of OUTPUT_SIZE
WATERMARK_WIDTH_FRAC = 0.15    # "corner": logo width as a fraction of OUTPUT_SIZE
WATERMARK_OPACITY = 0.85       # "corner": 0-1

# "center"
WATERMARK_CENTER_WIDTH_FRAC = 0.55
WATERMARK_CENTER_OPACITY = 0.16

# "tiled" / "emboss" — one logo per lattice cell, rotated by ANGLE. GAP is the
# cell pitch as a multiple of the tile's own size, so >1 leaves space between
# repeats. Keep the opacity low: it is applied across the entire image,
# including over the product.
WATERMARK_TILE_WIDTH_FRAC = 0.30
WATERMARK_TILE_OPACITY = 0.05
WATERMARK_TILE_ANGLE = 30      # degrees, counter-clockwise
WATERMARK_TILE_GAP = 1.7
WATERMARK_EMBOSS_OPACITY = 0.22  # "emboss" reads fainter than flat ink, so it runs higher

# --------------------------------------------------------------------------
# Phase 2 (shared knobs; uploader-specific selectors live in oms_uploader.py)
# --------------------------------------------------------------------------
UPLOAD_MANIFEST_PATH = WORK_DIR / "upload_manifest.json"
STORAGE_STATE_PATH = WORK_DIR / "oms_storage_state.json"

# Persistent Chromium profile for the uploader. Logging in writes here and every
# later run reuses it, which keeps the OMS's "remembered device" cookie alive —
# important because a fresh login sends an OTP to an email that can only be
# retrieved by phoning head office. Contains live session cookies: never commit.
BROWSER_PROFILE_DIR = WORK_DIR / "oms_browser_profile"
OMS_URL = os.environ.get("OMS_URL", "")

# The OMS account's email address. Not a credential — Woopsa is passwordless, so
# the emailed one-time code is the only secret, and it cannot be stored. This is
# here purely so `--login` can prefill the form.
OMS_LOGIN_EMAIL = os.environ.get("OMS_LOGIN_EMAIL", "")
UPLOAD_SLEEP_BETWEEN = 1.0
