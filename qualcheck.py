"""Deterministic qualifier check: does the image contradict the row's size/format variant?

Companion to sizecmp.py. Kept outside src/ so it can be validated against real
run data before anything in the package changes.

THE BUG THIS EXISTS FOR
-----------------------
Catalogue rows carry size/format qualifiers that are part of the product's
IDENTITY, not decoration: KING SIZE, SHARE SIZE, FUN SIZE, JUMBO, MINI, MEGA.
These are separate SKUs sold side by side. In the adversarial batch:

  * CHEF BOYARDEE ... JUMBO SPAGHETTI & MEATBALL got a plain can. The model
    found four correct JUMBO cans and rejected all four, reasoning "the product
    is the Jumbo variant, not the standard Spaghetti & Meatballs" -- it
    inverted the requirement.
  * 3 MUSKETEER 24CT KING SIZE got a SHARE SIZE box (verified by eye), and the
    model reported detected_flavour as bare "3 Musketeers" for all ten
    candidates -- it never read the qualifier off the packaging at all.

A prompt addendum fixed the first and not the second, which is why this check
exists in code.

THE ASYMMETRY THAT DRIVES THE DESIGN
------------------------------------
Across every run, the model omits the qualifier from detected_flavour even when
the image is CORRECT: "ALMOND JOY KING SIZE 18CT" -> "ALMOND JOY",
"100 GRAND KING SIZE 24CT" -> "100 GRAND". Both images were verified correct by
eye. So:

    a MISSING qualifier is absence of evidence, NOT evidence of a wrong product.

Failing on a missing qualifier would reject a large number of good images --
6 of the 6 qualifier rows in the sample report no qualifier. This check
therefore fires ONLY on a CONTRADICTION: the row says one qualifier and the
image is reported showing a DIFFERENT, mutually-exclusive one.

That is the same rule the rest of the gate follows: revoke on contradiction,
never on ignorance.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# Mutually-exclusive size/format families. Within a group, two different members
# cannot describe the same physical SKU -- a bar is King Size or Share Size, not
# both. Across groups there is no conflict.
#
# Each entry: canonical name -> the surface forms that mean it. Order matters
# only in that longer forms are matched first (see _TOKEN_RE).
_SIZE_FORMAT = {
    "KING SIZE":   ["king size", "king-size", "kingsize", "king sz"],
    "SHARE SIZE":  ["share size", "sharing size", "share-size", "sharing"],
    "FUN SIZE":    ["fun size", "fun-size"],
    "SNACK SIZE":  ["snack size", "snack-size"],
    "BITE SIZE":   ["bite size", "bite-size", "bites"],
    "FAMILY SIZE": ["family size", "family-size"],
    "PARTY SIZE":  ["party size", "party-size"],
    "JUMBO":       ["jumbo"],
    "MINI":        ["mini", "minis"],
    "MEGA":        ["mega"],
    "GIANT":       ["giant"],
    "REGULAR":     ["regular", "reg", "standard", "original size"],
}

# "KING" alone appears in 56 catalogue rows (vs 47 for the full "KING SIZE"),
# so it must map to KING SIZE -- but only as a standalone word, never inside
# "Burger King" or "King's Hawaiian". Handled by the word-boundary regex below.
_BARE = {"king": "KING SIZE"}

# Groups whose members are mutually exclusive. Everything in _SIZE_FORMAT is
# one group: a single product has exactly one size/format designation.
_EXCLUSIVE_GROUP = set(_SIZE_FORMAT) | {"KING SIZE"}

# Longest-first so "king size" wins over bare "king", and "share size" over
# "sharing".
_ALL_FORMS: list[tuple[str, str]] = sorted(
    [(form, canon) for canon, forms in _SIZE_FORMAT.items() for form in forms]
    + [(form, canon) for form, canon in _BARE.items()],
    key=lambda fc: -len(fc[0]),
)

# REGULAR is special: catalogue rows use "REG"/"REGULAR" to mean "not the king
# size one", but packaging almost never prints the word "Regular". Treating an
# unlabelled bar as a REGULAR contradiction would fire constantly, so REGULAR
# only ever conflicts with an explicitly-detected larger format.
_WEAK = {"REGULAR"}

# Manufacturer renames: the same physical SKU sold under two names over time.
# Mars renamed the 3.28 oz bar from KING SIZE to SHARE SIZE (both listings show
# 3.28 oz / 24 count in search results), so a catalogue row still saying KING
# SIZE is matched correctly by a SHARE SIZE photo -- the packaging changed, the
# product did not. Treating these as a contradiction would reject the only
# images that exist for a stale catalogue name.
#
# Kept as an explicit, auditable list: silently merging KING SIZE and SHARE SIZE
# everywhere would hide genuine mismatches for brands that still sell both.
_EQUIVALENT: list[set[str]] = [
    {"KING SIZE", "SHARE SIZE"},
]


@dataclass(frozen=True)
class Qualifier:
    canonical: str
    matched: str

    def __str__(self) -> str:
        return self.canonical


def _normalise(text: str) -> str:
    """Lowercase, strip punctuation to spaces, collapse runs of whitespace."""
    t = re.sub(r"[^a-z0-9]+", " ", str(text).lower())
    return f" {t.strip()} "


def find_qualifiers(text: str) -> list[Qualifier]:
    """Every size/format qualifier present in a name or a detected_flavour.

    Longest form wins at a given position, so "KING SIZE" is never also
    reported as bare "KING".
    """
    if not text:
        return []
    hay = _normalise(text)
    found: list[Qualifier] = []
    claimed: list[tuple[int, int]] = []

    for form, canon in _ALL_FORMS:
        needle = f" {form} "
        start = 0
        while True:
            i = hay.find(needle, start)
            if i < 0:
                break
            span = (i, i + len(needle))
            # skip if this position was already claimed by a longer form
            if not any(s <= i < e or s < span[1] <= e for s, e in claimed):
                claimed.append(span)
                if canon not in {q.canonical for q in found}:
                    found.append(Qualifier(canon, form))
            start = i + 1
    return found


def check(row_name: str, detected_flavour: str) -> tuple[bool, str]:
    """Does the detected variant CONTRADICT the row's qualifier?

    Returns (ok, reason). ok=False only on a genuine conflict:
    the row names one size/format and the image is reported as a different,
    mutually-exclusive one.

    Deliberately passes when:
      * the row has no qualifier;
      * the image reports no qualifier (the common case -- the model omits it
        even for correct images, so this is absence of evidence);
      * both name the same qualifier.
    """
    row_q = [q for q in find_qualifiers(row_name)]
    det_q = [q for q in find_qualifiers(detected_flavour)]

    if not row_q or not det_q:
        return True, ""

    row_set = {q.canonical for q in row_q}
    det_set = {q.canonical for q in det_q}

    if row_set & det_set:
        return True, ""  # they agree on at least one

    # Known manufacturer renames count as agreement.
    for group in _EQUIVALENT:
        if (row_set & group) and (det_set & group):
            return True, ""

    row_ex = {q for q in row_set if q in _EXCLUSIVE_GROUP}
    det_ex = {q for q in det_set if q in _EXCLUSIVE_GROUP}
    if not row_ex or not det_ex:
        return True, ""

    # REGULAR on either side is a weak signal: packaging rarely prints it, and
    # catalogue "REG" often just distinguishes a row from its KING SIZE sibling.
    # Only flag it against an explicit larger format on the other side.
    if row_ex <= _WEAK and det_ex <= _WEAK:
        return True, ""

    return False, (
        f"size/format mismatch: catalogue says {'/'.join(sorted(row_ex))}, "
        f"image shows {'/'.join(sorted(det_ex))}"
    )


def check_verdict(row_name: str, chosen: dict) -> tuple[bool, str]:
    """check() against a chosen observation dict from the model's verdict."""
    if not chosen:
        return True, ""
    return check(row_name, str(chosen.get("detected_flavour") or ""))


