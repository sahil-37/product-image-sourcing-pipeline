"""Vision model -> best-match selection among shortlisted candidate images.

This is the ONE place an LLM makes a judgment call in Phase 1. Everything
upstream (search ranking, metadata filtering) is deterministic text scoring.
All MAX_VERIFY shortlisted candidates are sent to the model together in a
single call; the model picks the single best match (or none).

Emulates the manual process: look at the full product description (name +
size + pack, exactly as it appears in the product list), look at the candidate
photos, skip anything watermarked, and visually match flavour / container /
size / pack count. The model reads those attributes off the packaging itself —
they are never pre-extracted by a separate call.

Every decision is forced into an explicit, ordered, per-candidate step
(OBSERVE -> HARD GATES -> RANK -> CHOOSE -> SELF-CONSISTENCY) so a lighter
model can't skip a check or let its reasoning drift from its verdict. Run at
temperature 0 for repeatability.
"""
from __future__ import annotations

import io
import logging
import time
from dataclasses import asdict, dataclass

from google import genai
from google.genai import types
from PIL import Image
from pydantic import BaseModel, Field

from .. import config

log = logging.getLogger("verify")

# Transient conditions worth another attempt: server-side unavailability, rate
# limiting, and local socket exhaustion (Errno 49 / 54) under sustained load.
_RETRYABLE_MARKERS = (
    "503", "502", "500", "504", "429",
    "UNAVAILABLE", "RESOURCE_EXHAUSTED", "DEADLINE",
    "Deadline expired", "timed out", "timeout",
    "Can't assign requested address", "Connection reset", "Connection aborted",
    "Errno 49", "Errno 54", "Errno 104",
)


def _is_retryable(exc: Exception) -> bool:
    """True if `exc` looks transient rather than a permanent rejection.

    Matched on text because google-genai surfaces transport and API errors as
    several unrelated exception types with no shared status attribute.
    """
    text = str(exc)
    return any(marker.lower() in text.lower() for marker in _RETRYABLE_MARKERS)


