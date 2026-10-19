"""name -> search query + tokens, and text-only candidate scoring.

`original` (the join key used for OMS lookup and filenames) is never mutated.
Everything here is deterministic — regex and lookups, no LLM calls.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

from .. import config

_WHITESPACE_RE = re.compile(r"\s+")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9\s./]")
_PAREN_RE = re.compile(r"\(([^)]*)\)")

# e.g. "330ml", "8.4 oz", "1.5l", "12 fl oz"
_SIZE_RE = re.compile(
    r"\b(\d+(?:\.\d+)?)\s?(ml|l|oz|fl oz|g|kg|lb|lbs)\b", re.IGNORECASE
)
# Whether the catalogue's "Size / UOM" cell states a real size, i.e. a number
# followed by a unit. Wider than _SIZE_RE, which only recognises the weight and
# volume units worth extracting as scoring tokens: this also accepts spelled-out
# volumes ("1 LITER") and the count units a food catalogue uses ("18 PC",
# "50 CT"), all of which help a search when appended to the query.
_UOM_RE = re.compile(
    r"\b\d+(?:\.\d+)?\s?"
    r"(ml|l|liter|litre|oz|fl oz|g|kg|lb|lbs|pc|pk|ct|count|piece|pieces|sheet|sheets)\b",
    re.IGNORECASE,
)
# e.g. "24 pack", "x24", "pack of 12", "24ct", "24 count"
_PACK_RE = re.compile(
    r"\b(?:x\s?(\d+)|(\d+)\s?(?:pack|pk|ct|count)|pack of\s?(\d+))\b",
    re.IGNORECASE,
)
# Runs of the operator's own annotation marks: "ORIGINAL*****", "diet***".
_BOOKKEEPING_RE = re.compile(r"[*#]{2,}|\*+\s*$")


@dataclass
class NormalizedProduct:
    original: str
    query: str
    sizes: list[str] = field(default_factory=list)
    packs: list[str] = field(default_factory=list)
    tokens: set[str] = field(default_factory=set)


def _expand_abbreviations(text: str) -> str:
    words = text.split()
    expanded = [config.ABBREV.get(w, w) for w in words]
    return " ".join(expanded)


def _strip_parentheticals(text: str) -> str:
    """Drop any parenthetical text, e.g. "(COKE)" in "CANADA DRY (COKE)" — in
    wholesale exports these mark a distributor/warehouse grouping, not the
    manufacturer, and folding them into the search query risks biasing image
    search toward the wrong brand (Canada Dry is a Dr Pepper brand, not Coke)."""
    return _PAREN_RE.sub(" ", text)


def _strip_bookkeeping_marks(text: str) -> str:
    """Remove the operator's own annotation marks from a search query.

    Catalogue descriptions carry trailing markers that mean something to whoever
    maintains the sheet and nothing to an image search — "ORIGINAL*****",
    "diet***". Sent to Google verbatim they are noise at best; asterisks are
    also wildcard syntax in some search backends.

    Only stripped from the query. The vision model still receives the untouched
    description, because a mark it doesn't recognise is harmless there.
    """
    return _BOOKKEEPING_RE.sub(" ", text)


def _extract_sizes(text: str) -> list[str]:
    return [f"{num}{unit}".lower().replace(" ", "") for num, unit in _SIZE_RE.findall(text)]


def _extract_packs(text: str) -> list[str]:
    packs = []
    for x_form, n_form, of_form in _PACK_RE.findall(text):
        n = x_form or n_form or of_form
        if n:
            packs.append(f"{n}pack")
    return packs


def normalize_name(name: str, pack_count: str = "", unit_size: str = "") -> NormalizedProduct:
    """Build a search query from a product name without touching the original.

    The query stays close to the source wording/casing (parentheticals
    stripped) rather than being fully lowercased/rewritten, e.g.
    "WELCH 16OZ BLUE RASPBERRY 1 LITER". A separate lowercased, tokenized form
    is used for scoring, where case-insensitive matching and abbreviation
    expansion improve accuracy without needing to touch the query itself.
    """
    lowered = name.lower()
    sizes = _extract_sizes(lowered)
    packs = _extract_packs(lowered)
    if pack_count.isdigit():
        pack_token = f"{pack_count}pack"
        if pack_token not in packs:
            packs.append(pack_token)

    query = _strip_parentheticals(name)
    query = _strip_bookkeeping_marks(query)
    query = _WHITESPACE_RE.sub(" ", query).strip()

    # The pack count is deliberately NOT appended to the query.
    #
    # It reads as a helpful hint and is actively harmful: for "1L POLAR SELTZER
    # BLACK CHERRY" appending "12 pack" pushes Google toward 12 x 12 fl oz CAN
    # multipacks, and every returned candidate then fails the container gate.
    # Dropping it puts genuine 1 Litre bottles at the top instead — including
    # "12 pack, 33.8 fl oz bottles", which is both the right unit and the right
    # pack, i.e. the tier-1 result.
    #
    # The pack count still reaches the vision model through the prompt, and is
    # still enforced by the deterministic gate. It just doesn't skew the search.
    #
    # The catalogue SIZE, by contrast, earns its place. Descriptions abbreviate
    # it ("1L", "20OZ") where retailers spell it out, so appending the catalogue
    # form pulls in listings the abbreviation alone misses: for the row above,
    # litre-sized results in the top 6 go from 4 to 6.
    #
    # A bare number in that column is not a size. 420 rows of the food list
    # carry "1", "6" or "24" there — the pack count repeated, or a placeholder.
    # Appended verbatim they turn "AIRHEAD 36CT BLUE RASPBERRY" into
    # "... BLUE RASPBERRY 1", which is noise at best and at worst reads as a
    # quantity. Only a size that names its unit is worth adding.
    if unit_size and _UOM_RE.search(unit_size):
        spelled = _WHITESPACE_RE.sub(" ", unit_size).strip()
        if spelled and spelled.lower() not in query.lower():
            query = f"{query} {spelled}"

    scoring_text = _strip_parentheticals(lowered)
    scoring_text = _NON_ALNUM_RE.sub(" ", scoring_text)
    scoring_text = _WHITESPACE_RE.sub(" ", scoring_text).strip()
    scoring_text = _expand_abbreviations(scoring_text)
    tokens = {t for t in scoring_text.split() if len(t) > 1}
    if pack_count.isdigit():
        tokens.add(pack_count)

    return NormalizedProduct(
        original=name,
        query=query,
        sizes=sizes,
        packs=packs,
        tokens=tokens,
    )


def domain_of(url: str) -> str:
    try:
        netloc = urlparse(url).netloc.lower()
    except ValueError:
        return ""
    return netloc[4:] if netloc.startswith("www.") else netloc


def domain_in_set(domain: str, domain_set: set[str]) -> bool:
    return any(domain == d or domain.endswith("." + d) for d in domain_set)


def score_candidate(product: NormalizedProduct, candidate) -> float:
    """Text-only score, kept for diagnostics in the trace only.

    This score does NOT decide which candidates the model sees — every search
    result is sent. Text overlap turned out to be a bad gatekeeper: it rewards
    verbose titles over accurate ones (a terse "Can Coke 12pk" lost to a longer
    Diet Coke listing), so the judgment now belongs entirely to the model, which
    can actually look at the picture.
    """
    title = candidate.title.lower()
    secondary = f"{candidate.snippet} {candidate.source} {candidate.url}".lower()
    haystack = f"{title} {secondary}"

    score = 0.0

    if product.tokens:
        title_overlap = sum(1 for t in product.tokens if t in title)
        secondary_overlap = sum(
            1 for t in product.tokens if t not in title and t in secondary
        )
        weighted = title_overlap + (secondary_overlap * config.W_SECONDARY_FIELD_FRAC)
        score += (weighted / len(product.tokens)) * config.W_TITLE

    for size in product.sizes:
        if size in haystack.replace(" ", ""):
            score += config.W_SIZE

    for pack in product.packs:
        pack_digits = pack.replace("pack", "")
        if pack in haystack.replace(" ", "") or f"{pack_digits} pack" in haystack:
            score += config.W_PACK

    domain = domain_of(candidate.source or candidate.url)
    if domain_in_set(domain, config.TRUSTED_DOMAINS):
        score += config.W_TRUSTED
    if domain_in_set(domain, config.JUNK_DOMAINS):
        score += config.W_JUNK

    product_wants_variety = any(t in product.tokens for t in ("variety", "assorted", "mixed"))
    if not product_wants_variety and any(sig in haystack for sig in config.VARIETY_PACK_SIGNALS):
        score += config.W_VARIETY_MISMATCH

    return score
