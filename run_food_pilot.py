#!/usr/bin/env python
"""Food-catalogue pilot runner — throwaway harness, not part of the shipped package.

Redirects every Phase 1 output path to a `*_food_pilot` sibling so a pilot run
can never touch the drinks manifest, library, or traces. Deliberately kept
outside src/ and free of any edits to config.py: the pilot exists to sanity-check
the food catalogue before committing to it, and the drinks pipeline is live.

Every override below is an ABSOLUTE path. pipeline.process_product records the
output as `output_path.relative_to(config.BASE_DIR)`, which raises ValueError on
a relative override — the image gets written, then the record is lost, so the
run silently produces images with no manifest entries.

Usage:
    .venv/bin/python run_food_pilot.py --csv data/food_pilot30.xlsx
    .venv/bin/python run_food_pilot.py --csv data/food_pilot30.xlsx --limit 5 -v
    .venv/bin/python run_food_pilot.py --review     # build the review page only

All other flags (--mock, --limit, --redo, -v) pass straight through to
pipeline.main().
"""
from __future__ import annotations

import sys
from pathlib import Path

from lowprice import config

# Namespace for this run's outputs. --batch NAME gives each batch its own
# manifest/library/traces so an earlier batch's verified results are never
# overwritten by a later one. Parsed by hand here because these paths must be
# set before pipeline is imported.
def _pop_batch(argv: list[str]) -> str:
    """Extract --batch NAME / --batch=NAME from argv, removing it in place."""
    name = "food_pilot"
    i = 0
    while i < len(argv):
        if argv[i] == "--batch" and i + 1 < len(argv):
            name = argv[i + 1]
            del argv[i:i + 2]
            continue
        if argv[i].startswith("--batch="):
            name = argv[i].split("=", 1)[1]
            del argv[i]
            continue
        i += 1
    return name


PILOT = _pop_batch(sys.argv)

# --- Redirect all pilot state. Absolute, and set before pipeline/tracelog import. ---
config.LIBRARY_DIR = config.BASE_DIR / f"library_{PILOT}"
config.MANIFEST_PATH = config.WORK_DIR / f"manifest_{PILOT}.json"
config.REVIEW_HTML_PATH = config.BASE_DIR / f"review_{PILOT}.html"
config.ENHANCED_DIR = config.BASE_DIR / f"library_enhanced_{PILOT}"

config.LIBRARY_DIR.mkdir(parents=True, exist_ok=True)
config.WORK_DIR.mkdir(parents=True, exist_ok=True)

# tracelog binds TRACES_DIR at import time (module-level constant, not a config.X
# lookup), so assigning config.WORK_DIR above would not move it. Override the
# module attribute directly, after import, to keep pilot traces out of the
# shared work/traces/ used by the drinks run.
from lowprice import tracelog  # noqa: E402

tracelog.TRACES_DIR = config.WORK_DIR / f"traces_{PILOT}"
tracelog.TRACES_DIR.mkdir(parents=True, exist_ok=True)


# --- Sliding candidate window -------------------------------------------------
# Stock pipeline: search returns CANDIDATES_PER_PRODUCT (10), prefilter keeps
# them, and `filtered[:MAX_VERIFY]` sends all 10 to download. There is no
# reserve, so every 403 or unreadable URL shrinks the pool the model chooses
# from: 10 candidates minus 2 dead links means Gemini compares 8. If enough die,
# the product falls out as "no candidates downloaded successfully".
#
# Fix without touching src/: over-fetch from Serper, then slide. get_candidates
# already accepts `count`, so ask for OVERFETCH instead of 10. The pipeline
# still slices [:MAX_VERIFY], so the extras sit unused as a reserve — and the
# patched download_image below pulls from that reserve whenever a fetch fails.
#
# Cost is unchanged: MAX_VERIFY still caps how many images reach the model
# (Gemini is the expensive part). Only the Serper page size grows, which is one
# request either way.
OVERFETCH = 20

from lowprice.phase1 import process, search  # noqa: E402

_orig_get_candidates = search.get_candidates
_orig_download_image = process.download_image
_orig_to_jpeg_bytes = process.to_jpeg_bytes

# Reserve candidates for the product currently being processed: the ones that
# were fetched but fell outside the [:MAX_VERIFY] slice.
_reserve: list = []


def _get_candidates(query: str, mock: bool = False, count: int = None):
    """Over-fetch, then stash everything past MAX_VERIFY as the reserve."""
    candidates = _orig_get_candidates(query, mock=mock, count=count or OVERFETCH)

    _reserve.clear()
    # prefilter here too, so the reserve is already free of stock-agency junk
    # and lookaside.* crawler endpoints by the time we substitute from it.
    kept = process.prefilter(candidates)
    _reserve.extend(kept[config.MAX_VERIFY:])
    return candidates