SYSTEM_PROMPT = """You are a product-image verifier for a wholesale catalogue. \
For each product you are shown several candidate photos and must choose the \
SINGLE best one to show to customers — or NONE, if no candidate correctly \
represents the product.

The catalogue spans every category a wholesaler carries — drinks, candy and \
snacks, tobacco and vapes, household and cleaning goods, personal care, \
general merchandise. Work out what KIND of product each row is before judging \
it, and apply the standards of that category rather than one fixed idea of what \
a product looks like.

Your customers are wholesale buyers deciding what to buy. The chosen image must \
let them see clearly what the product is and what it looks like. A wrong image \
(wrong variant, size, packaging, or pack count) is worse than no image at all. \
When no candidate is correct, return none.

You will receive the product's exact attributes as text:
- name: the full product description exactly as it appears in the catalogue.
        These are terse trade descriptions written for staff, not shoppers, so
        expect abbreviations ("BTL", "CT", "PK", "ASST"), truncated words, a
        trailing vendor code in brackets, and bookkeeping marks like "***".
        Read through that to the actual product.
- unit_size: the size of ONE unit — a volume ("1 LITER"), a weight ("12 OZ"),
        a count ("50 CT"), or blank when size does not apply.
- pack_count: integer number of units. 1 means a single unit.

Read the required variant, packaging, and product type from the name itself. \
Read each candidate's actual variant, packaging, size, and pack count off the \
packaging in the photo.

===================================================================
STEP 1 — OBSERVE each candidate. Record only what the image actually shows.
For candidate i, note:
- detected_flavour       : the flavour/variant printed on the product
- detected_container     : how the product is packaged, in one or two words —
                           bottle, can, carton, box, bag, pouch, jar, tub,
                           tube, blister pack, shrink-wrapped case, loose
                           item, or whatever it actually is. Describe what you
                           see; do not force it into a fixed list.
- detected_unit_size     : the per-unit size printed on the product, or
                           "not legible" if no size can be read. Do not guess
                           a size that isn't shown.
- shows_individual       : true if a single loose unit is clearly visible
- shows_pack             : true if a multipack / case / box is shown
- detected_pack_count    : the number shown ON the pack (read it, e.g.
                           "12 PACK" -> 12). Use 1 if only a single unit is
                           shown. Use null if a pack is shown but the count
                           cannot be read.
- label_legible          : true if the product name / flavour / size can be
                           read on the product or pack
- is_clean               : true if a plain product shot (NOT a lifestyle
                           scene, collage, or comparison grid)
- has_overlay            : true if something was ADDED ON TOP OF the photo that
                           would not be there if you photographed the product
                           yourself. Three kinds, all disqualifying:
                             * watermarks / stock-photo marks / attribution text
                             * marketing callouts composited onto the frame —
                               a "12oz" badge in a coloured circle, a price
                               flash, "NEW", a burst or ribbon graphic
                             * hands, arms, people, or props (glasses, ice,
                               poured drinks, food) staged around the product
                           Text and graphics PRINTED ON the physical packaging
                           are NOT an overlay — that is just what the product
                           looks like. A carton whose own artwork says "12 Cans",
                           "ORIGINAL TASTE", or carries a licensed character or
                           sports logo is clean. So is a retailer's legal
                           disclaimer printed along the carton edge. The test is
                           "added to the image?" not "is there text?"
- is_studio_shot         : true if this is OFFICIAL PRODUCT PHOTOGRAPHY — the
                           kind of image a brand or retailer publishes on a
                           product page. Signs: the product isolated on a plain
                           white or seamless background, even lighting, square
                           on to the camera, sharp, no surroundings visible.
                           False if it looks like a PHOTO SOMEONE TOOK IN A
                           SHOP or at home. Signs of that: a shelf, fridge
                           door, price label, floor, table, wall or ceiling
                           visible; other products around it; a skewed or
                           tilted angle; uneven or yellowish store lighting;
                           glare, shadows or reflections cast across the
                           product; visible depth of field or camera blur.
                           Both kinds can show the correct product — this is
                           about the QUALITY of the photograph, not whether the
                           item is right.

===================================================================
STEP 2 — HARD GATES. A candidate is ELIGIBLE only if ALL of these pass.

Judge each gate the way an experienced buyer would: the question is always
"is this a DIFFERENT product than the catalogue row describes?" — not "is the
photo missing information?" Use your knowledge of how these products are
actually sold. A gate FAILS on a genuine contradiction; it PASSES when the
photo simply doesn't show enough to contradict anything (lower the confidence
instead). Never invent a mismatch out of missing detail.

- flavour_ok   : the strictest gate. "Flavour" here means whatever distinguishes
                 this row from its siblings on the shelf — the variant axis for
                 this kind of product. Depending on the category that may be a
                 flavour, a scent, a colour, a strength or nicotine level, a
                 formulation (regular / diet / zero / sensitive / extra-strength),
                 a material, a shade, or a model.
                   drinks       : Diet vs Zero Sugar, Cherry vs regular
                   candy        : Sour vs Original, Milk vs Dark chocolate
                   tobacco      : Menthol vs Regular, 3MG vs 6MG, Blue vs Gold
                   personal care: Sensitive vs Original, Lavender vs Unscented
                   household    : Lemon vs Fresh Linen, Concentrated vs regular
                 A genuinely different variant is NEVER acceptable, however
                 similar the packaging looks. But a more specific or
                 differently-worded name for the SAME thing is fine — "Mandarin
                 Orange" for an ORANGE row, "Ginger Ale" for a CANADA DRY row,
                 "Deep Cleansing" for a DEEP CLEAN row. If no candidate has the
                 right variant, answer none.
- container_ok : fails only when the packaging genuinely conflicts with how this
                 product is sold at this size. First work out what the row is —
                 a drink, a candy, a cigarette carton, a shampoo, a cleaning
                 product — then ask whether the packaging in the photo is a
                 form that product is actually sold in at that size. Chewing gum
                 comes in a pack, not a bottle; motor oil in a bottle or jug,
                 not a pouch; cigarettes in a pack or carton.
                 If the catalogue name states a container ("BTL", "CAN", "BAG",
                 "CARTON"), the photo must not contradict it. If it states none
                 and the photo shows a normal format for that product and size,
                 PASS. Only a genuine contradiction fails this gate.
- size_ok      : compare detected_unit_size to unit_size as real quantities
                 after converting units, not by text spelling.
                   volume : 1 L = 1000 mL = 33.8 FL OZ; 1.25 L = 42.3 FL OZ
                   weight : 1 LB = 16 OZ = 454 g; 1 KG = 1000 g = 35.3 OZ
                   count  : "50 CT", "100 SHEETS", "24 PIECES" — compare as
                            plain numbers
                 "OZ" is ambiguous and the product decides which it is: on a
                 drink it is fluid ounces (volume), on candy, snacks or soap it
                 is weight. Do not convert across those two — a 12 OZ bag of
                 sweets and a 12 FL OZ can are different measurements that
                 happen to share a word.
                 Some products have no meaningful size at all (a lighter, a
                 greetings card, a single garment); if the row gives no size or
                 the concept doesn't apply, PASS.
                 Product photos very often do not print a legible size — if you
                 cannot read one, PASS this gate and lower the confidence. Fail
                 ONLY when a size is clearly readable and is genuinely a
                 different quantity.
- packcount_ok : the image must not show a pack of the WRONG count.
                 * single unit only (shows_pack = false) -> PASS (a single unit
                   never asserts a pack count).
                 * shows a pack whose detected_pack_count == pack_count -> PASS.
                 * shows a pack whose detected_pack_count != pack_count -> FAIL.
                 * shows a pack with an unreadable count -> PASS but lower the
                   confidence.
- clean_ok     : has_overlay is false. A photo needs to look like the product
                 alone against a plain background — a hand holding the can, a
                 "12oz" marketing badge, or a stock watermark all disqualify it,
                 even when the product itself is exactly right. Judge the
                 photograph, not the packaging: promotional artwork printed on
                 the carton does not fail this gate.

===================================================================
STEP 3 — RANK the eligible candidates. Assign each a representation tier.

ALL THREE TIERS ARE ACCEPTABLE. The tier is a preference order for choosing
between candidates, NOT a quality bar — a tier-3 pack shot of the right product
is a good catalogue image and must never be rejected or marked down merely for
being tier 3. What actually matters is that the item is CORRECT and its
description is READABLE:

  * it is genuinely the product the row describes;
  * the photo is professional product photography, not a shop snapshot;
  * the item name, variant and size can be read in the image.

A candidate meeting those three is publishable at any tier.

- Tier 1: shows the individual unit AND the pack together (and, for a pack
          product, the pack shows the correct count). Best — the customer sees
          the single unit clearly and confirms the pack.
- Tier 2: shows the individual unit clearly (clean single-unit shot).
- Tier 3: shows the pack only, with the product name / variant / size clearly
          legible on it.

Notes:
- For a single-unit product (pack_count = 1), Tier 1 usually does not apply and
  a clean single-unit shot (Tier 2) is the target; any multipack is a wrong
  pack count and was already disqualified in Step 2.
- "Unit" means whatever one sellable item is for this row: one bottle, one
  candy bar, one pack of cigarettes, one bottle of shampoo, one greetings card.
  For products sold as a carton of packs (cigarettes) or a case of packs
  (snacks), the "unit" is the pack and the "pack" is the carton or case.
- Give an ineligible candidate tier 0.

PHOTO QUALITY decides between candidates of the SAME tier, and it matters a
great deal. These images go in a wholesale catalogue, so they must look like
official product photography, not like someone's phone picture of a shelf.

Prefer, in this order:
  1. is_studio_shot true — official photography on a plain background
  2. label_legible true — the name, variant and size can actually be read
  3. the lower tier number (unit + pack beats unit alone beats pack alone)
  4. plainer background, better lighting, product square on to the camera
  5. sharper image

Note the order: photography quality and legibility outrank the tier. A clean
official pack shot (tier 3) beats a blurry shelf photo of a unit and its case
(tier 1), because the buyer can actually read what they are buying.

A studio shot at a LOWER tier beats a shop photo at a higher one. A clean
official single-unit photo (tier 2) is a better catalogue image than a blurry
in-store snap of the unit beside its case (tier 1) — so pick the studio shot
and record the tier you actually chose. Only fall back to a shop photo when it
is the ONLY eligible candidate, and lower the confidence when you do.

===================================================================
STEP 4 — CHOOSE, in strict tier order: the best available tier wins. Take the
eligible candidate with the lowest tier number, breaking ties by the Step-3
preferences.

If ANY candidate is eligible you MUST choose one. best_index = null is only for
the case where NO candidate passed Step 2 at all.

In particular, a missing pack shot is NOT a reason to refuse. If no candidate
shows the unit and pack together (tier 1), a clean single-unit shot (tier 2) is
the correct answer — showing the customer one bottle is better than showing
nothing. Likewise, if only a pack shot exists (tier 3), choose it. Work down the
tiers and take the best one that exists; never skip past an eligible candidate
because a better tier was unavailable.

`confidence` measures how sure you are that this is the RIGHT PRODUCT, clearly
photographed — not which tier it is. Do NOT reduce confidence merely because the
photo shows a single unit rather than a pack, or a pack rather than a unit; all
three tiers are acceptable outcomes.

Lower it for genuine doubt: the variant is hard to make out, the size is not
printed anywhere, the label is partly obscured, the photograph is poor, or the
match rests on an assumption rather than something you can see.

STEP 5 — SELF-CONSISTENCY. Re-read your own Step-1 notes for the candidate you
chose and confirm your Step-2 gates were applied honestly — that flavour,
container and size do not contradict the catalogue row. If they do, that
candidate should not have been eligible: fix its gate flags and choose the next
best eligible candidate instead.

Only set best_index = null here if that leaves nothing eligible. Do NOT null out
a choice merely because it shows a single unit rather than the pack, or because
the size was not legible — those were already settled in Steps 2 and 4 and are
reasons for lower confidence, not refusal."""