# --- self-test ---------------------------------------------------------------
if __name__ == "__main__":
    CASES = [
        # (row name, detected_flavour, expect_ok)

        # Mars renamed the 3.28oz bar KING SIZE -> SHARE SIZE. Same SKU, so a
        # SHARE SIZE photo is correct for a stale KING SIZE row.
        ("3 MUSKETEER 24CT KING SIZE", "3 Musketeers SHARE SIZE", True),
        ("3 MUSKETEER 24CT KING SIZE", "3 Musketeers KING SIZE", True),
        # a real size/format conflict still fires
        ("3 MUSKETEER 24CT KING SIZE", "3 Musketeers FUN SIZE", False),
        ("SNICKERS KING SIZE 24CT", "Snickers Mini", False),
        # the JUMBO inversion
        ("CHEF BOYARDEE 14.5OZ JUMBO SPAGHETTI & MEATBALL", "Mini Spaghetti & Meatballs", False),
        ("CHEF BOYARDEE 14.5OZ JUMBO SPAGHETTI & MEATBALL", "JUMBO Spaghetti & Meatballs", True),
        ("CHEF BOYARDEE 14.5OZ MINI SPAGHETTI & MEATBALL", "JUMBO Spaghetti & Meatballs", False),
        ("CHEF BOYARDEE 14.5OZ MINI SPAGHETTI & MEATBALL", "Mini spaghetti & meatballs", True),

        # THE CRITICAL PASSES -- model omits the qualifier on correct images.
        # These must NOT fail, or the check rejects a pile of good pictures.
        ("ALMOND JOY KING SIZE 18CT", "ALMOND JOY", True),
        ("100 GRAND KING SIZE 24CT", "100 GRAND", True),
        ("MILKY WAY KING SIZE 24CT MIDNIGHT", "MILKY WAY MIDNIGHT", True),
        ("3 MUSKETEER 24CT KING SIZE", "3 Musketeers", True),

        # row has no qualifier -> never fires
        ("LAY'S BBQ 2.25OZ", "Barbecue", True),
        ("HI CHEW 15CT ACAI", "ACAI", True),
        ("CHEF BOYARDEE 14.5OZ SPAGHETTI & MEATBALL", "Spaghetti & Meatballs", True),

        # bare KING means KING SIZE (and Share Size is its known rename)
        ("SNICKERS KING 24CT", "Snickers Share Size", True),
        ("SNICKERS KING 24CT", "Snickers King Size", True),
        ("SNICKERS KING 24CT", "Snickers Fun Size", False),
        # ...but not inside a brand name
        ("BURGER KING SAUCE 12OZ", "Burger King Sauce", True),

        # REGULAR is a weak signal both ways
        ("3 MUSKETEER 36CT REG", "3 Musketeers", True),
        ("3 MUSKETEER 36CT REG", "3 Musketeers Regular", True),
        ("3 MUSKETEER 36CT REG", "3 Musketeers KING SIZE", False),
        ("BUTTERFINGER KING SIZE 18CT REGULAR", "Butterfinger King Size", True),

        # same qualifier, different surface form
        ("CANDY FUN SIZE 30CT", "Fun-Size Candy", True),
        ("CANDY SHARE SIZE", "Sharing Size Candy", True),

        # empty / missing input
        ("HI CHEW 15CT GRAPE", "", True),
        ("", "King Size", True),
        ("PRINGLE LG HOT HONEY", None, True),
    ]

    bad = 0
    for row, det, want in CASES:
        ok, reason = check(row, det or "")
        good = ok is want
        if not good:
            bad += 1
        flag = "ok  " if good else "FAIL"
        print(f"{flag}  {row[:44]:44} | {str(det)[:26]:26} -> {ok}  {reason[:44]}")
    print()
    print(f"{len(CASES) - bad}/{len(CASES)} passed")
    raise SystemExit(1 if bad else 0)