def _adopt(candidate, nxt) -> None:
    """Point `candidate` at `nxt` in place, so the caller's downloaded_candidates
    entry (and the manifest's source/url) reflects what was actually fetched."""
    candidate.url = nxt.url
    candidate.source = nxt.source
    candidate.width, candidate.height = nxt.width, nxt.height
    candidate.title, candidate.snippet = nxt.title, nxt.snippet


def _fetch_usable(candidate):
    """Return JPEG bytes for `candidate`, sliding through the reserve on failure.

    A candidate is only usable if it both downloads AND decodes. The pipeline
    does these as two calls -- process.download_image then process.to_jpeg_bytes
    -- and either can raise ProcessError: a 403 fails the first, a crawler
    endpoint returning HTML (lookaside.fbsbx.com) fails the second. Both must
    slide, so the whole download+decode pair is retried per candidate here.
    """
    try:
        return _orig_to_jpeg_bytes(_orig_download_image(candidate))
    except process.ProcessError as first_exc:
        while _reserve:
            nxt = _reserve.pop(0)
            try:
                jpeg = _orig_to_jpeg_bytes(_orig_download_image(nxt))
            except process.ProcessError:
                continue  # this one's dead too, keep sliding
            _adopt(candidate, nxt)
            print(f"    [slide] {first_exc} -> substituted {nxt.url[:70]}")
            return jpeg
        raise  # reserve exhausted; fail exactly as the stock pipeline would


# The pipeline calls download_image then to_jpeg_bytes as separate steps. We do
# both inside download_image (so the pair can slide together) and make the
# pipeline's own to_jpeg_bytes call a pass-through for what we already decoded.
_JPEG = object()


def _download_image(candidate):
    return (_JPEG, _fetch_usable(candidate))


def _unwrap(raw):
    if isinstance(raw, tuple) and len(raw) == 2 and raw[0] is _JPEG:
        return raw[1]
    return raw


def _to_jpeg_bytes(raw):
    if isinstance(raw, tuple) and len(raw) == 2 and raw[0] is _JPEG:
        return raw[1]
    return _orig_to_jpeg_bytes(raw)


# The final save re-downloads the winning candidate and passes the bytes
# straight to resize_to_square, which Image.open()s them -- so that path must
# unwrap the tuple too, or every save fails on the sentinel.
_orig_resize_to_square = process.resize_to_square


def _resize_to_square(raw, output_path):
    return _orig_resize_to_square(_unwrap(raw), output_path)


search.get_candidates = _get_candidates
process.download_image = _download_image
process.to_jpeg_bytes = _to_jpeg_bytes
process.resize_to_square = _resize_to_square


# --- Robust size equivalence --------------------------------------------------
# gate.sizes_match is volume-only and cannot read fractions, so:
#   * "2 1/4 OZ" parsed as 4 OZ  -> good Lay's image REJECTED (false positive)
#   * "1LB" / "70 G" parsed as None -> size check SILENTLY SKIPPED on 37% of
#     the catalogue, letting wrong-size images through (false negative)
# sizecmp.py replaces it with domain-aware parsing (weight vs volume),
# fraction support, and bare-OZ disambiguation. Validated against all 600
# candidate comparisons in the batch-1/batch-2 traces: 24 improvements,
# 0 regressions. Patched here rather than in gate.py to keep src/ untouched.
import sizecmp  # noqa: E402
from lowprice.phase1 import gate, verify  # noqa: E402

gate.sizes_match = sizecmp.sizes_match
gate.parse_size_ml = lambda t: (lambda s: s.value if s else None)(sizecmp.parse_size(t))