USER_PROMPT = """Product to verify:
- name: "{name}"
- unit_size: "{unit_size}"
- pack_count: {pack_count}

You are shown {n} candidate images, labeled candidate_0 through \
candidate_{n_minus_1}, in that order. Follow steps 1-5 and return only the JSON."""


class RequiredAttrs(BaseModel):
    """What the product list says this product must be."""
    flavour: str = Field(description="the flavour/variant required by the product name")
    container: str = Field(description="the container required by the product name")
    unit_size: str = Field(description="the required per-unit size")
    pack_count: int = Field(description="the required number of units")


class CandidateObservation(BaseModel):
    """Step 1 observations + Step 2 gate results + Step 3 tier, per candidate."""
    index: int
    detected_flavour: str
    detected_container: str
    detected_unit_size: str
    shows_individual: bool
    shows_pack: bool
    detected_pack_count: int | None = Field(
        default=None, description="pack count read off the pack; 1 if single unit; null if unreadable"
    )
    label_legible: bool
    is_clean: bool
    has_overlay: bool
    is_studio_shot: bool = Field(
        default=True,
        description="official product photography on a plain background, "
                    "rather than a photo taken in a shop",
    )
    flavour_ok: bool
    container_ok: bool
    size_ok: bool
    packcount_ok: bool
    clean_ok: bool
    eligible: bool
    tier: int = Field(description="1 (best), 2, 3, or 0 if ineligible")
    reason: str = Field(description="one short sentence")


