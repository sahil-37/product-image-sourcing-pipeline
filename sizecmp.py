"""Robust size parsing and equivalence for catalogue vs. image-detected sizes.

Drop-in replacement for gate.parse_size_ml / gate.sizes_match. Kept outside
src/ so it can be validated against real catalogue data before anything in the
package changes.

Fixes four defects in the current gate:

1. FRACTIONS. `_SIZE_RE` matches the first `\\d+(\\.\\d+)?` followed by a unit,
   so "2 1/4 OZ" matched the "4" and parsed as 4 OZ -- 78% off, far outside any
   tolerance. Lay's prints "2 1/4 OZ" where the catalogue says "2.25 OZ", and a
   correct image was rejected. Both ASCII ("2 1/4") and vulgar ("2()") forms occur
   in model output.

2. WEIGHT UNITS. The unit table is volume-only (ml/l/oz-as-fluid). "1LB",
   "3.5 LBS", "70 G" all parse to None, and None means "no evidence" -- so the
   size check is SKIPPED rather than failed. 608 of 1629 rows (37%) currently
   get no size verification at all. This is the dangerous direction: wrong-size
   images pass silently.

3. OZ AMBIGUITY. In a food catalogue "OZ" is nearly always weight
   (28.3495 g), not US fluid ounce (29.5735 ml). Comparing oz-to-oz the shared
   multiplier cancels, so the old code got lucky -- but "1 LB" vs "16 OZ", or
   "70 G" vs "2.5 OZ", cannot work without a real weight domain. Sizes are
   therefore parsed into a (magnitude, domain) pair and only compared within a
   domain; a cross-domain comparison returns None (no evidence) rather than a
   bogus False.

4. BARE NUMBERS / COUNTS. "1", "16", "15 PC", "2PK", "EACH" carry no unit.
   These are genuinely not sizes and must stay None (no evidence) -- but they
   should be recognised as such deliberately, not fall through a regex miss.

Design rule preserved from gate.py: return None whenever a comparison cannot be
made. The gate revokes on contradiction; it must never revoke on ignorance.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

# --- unit tables, per physical domain ---------------------------------------
# Volume, normalised to millilitres.
_VOLUME = {
    "ml": 1.0,
    "milliliter": 1.0, "millilitre": 1.0,
    "cl": 10.0,
    "dl": 100.0,
    "l": 1000.0, "lit": 1000.0, "liter": 1000.0, "litre": 1000.0,
    "floz": 29.5735, "fluidounce": 29.5735,
    "pt": 473.176, "pint": 473.176,
    "qt": 946.353, "quart": 946.353,
    "gal": 3785.41, "gallon": 3785.41,
}

# Weight, normalised to grams.
_WEIGHT = {
    "g": 1.0, "gr": 1.0, "gram": 1.0, "gramme": 1.0,
    "kg": 1000.0, "kilogram": 1000.0,
    "mg": 0.001,
    "oz": 28.3495,           # avoirdupois -- the food-catalogue default
    "ounce": 28.3495,
    "lb": 453.592, "lbs": 453.592, "pound": 453.592,
}

VOLUME, WEIGHT = "volume", "weight"

# Units that are explicitly counts, not sizes. Recognised so they return a
# clean "not a size" rather than accidentally matching something else.
_COUNT_UNITS = {"pc", "pcs", "piece", "ct", "count", "pk", "pack", "each", "ea", "dz", "dozen"}

# "FL OZ" must win over bare "OZ", so it is listed first in the alternation.
_UNIT_ALTERNATION = (
    r"fl\.?\s*oz\.?|fluid\s*ounces?|"
    r"ml|milliliters?|millilitres?|cl|dl|liters?|litres?|lit\b|l\b|"
    r"pints?|pt\b|quarts?|qt\b|gallons?|gal\b|"
    r"kilograms?|kg|grams?|grammes?|gr\b|g\b|mg|"
    r"ounces?|oz\.?|pounds?|lbs?\b|"
    r"pcs?\b|pieces?|ct\b|counts?|pks?\b|packs?|each|ea\b|dozen|dz\b"
)

_VULGAR = {
    "¼": "1/4", "½": "1/2", "¾": "3/4",
    "⅓": "1/3", "⅔": "2/3",
    "⅛": "1/8", "⅜": "3/8", "⅝": "5/8", "⅞": "7/8",
    "⅕": "1/5", "⅖": "2/5", "⅗": "3/5", "⅘": "4/5",
    "⅙": "1/6", "⅚": "5/6",
}

# number := optional whole part + optional fraction, e.g. "2", "2.25", "2 1/4",
# "1/2", "2()". The whole+fraction form is what the old regex could not see.
_NUM = r"(?:\d+(?:\.\d+)?\s*\d+\s*/\s*\d+|\d+\s*/\s*\d+|\d+(?:\.\d+)?)"

_SIZE_RE = re.compile(
    rf"(?P<num>{_NUM})\s*(?P<unit>{_UNIT_ALTERNATION})",
    re.IGNORECASE,
)

# Sizes within this relative tolerance are equal -- covers label rounding
# (1 L vs 33.8 OZ) and the model's own conversions. Matches gate.SIZE_TOLERANCE.
SIZE_TOLERANCE = 0.06


@dataclass(frozen=True)
class Size:
    """A parsed physical size: magnitude in the domain's base unit."""
    value: float          # ml for volume, g for weight
    domain: str           # VOLUME | WEIGHT

    def __str__(self) -> str:
        return f"{self.value:g}{'ml' if self.domain == VOLUME else 'g'}"


