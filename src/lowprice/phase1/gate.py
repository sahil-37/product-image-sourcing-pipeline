"""Deterministic post-check on the vision model's choice.

The product list gives us pack_count and unit_size as real fields, so the
pack/size guarantee does not have to depend on the model getting it right on
any given call. After verify.py returns, re-check the chosen candidate's own
reported observations against the catalogue values here, in code, and revoke
the choice on a conflict.

Same principle used elsewhere in the pipeline: deterministic core, LLM only
for the judgment that genuinely needs eyes.
"""
from __future__ import annotations

import re

# Normalized to millilitres. Only volume units appear in this catalogue; add
# rows here rather than special-casing at a call site.
_ML_PER_UNIT = {
    "ml": 1.0,
    "milliliter": 1.0,
    "millilitre": 1.0,
    "l": 1000.0,
    "lit": 1000.0,
    "liter": 1000.0,
    "litre": 1000.0,
    "oz": 29.5735,      # US fluid ounce
    "floz": 29.5735,
    "fl oz": 29.5735,
}

_SIZE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(fl\s*oz|floz|oz|ml|milliliters?|millilitres?|liters?|litres?|lit|l)\b", re.IGNORECASE)

# Sizes within this relative tolerance are treated as equal — covers rounding
# in printed labels (1 L vs 33.8 OZ) and the model's own unit conversion.
SIZE_TOLERANCE = 0.06


def parse_size_ml(text: str) -> float | None:
    """Best-effort '1 LITER' / '12 OZ' / '1.25 LIT' -> millilitres."""
    if not text:
        return None
    match = _SIZE_RE.search(text)
    if not match:
        return None
    value, unit = match.group(1), match.group(2).lower().replace(" ", "")
    per_unit = _ML_PER_UNIT.get(unit)
    if per_unit is None:
        # normalize plurals ("liters" -> "liter")
        per_unit = _ML_PER_UNIT.get(unit.rstrip("s"))
    if per_unit is None:
        return None
    try:
        return float(value) * per_unit
    except ValueError:
        return None


def sizes_match(required: str, detected: str, tolerance: float = SIZE_TOLERANCE) -> bool | None:
    """True/False when both sizes parse, None when the comparison can't be made
    (missing or unparseable input — treated as 'no evidence', not a failure)."""
    req_ml = parse_size_ml(required)
    det_ml = parse_size_ml(detected)
    if req_ml is None or det_ml is None or req_ml <= 0:
        return None
    return abs(req_ml - det_ml) / req_ml <= tolerance


def check(verdict, unit_size: str, pack_count: int) -> tuple[bool, str]:
    """Re-validate verdict.chosen against the catalogue values.

    Returns (ok, reason). ok=False means the choice must be revoked
    (best_index -> None). A check with no evidence to judge on passes — this
    revokes on contradiction, it does not require proof.
    """
    if verdict.best_index is None:
        return True, ""

    chosen = verdict.chosen
    if chosen is None:
        return False, "model returned best_index with no matching candidate record"

    # The model's own gate flags must not contradict its choice. Step 5 of the
    # prompt asks for this; enforcing it here makes it independent of the model.
    for flag in ("flavour_ok", "container_ok", "size_ok", "packcount_ok", "clean_ok"):
        if chosen.get(flag) is False:
            return False, f"chosen candidate failed its own {flag} gate"
    if chosen.get("eligible") is False:
        return False, "chosen candidate was marked ineligible by the model"
    if chosen.get("has_overlay") is True:
        return False, "chosen candidate has a watermark/overlay"

    # Pack count: a pack showing a different count than the catalogue is wrong,
    # regardless of what the model concluded. A single-unit shot asserts no
    # pack count, and an unreadable count is not a contradiction.
    if chosen.get("shows_pack") and chosen.get("detected_pack_count") is not None:
        detected_pack = chosen["detected_pack_count"]
        if isinstance(detected_pack, int) and detected_pack != pack_count:
            return False, (
                f"pack count mismatch: image shows {detected_pack}, catalogue says {pack_count}"
            )

    # Unit size, compared as real quantities after unit conversion.
    size_ok = sizes_match(unit_size, chosen.get("detected_unit_size", ""))
    if size_ok is False:
        return False, (
            f"unit size mismatch: image shows {chosen.get('detected_unit_size')!r}, "
            f"catalogue says {unit_size!r}"
        )

    return True, ""


def apply(verdict, unit_size: str, pack_count: int):
    """Run check() and revoke the choice in place on failure. Returns the
    verdict for convenience."""
    ok, reason = check(verdict, unit_size, pack_count)
    if not ok:
        verdict.best_index = None
        verdict.confidence = 0.0
        verdict.overridden = True
        verdict.override_reason = reason
        verdict.reason = f"rejected by deterministic gate: {reason}"
    return verdict