class GeminiVerdict(BaseModel):
    """Strict schema Gemini must fill — passed as response_schema."""
    required: RequiredAttrs
    candidates: list[CandidateObservation]
    best_index: int | None = None
    confidence: float


@dataclass
class Verdict:
    """What the pipeline consumes. `observations` keeps the full per-candidate
    detail for tracing and for the deterministic post-check."""
    best_index: int | None
    confidence: float
    reason: str
    required: dict
    observations: list[dict]
    overridden: bool = False
    override_reason: str = ""

    @property
    def score(self) -> float:
        if self.best_index is None:
            return 0.0
        return self.confidence

    @property
    def chosen(self) -> dict | None:
        """The chosen candidate's observation record, if any."""
        if self.best_index is None:
            return None
        for obs in self.observations:
            if obs.get("index") == self.best_index:
                return obs
        # fall back to positional lookup if the model didn't echo index correctly
        if 0 <= self.best_index < len(self.observations):
            return self.observations[self.best_index]
        return None


class VerifyError(Exception):
    pass


_client: genai.Client | None = None


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        if not config.GEMINI_API_KEY:
            raise VerifyError(
                "GEMINI_API_KEY not set. Use --mock for offline runs, "
                "or configure credentials per README."
            )
        _client = genai.Client(api_key=config.GEMINI_API_KEY)
    return _client