def _expand_vulgar(text: str) -> str:
    """Turn '2()' into '2 1/4' so one number grammar handles both forms.

    A space is inserted before the fraction so '2()' becomes the mixed number
    '2 1/4' rather than the digit-string '21/4'.
    """
    out = []
    for ch in text:
        if ch in _VULGAR:
            if out and out[-1].isdigit():
                out.append(" ")
            out.append(_VULGAR[ch])
        else:
            out.append(ch)
    return "".join(out)


def _parse_number(raw: str) -> float | None:
    """'2', '2.25', '2 1/4', '1/2', '.25' -> float."""
    s = raw.strip()
    # mixed number: whole + fraction
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s+(\d+)\s*/\s*(\d+)", s)
    if m:
        whole, num, den = float(m.group(1)), float(m.group(2)), float(m.group(3))
        return whole + num / den if den else None
    # bare fraction
    m = re.fullmatch(r"(\d+)\s*/\s*(\d+)", s)
    if m:
        num, den = float(m.group(1)), float(m.group(2))
        return num / den if den else None
    try:
        return float(s)
    except ValueError:
        return None


def _normalise_unit(raw: str) -> str:
    u = raw.lower().strip().rstrip(".")
    u = re.sub(r"[\s.]", "", u)
    if u.startswith("fl") or u.startswith("fluid"):
        return "floz"
    return u


def parse_size(text: str) -> Size | None:
    """Parse a size string into a Size, or None when it isn't a physical size.

    None means 'no evidence' -- a bare count ('15 PC'), a bare number ('1'),
    an empty string, or anything unrecognised. Callers must treat None as
    'cannot judge', never as a mismatch.
    """
    if not text:
        return None

    # Expand vulgar fractions BEFORE NFKC. NFKC maps '2()' to '21()4' (U+2044
    # fraction slash), which then reads as the number 21/4 = 5.25 -- silently
    # wrong rather than unparseable. Expanding first avoids that entirely.
    text = _expand_vulgar(str(text))
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("⁄", "/")  # any surviving fraction slash

    for m in _SIZE_RE.finditer(text):
        unit = _normalise_unit(m.group("unit"))
        if unit in _COUNT_UNITS:
            continue  # a count, not a size -- keep looking for a real one
        value = _parse_number(m.group("num"))
        if value is None:
            continue
        if unit in _WEIGHT:
            return Size(value * _WEIGHT[unit], WEIGHT)
        # plural fallback ("liters" -> "liter")
        if unit in _VOLUME:
            return Size(value * _VOLUME[unit], VOLUME)
        singular = unit.rstrip("s")
        if singular in _WEIGHT:
            return Size(value * _WEIGHT[singular], WEIGHT)
        if singular in _VOLUME:
            return Size(value * _VOLUME[singular], VOLUME)
    return None


def _is_ambiguous_oz(text: str) -> bool:
    """True when the size is a bare 'OZ' -- which could be weight or fluid.

    'FL OZ' is unambiguous, as is any gram/pound/millilitre reading. Only bare
    'OZ' has to be guessed, and it is the single most common unit in the file.
    """
    if not text:
        return False
    t = unicodedata.normalize("NFKC", str(text)).lower()
    if re.search(r"fl\.?\s*oz|fluid", t):
        return False
    return bool(re.search(r"\boz\b|\bounces?\b", t))


