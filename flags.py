"""Derive review flags for a chosen product image.

Companion to sizecmp.py and qualcheck.py. Kept outside src/ so it can be
validated against real run data before anything in the package changes.

WHY THIS EXISTS
---------------
The pipeline already relaxes its checks in the right places -- gate.py only
enforces pack count when the image actually shows a pack, so a single-bar photo
on a 24-count row passes. That is the desired behaviour: the buyer sees the
right product.

But the relaxation is INVISIBLE. Across the first two batches, 13 of 60
products shipped with a pack or size discrepancy and the manifest recorded
nothing: ANDY FRIES BBQ has catalogue pack 12, the image shows 1 unit, status
"accepted", no trace of the difference. A reviewer cannot find these, and a
stakeholder cannot see them at all.

Flags close that gap. They record what was NOT verified, without changing what
ships. Every signal below is already present in the model's own observations --
this derives, it does not call the model again.

SEVERITY
--------
    blunder  wrong product -- must not upload (handled by the gate; a product
             that reaches flagging has already passed it, so this band appears
             here only for rejected records)
    flagged  right product, imperfect photo -- upload and surface for review
    clean    nothing to report

A flag is never a reason to reject. It is a reason to look.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# Flag definitions: code -> (short label, why a reviewer should care).
# Ordered roughly by how much a reviewer would want to see it first.
FLAG_DEFS: dict[str, tuple[str, str]] = {
    "unit_shown_for_pack": (
        "Single unit shown for a multipack row",
        "The catalogue sells this as a pack but the photo shows one loose unit. "
        "Correct product, but the buyer cannot see what they actually receive.",
    ),
    "pack_shown_for_single": (
        "Pack shown for a single-unit row",
        "The catalogue sells one unit but the photo shows a case or multipack.",
    ),
    "pack_count_unreadable": (
        "Pack count not readable",
        "A pack is shown but its count could not be read, so it was never checked "
        "against the catalogue.",
    ),
    "size_unverified": (
        "Size not legible on the packaging",
        "No size could be read off the photo, so the catalogue size was never "
        "confirmed against the image.",
    ),
    "size_not_in_catalogue": (
        "Catalogue has no size to check",
        "The row carries no usable size (blank, or a bare count), so no size "
        "verification was possible from either side.",
    ),
    "not_studio_shot": (
        "Not official product photography",
        "Looks like a shop or phone photo rather than a brand/retailer studio "
        "image. Acceptable, but a better picture likely exists.",
    ),
    "label_not_legible": (
        "Label not readable",
        "The product name, variant or size cannot be read in the image.",
    ),
    "assorted_row": (
        "Assorted/variety row",
        "The row covers mixed flavours, so no single photo is strictly correct. "
        "Worth a human eye on whether the chosen image represents the mix.",
    ),
    "low_confidence": (
        "Model expressed doubt",
        "The verifier's own confidence fell below the clean threshold.",
    ),
    "qualifier_unconfirmed": (
        "Size/format qualifier not confirmed",
        "The row names a format (KING SIZE, JUMBO, MINI) that could not be read "
        "on the packaging, so the two SKUs are indistinguishable from the photo.",
    ),
}

# Confidence at or below this earns a flag. AUTO_ACCEPT is 0.80, so this sits
# just above it: everything that scrapes through acceptance gets looked at.
LOW_CONFIDENCE_AT = 0.90

SEV_CLEAN, SEV_FLAGGED, SEV_BLUNDER = "clean", "flagged", "blunder"


@dataclass
class Assessment:
    severity: str
    flags: list[str] = field(default_factory=list)

    @property
    def details(self) -> list[dict]:
        out = []
        for code in self.flags:
            label, why = FLAG_DEFS.get(code, (code, ""))
            out.append({"code": code, "label": label, "why": why})
        return out


def _is_assorted(name: str) -> bool:
    up = f" {name.upper()} "
    return any(w in up for w in (" ASST ", " ASSORTED ", " VARIETY ", " MIXED "))


def assess(record: dict, observation: dict | None, normalize: dict | None = None) -> Assessment:
    """Derive flags for one manifest record.

    `record`      the manifest entry (status, confidence, tier, name...)
    `observation` the model's notes on the CHOSEN candidate, or None
    `normalize`   the trace's normalize stage (catalogue pack/size), or None

    Everything is read defensively: traces from older runs may lack fields, and
    a missing signal must never invent a flag.
    """
    status = record.get("status")
    if status in ("no_match", "no_candidates", "error"):
        return Assessment(SEV_BLUNDER if status == "no_match" else SEV_CLEAN, [])

    obs = observation or {}
    norm = normalize or {}
    flags: list[str] = []

    cat_pack = norm.get("catalogue_pack_count")
    if not isinstance(cat_pack, int):
        cat_pack = 1
    cat_size = str(norm.get("catalogue_unit_size") or "").strip()

    shows_pack = obs.get("shows_pack")
    det_pack = obs.get("detected_pack_count")

    # Pack presentation. gate.py already rejected a pack whose count genuinely
    # conflicts, so anything reaching here is a presentation difference.
    if cat_pack > 1 and shows_pack is False:
        flags.append("unit_shown_for_pack")
    elif cat_pack == 1 and shows_pack is True:
        flags.append("pack_shown_for_single")
    elif shows_pack is True and det_pack is None:
        flags.append("pack_count_unreadable")

    # Size verification.
    det_size = str(obs.get("detected_unit_size") or "").strip()
    size_illegible = (not det_size) or "not legible" in det_size.lower()
    # A bare count ("1", "15 PC") is not a size the gate can check.
    import sizecmp

    cat_size_usable = sizecmp.parse_size(cat_size) is not None
    if not cat_size_usable:
        flags.append("size_not_in_catalogue")
    elif size_illegible:
        flags.append("size_unverified")

    # Photo quality.
    if obs.get("is_studio_shot") is False:
        flags.append("not_studio_shot")
    if obs.get("label_legible") is False:
        flags.append("label_not_legible")

    # Row-level hazards.
    if _is_assorted(str(record.get("name") or "")):
        flags.append("assorted_row")

    # Size/format qualifier present in the row but not confirmed on the pack.
    import qualcheck

    row_q = qualcheck.find_qualifiers(str(record.get("name") or ""))
    det_q = qualcheck.find_qualifiers(str(obs.get("detected_flavour") or ""))
    if row_q and not det_q:
        flags.append("qualifier_unconfirmed")

    conf = record.get("confidence")
    if isinstance(conf, (int, float)) and conf <= LOW_CONFIDENCE_AT:
        flags.append("low_confidence")

    severity = SEV_FLAGGED if flags else SEV_CLEAN
    if status == "review":
        severity = SEV_FLAGGED
    return Assessment(severity, flags)


def assess_from_trace(record: dict, trace: dict) -> Assessment:
    """assess() using a loaded per-product trace for the observation."""
    pv = (trace.get("gemini_verify") or {}).get("parsed_verdict") or {}
    obs_list = pv.get("observations") or []
    bi = pv.get("best_index")
    obs = obs_list[bi] if isinstance(bi, int) and 0 <= bi < len(obs_list) else None
    return assess(record, obs, trace.get("normalize"))