def _jpeg_bytes_to_image(jpeg_bytes: bytes) -> Image.Image:
    img = Image.open(io.BytesIO(jpeg_bytes))
    img.load()
    return img


def _summarize(parsed: GeminiVerdict) -> str:
    """One-line reason for the manifest/review page, drawn from the chosen
    candidate (or from the rejections when nothing was eligible)."""
    if parsed.best_index is not None:
        for c in parsed.candidates:
            if c.index == parsed.best_index:
                return f"tier {c.tier}: {c.reason}"
        return "selected"
    if parsed.candidates:
        return "; ".join(f"c{c.index}: {c.reason}" for c in parsed.candidates[:3])
    return "no eligible candidates"


def select_best(
    product_name: str,
    jpeg_images: list[bytes],
    unit_size: str = "",
    pack_count: int = 1,
    trace_out: dict | None = None,
) -> Verdict:
    """Send all shortlisted candidate images + the product's catalogue
    attributes to Gemini in one call; it picks the single best match (or none).

    If trace_out is given, it's filled in-place with the exact prompts, model,
    raw response, and parsed verdict — for debugging (see trace.py), never for
    control flow."""
    if not jpeg_images:
        raise VerifyError("select_best called with no candidate images")

    client = _get_client()

    user_prompt = USER_PROMPT.format(
        name=product_name,
        unit_size=unit_size,
        pack_count=pack_count,
        n=len(jpeg_images),
        n_minus_1=len(jpeg_images) - 1,
    )

    contents: list = [user_prompt]
    for i, jpeg_bytes in enumerate(jpeg_images):
        contents.append(f"candidate_{i}:")
        contents.append(_jpeg_bytes_to_image(jpeg_bytes))

    if trace_out is not None:
        trace_out["model"] = config.GEMINI_VISION_MODEL
        trace_out["temperature"] = config.VERIFY_TEMPERATURE
        trace_out["system_prompt"] = SYSTEM_PROMPT
        trace_out["user_prompt"] = user_prompt
        trace_out["candidate_count"] = len(jpeg_images)

    # Retry transient failures. A long run reliably meets 503 "Deadline
    # expired" and socket errors from the API side; without this a single blip
    # turns one product into a permanent `error` row.
    request_config = types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT,
        temperature=config.VERIFY_TEMPERATURE,
        response_mime_type="application/json",
        response_schema=GeminiVerdict,
        # Without this, a request that hangs on a dead connection (no
        # response, no exception) blocks forever -- the retry loop below
        # only helps once something actually raises. Confirmed live
        # 2026-10-05: two multi-hour freezes, each on a single product, with
        # "running" status the only visible symptom.
        http_options=types.HttpOptions(timeout=config.VERIFY_TIMEOUT_MS),
    )

    last_error: Exception | None = None
    for attempt in range(config.VERIFY_MAX_ATTEMPTS):
        try:
            response = client.models.generate_content(
                model=config.GEMINI_VISION_MODEL,
                contents=contents,
                config=request_config,
            )
            break
        except Exception as exc:  # google-genai raises assorted API/transport errors
            last_error = exc
            if not _is_retryable(exc) or attempt == config.VERIFY_MAX_ATTEMPTS - 1:
                if trace_out is not None:
                    trace_out["error"] = str(exc)
                raise VerifyError(f"Gemini vision request failed: {exc}") from exc
            delay = config.VERIFY_RETRY_BACKOFF * (2**attempt)
            log.warning("Gemini request failed (attempt %d/%d), retrying in %.1fs: %s",
                        attempt + 1, config.VERIFY_MAX_ATTEMPTS, delay,
                        str(exc).split("\n")[0][:120])
            time.sleep(delay)
    else:  # pragma: no cover - the loop always breaks or raises
        raise VerifyError(f"Gemini vision request failed: {last_error}")

    if trace_out is not None:
        trace_out["raw_response_text"] = response.text
        usage = getattr(response, "usage_metadata", None)
        if usage is not None:
            trace_out["usage"] = {
                "prompt_tokens": getattr(usage, "prompt_token_count", None),
                "output_tokens": getattr(usage, "candidates_token_count", None),
                "thoughts_tokens": getattr(usage, "thoughts_token_count", None),
                "total_tokens": getattr(usage, "total_token_count", None),
            }

    parsed = response.parsed
    if parsed is None:
        raise VerifyError(f"Gemini returned no parsed verdict: {response.text!r}")

    if parsed.best_index is not None and not (0 <= parsed.best_index < len(jpeg_images)):
        raise VerifyError(f"Gemini returned out-of-range best_index: {parsed.best_index!r}")

    verdict = Verdict(
        best_index=parsed.best_index,
        confidence=parsed.confidence,
        reason=_summarize(parsed),
        required=parsed.required.model_dump(),
        observations=[c.model_dump() for c in parsed.candidates],
    )

    if trace_out is not None:
        trace_out["parsed_verdict"] = asdict(verdict)
        trace_out["score"] = verdict.score

    return verdict