def sizes_match(required: str, detected: str, tolerance: float = SIZE_TOLERANCE) -> bool | None:
    """True/False when both sizes are comparable; None when they are not.

    Bare 'OZ' is ambiguous: 28.35 g by weight, 29.57 ml by volume. The
    catalogue is mostly food (weight) but also carries oils and drinks
    (volume), and nothing in the row reliably says which. Rather than guess the
    category, when one side is a bare 'OZ' and the other is an unambiguous
    volume, re-read the OZ as fluid ounces and compare -- a 40 OZ oil bottle
    against a '1.18 L' reading is a genuine match, not a domain conflict.

    A conflict is only reported when both sides are unambiguous and disagree.
    """
    req = parse_size(required)
    det = parse_size(detected)
    if req is None or det is None:
        return None
    if req.value <= 0:
        return None

    if req.domain != det.domain:
        # Try reinterpreting an ambiguous bare-OZ side into the other's domain.
        req_amb, det_amb = _is_ambiguous_oz(required), _is_ambiguous_oz(detected)
        if req_amb and not det_amb and det.domain == VOLUME:
            req = Size(req.value / _WEIGHT["oz"] * _VOLUME["floz"], VOLUME)
        elif det_amb and not req_amb and req.domain == VOLUME:
            det = Size(det.value / _WEIGHT["oz"] * _VOLUME["floz"], VOLUME)
        else:
            # A real cross-domain comparison (e.g. grams vs millilitres) needs
            # density to resolve. No evidence -- do not revoke.
            return None

    return abs(req.value - det.value) / req.value <= tolerance


# --- self-test ---------------------------------------------------------------
if __name__ == "__main__":
    # (required, detected, expected)
    CASES = [
        # the bug that started this
        ("2.25 OZ", "2 1/4 OZ", True),
        ("2.25 OZ", "2¼ OZ", True),
        ("2.25 OZ", "2.25 OZ", True),
        # fractions generally
        ("1.5 OZ", "1 1/2 OZ", True),
        ("0.5 OZ", "1/2 OZ", True),
        ("2.25 OZ", "4 OZ", False),      # the old code's wrong answer
        # weight units, previously unparseable -> skipped
        ("1LB", "16 OZ", True),
        ("3.5LB", "56 OZ", True),
        ("2LB", "32 OZ", True),
        ("1LB", "12 OZ", False),
        ("70 G", "2.5 OZ", True),        # 70g == 2.469oz, within 6%
        ("70 G", "70 G", True),
        ("26 OZ", "737 G", True),        # 26oz == 737.1g
        # volume still works
        ("375ML", "375 ML", True),
        ("1 LITER", "33.8 FL OZ", True),
        ("750ML", "25.4 FL OZ", True),
        ("375ML", "750 ML", False),
        # tolerance
        ("13.29 OZ", "13.3 OZ", True),
        ("10 OZ", "11 OZ", False),
        # bare OZ vs an unambiguous volume: reinterpret as fluid oz
        ("40 OZ", "1.18 L", True),        # cooking oil
        ("12 OZ", "355 ML", True),        # 12 fl oz == 355 ml
        ("12 OZ", "500 ML", False),       # reinterpreted, and genuinely wrong
        ("16 FL OZ", "473 ML", True),
        # genuine cross-domain (grams vs millilitres) needs density -> no evidence
        ("70 G", "355 ML", None),
        ("1 LITER", "500 G", None),
        # non-sizes -> no evidence
        ("1", "2.5 OZ", None),
        ("15 PC", "2 OZ", None),
        ("EACH", "5 OZ", None),
        ("2PK", "3 OZ", None),
        ("", "5 OZ", None),
        ("5 OZ", "", None),
        # fl oz must not be read as weight oz
        ("16 FL OZ", "473 ML", True),
        ("16 FL OZ", "1 PINT", True),
    ]
    bad = 0
    for req, det, want in CASES:
        got = sizes_match(req, det)
        ok = got is want
        if not ok:
            bad += 1
        print(f"{'ok ' if ok else 'FAIL'}  {req!r:12} vs {det!r:12} -> {got}  (want {want})")
    print()
    print(f"{len(CASES) - bad}/{len(CASES)} passed")
    raise SystemExit(1 if bad else 0)