# --- Qualifier tokens are requirements, not deviations ------------------------
# Found by the adversarial batch. For CHEF BOYARDEE 14.5OZ JUMBO SPAGHETTI &
# MEATBALL the model INVERTED the requirement: it read the row as wanting the
# standard product and rejected all four correct JUMBO cans with
#     "The product is the Jumbo variant, not the standard Spaghetti & Meatballs."
# then accepted a plain can. Search had found the right images; the prompt
# mis-parsed WHICH TOKEN was the variant.
#
# The existing flavour_ok text lists variant axes as flavour/scent/strength, so
# a size-or-format qualifier sitting in the name (JUMBO, KING SIZE, MINI, SHARE
# SIZE, FUN SIZE) reads as a modifier of the product rather than part of its
# identity. It is the identity: JUMBO Spaghetti & Meatballs is a different SKU
# from Spaghetti & Meatballs, and so is 3 MUSKETEERS KING SIZE from SHARE SIZE
# (a real miss in the same batch, verified by eye).
#
# Appended rather than rewritten so the gate's existing wording is untouched.
_QUALIFIER_RULE = """

===================================================================
ADDENDUM — QUALIFIER TOKENS IN THE PRODUCT NAME

Some rows carry a qualifier that names a SIZE OR FORMAT VARIANT rather than a
flavour: JUMBO, KING SIZE, SHARE SIZE, FUN SIZE, MINI, SNACK SIZE, BITE SIZE,
FAMILY SIZE, PARTY SIZE, GRAB N GO, LARGE, MEGA, BIG.

Such a qualifier is part of the product's IDENTITY, not a deviation from it.
Treat it exactly as strictly as a flavour, and in the SAME DIRECTION:

  * The row says JUMBO -> the photo MUST show the Jumbo product. A standard
    can/bar/bag is the WRONG product and fails flavour_ok.
  * The row does NOT say JUMBO -> a photo of the Jumbo product is the WRONG
    product and fails flavour_ok.
  * The same applies to KING SIZE vs SHARE SIZE vs FUN SIZE vs regular: these
    are different SKUs sold side by side, not different photographs of one SKU.

Never reason "the product is the Jumbo variant, not the standard one" when the
row itself asked for Jumbo. Read the qualifier off the ROW FIRST, then look for
it on the packaging. If the row has a qualifier and no candidate shows it,
answer none rather than falling back to the unqualified product.

A qualifier the row does not mention and the photo does not show is not a
problem — this rule is about CONTRADICTION in either direction, not about
requiring the packaging to spell out a size the row never asked for.

RECORDING IT. When a qualifier is visible on the packaging, it MUST appear in
detected_flavour, appended to the variant you would otherwise report — write
"3 Musketeers KING SIZE" or "3 Musketeers SHARE SIZE", never bare
"3 Musketeers". These qualifiers are printed prominently on the front of the
pack, so read them off the photo the same way you read the flavour. A
detected_flavour that omits a qualifier visible in the image makes the two SKUs
indistinguishable in your own notes, and the Step-5 self-check cannot then
catch the error.
"""

verify.SYSTEM_PROMPT = verify.SYSTEM_PROMPT + _QUALIFIER_RULE


# --- Deterministic qualifier gate ---------------------------------------------
# The prompt addendum above asks the model to record qualifiers; qualcheck.py
# enforces the comparison in code, so a wrong-format image is revoked
# independently of the model's own judgement -- the same reason gate.py
# re-checks pack and size rather than trusting verify.py.
#
# It fires ONLY on contradiction (row says KING SIZE, image reported as SHARE
# SIZE). A missing qualifier passes: the model omits it even for correct images
# (ALMOND JOY KING SIZE -> "ALMOND JOY", verified correct by eye), so failing
# on absence would reject good pictures. Replayed over all 900 recorded
# observations: 0 false positives.
import qualcheck  # noqa: E402

# gate.apply() is what the pipeline calls, and it takes no product name -- but
# verify's parsed verdict carries the catalogue name in `required`, and the
# pipeline logs it. Simplest reliable seam: record the row name on the Trace as
# each product starts, and read it back here.
_CURRENT: dict = {"name": ""}

_orig_gate_apply = gate.apply


def _gate_apply(verdict, unit_size: str, pack_count: int):
    verdict = _orig_gate_apply(verdict, unit_size=unit_size, pack_count=pack_count)
    if verdict.best_index is None or not verdict.chosen:
        return verdict
    ok, reason = qualcheck.check_verdict(_CURRENT["name"], verdict.chosen)
    if not ok:
        verdict.best_index = None
        verdict.confidence = 0.0
        verdict.overridden = True
        verdict.override_reason = reason
        verdict.reason = f"rejected by deterministic gate: {reason}"
    return verdict


gate.apply = _gate_apply

# Trace(item_id, name, ...) is constructed once per product at the top of
# process_product, which makes it the reliable place to capture the row name.
_orig_trace_init = tracelog.Trace.__init__


def _trace_init(self, item_id, name, *a, **kw):
    _CURRENT["name"] = name
    return _orig_trace_init(self, item_id, name, *a, **kw)


tracelog.Trace.__init__ = _trace_init
# pipeline.py does `from . import process` style module refs for download_image,
# but binds `search.get_candidates` through the module too, so patching the
# module attributes above covers both call sites.


def main() -> None:
    argv = sys.argv[1:]

    # --review: regenerate the pilot review page from the existing manifest.
    # pipeline.main() never writes it; review.main() is a separate entry point.
    if "--review" in argv:
        from lowprice import review

        review.main()
        return

    from lowprice import pipeline

    if not any(a == "--csv" or a.startswith("--csv=") for a in argv):
        argv = ["--csv", str(config.DATA_DIR / "food_pilot30.xlsx"), *argv]

    sys.argv = ["lowprice-source", *argv]
    pipeline.main()

    print()
    print(f"manifest: {config.MANIFEST_PATH}")
    print(f"library:  {config.LIBRARY_DIR}")
    print(f"traces:   {tracelog.TRACES_DIR}")
    print(f"next:     .venv/bin/python {Path(__file__).name} --review")


if __name__ == "__main__":
    main()