def select_best_mock(
    product_name: str,
    jpeg_images: list[bytes],
    unit_size: str = "",
    pack_count: int = 1,
    trace_out: dict | None = None,
) -> Verdict:
    """Fixed offline verdict so the pipeline can run with zero credentials.
    Always picks the first candidate."""
    if not jpeg_images:
        raise VerifyError("select_best_mock called with no candidate images")
    observations = [
        {
            "index": 0,
            "detected_flavour": "mock",
            "detected_container": "bottle",
            "detected_unit_size": unit_size,
            "shows_individual": True,
            "shows_pack": False,
            "detected_pack_count": 1,
            "label_legible": True,
            "is_clean": True,
            "has_overlay": False,
            "is_studio_shot": True,
            "flavour_ok": True,
            "container_ok": True,
            "size_ok": True,
            "packcount_ok": True,
            "clean_ok": True,
            "eligible": True,
            "tier": 2,
            "reason": "mock verdict",
        }
    ]
    verdict = Verdict(
        best_index=0,
        confidence=0.92,
        reason="tier 2: mock verdict",
        required={
            "flavour": "mock",
            "container": "bottle",
            "unit_size": unit_size,
            "pack_count": pack_count,
        },
        observations=observations,
    )
    if trace_out is not None:
        trace_out["model"] = "mock"
        trace_out["temperature"] = config.VERIFY_TEMPERATURE
        trace_out["system_prompt"] = SYSTEM_PROMPT
        trace_out["user_prompt"] = USER_PROMPT.format(
            name=product_name,
            unit_size=unit_size,
            pack_count=pack_count,
            n=len(jpeg_images),
            n_minus_1=len(jpeg_images) - 1,
        )
        trace_out["candidate_count"] = len(jpeg_images)
        trace_out["raw_response_text"] = None
        trace_out["parsed_verdict"] = asdict(verdict)
        trace_out["score"] = verdict.score
    return verdict


def verify(
    product_name: str,
    jpeg_images: list[bytes],
    mock: bool = False,
    unit_size: str = "",
    pack_count: int = 1,
    trace_out: dict | None = None,
) -> Verdict:
    fn = select_best_mock if mock else select_best
    return fn(
        product_name,
        jpeg_images,
        unit_size=unit_size,
        pack_count=pack_count,
        trace_out=trace_out,
    )
