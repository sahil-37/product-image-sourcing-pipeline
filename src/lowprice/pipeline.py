"""Orchestrator: CSV -> per-product flow -> manifest (resumable) + CLI.

    python pipeline.py --mock --limit 5   # offline plumbing test, no credentials
    python pipeline.py --limit 20         # real run, first 20 rows
    python pipeline.py                     # full run, auto-resumes
    python pipeline.py --redo              # ignore manifest, reprocess all
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import re
import time
import warnings
from pathlib import Path
from typing import Any

from . import config, tracelog
from .phase1 import gate, process, search, verify
from .phase1.normalize import normalize_name, score_candidate
from .tracelog import Trace

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("pipeline")

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(name: str) -> str:
    slug = _SLUG_RE.sub("-", name.lower()).strip("-")
    return slug or "item"


def _read_rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    """Read a .csv or .xlsx export into (fieldnames, row dicts of strings)."""
    if path.suffix.lower() in (".xlsx", ".xlsm"):
        try:
            import openpyxl
        except ImportError as exc:  # pragma: no cover - dependency is in requirements.txt
            raise SystemExit(
                f"reading {path.name} needs openpyxl — pip install -r requirements.txt"
            ) from exc

        with warnings.catch_warnings():
            # Exports from some systems carry no default style; harmless.
            warnings.simplefilter("ignore", UserWarning)
            workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
        sheet = workbook.active
        row_iter = sheet.iter_rows(values_only=True)
        try:
            header = next(row_iter)
        except StopIteration:
            return [], []
        fieldnames = [str(h).strip() if h is not None else "" for h in header]
        rows = []
        for values in row_iter:
            # Excel keeps numerics as int/float; the rest of the pipeline works
            # in strings, and "12" must not arrive as "12.0".
            cells = []
            for value in values:
                if value is None:
                    cells.append("")
                elif isinstance(value, float) and value.is_integer():
                    cells.append(str(int(value)))
                else:
                    cells.append(str(value))
            # strict=False on purpose: trailing empty cells are common in
            # these exports, and a short row should still yield its columns.
            rows.append(dict(zip(fieldnames, cells, strict=False)))
        workbook.close()
        return fieldnames, rows

    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        return list(reader.fieldnames or []), [dict(row) for row in reader]


def load_products(csv_path: Path) -> list[dict[str, str]]:
    if not csv_path.exists():
        raise SystemExit(f"input file not found: {csv_path}")

    fieldnames, raw_rows = _read_rows(csv_path)
    if config.NAME_COL not in fieldnames:
        raise SystemExit(
            f"{csv_path.name} missing required column '{config.NAME_COL}' "
            f"(found columns: {fieldnames})"
        )

    rows = []
    for row in raw_rows:
        name = (row.get(config.NAME_COL) or "").strip()
        if not name:
            continue
        raw_id = (row.get(config.ID_COL) or "").strip() if config.ID_COL in row else ""
        item_id = raw_id or slugify(name)

        csv_pack = ""
        if config.PACK_COL and config.PACK_COL in row:
            csv_pack = (row.get(config.PACK_COL) or "").strip()

        csv_size = ""
        if config.SIZE_COL and config.SIZE_COL in row:
            csv_size = (row.get(config.SIZE_COL) or "").strip()

        rows.append(
            {"id": item_id, "name": name, "csv_pack": csv_pack, "csv_size": csv_size}
        )
    return rows


def load_manifest(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def save_manifest_record(path: Path, manifest: dict[str, dict[str, Any]], record: dict[str, Any]) -> None:
    manifest[record["id"]] = record
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True))


def process_product(
    item_id: str,
    name: str,
    mock: bool,
    csv_pack: str = "",
    csv_size: str = "",
    verbose: bool = False,
) -> dict[str, Any]:
    """Run the exact per-product flow: normalize -> search -> prefilter -> score
    -> verify -> deterministic gate -> tier -> resize.

    Every stage is recorded to a Trace (always written to
    work/traces/<id>.json; also printed live if verbose=True) so the full
    catalogue-row-to-decision path — including the exact Gemini prompts and raw
    response — can be inspected after any run."""
    base_record = {"id": item_id, "name": name}
    trace = Trace(item_id, name, verbose=verbose)
    pack_count = int(csv_pack) if csv_pack.isdigit() else 1

    try:
        product = normalize_name(name, pack_count=csv_pack, unit_size=csv_size)
        trace.log_stage(
            "normalize",
            original=product.original,
            query=product.query,
            catalogue_unit_size=csv_size,
            catalogue_pack_count=pack_count,
            sizes=product.sizes,
            packs=product.packs,
            tokens=product.tokens,
        )

        try:
            candidates = search.get_candidates(product.query, mock=mock)
        except search.SearchError as exc:
            log.warning("search failed for %r: %s", name, exc)
            trace.log_stage("search", error=str(exc))
            return {**base_record, "status": "error", "confidence": 0.0, "source": "", "image": "", "reason": str(exc)}

        trace.log_stage(
            "search",
            query=product.query,
            candidate_count=len(candidates),
            candidates=[
                {"url": c.url, "source": c.source, "width": c.width, "height": c.height, "title": c.title}
                for c in candidates
            ],
        )

        if not candidates:
            return {**base_record, "status": "no_candidates", "confidence": 0.0, "source": "", "image": "", "reason": "no search results"}

        filtered = process.prefilter(candidates)
        trace.log_stage(
            "prefilter",
            kept_count=len(filtered),
            dropped_count=len(candidates) - len(filtered),
            kept_urls=[c.url for c in filtered],
        )
        if not filtered:
            return {**base_record, "status": "no_candidates", "confidence": 0.0, "source": "", "image": "", "reason": "all candidates filtered (stock-agency domain)"}

        # Every candidate goes to the model, in the order the search engine
        # returned them. Text scoring is recorded for diagnostics but no longer
        # decides anything — the model looks at all the pictures and picks.
        to_verify = filtered[: config.MAX_VERIFY]
        trace.log_stage(
            "score_and_rank",
            ranked=[
                {"url": c.url, "source": c.source, "score": round(score_candidate(product, c), 2)}
                for c in filtered
            ],
            sent_to_gemini=[c.url for c in to_verify],
        )

        downloaded_candidates = []
        jpeg_images = []
        download_failures = []
        for candidate in to_verify:
            try:
                raw = process.download_image(candidate)
                jpeg_images.append(process.to_jpeg_bytes(raw))
                downloaded_candidates.append(candidate)
            except process.ProcessError as exc:
                log.warning("download/convert failed for %r candidate %s: %s", name, candidate.url, exc)
                download_failures.append({"url": candidate.url, "error": str(exc)})
                continue

        trace.log_stage(
            "download",
            downloaded_count=len(jpeg_images),
            failed=download_failures,
        )

        if not jpeg_images:
            return {**base_record, "status": "no_match", "confidence": 0.0, "source": "", "image": "", "reason": "no candidates downloaded successfully"}

        gemini_trace: dict[str, Any] = {}
        try:
            verdict = verify.verify(
                product.original,
                jpeg_images,
                mock=mock,
                unit_size=csv_size,
                pack_count=pack_count,
                trace_out=gemini_trace,
            )
        except verify.VerifyError as exc:
            log.warning("verify failed for %r: %s", name, exc)
            trace.log_stage("gemini_verify", **gemini_trace)
            return {**base_record, "status": "error", "confidence": 0.0, "source": "", "image": "", "reason": str(exc)}

        trace.log_stage("gemini_verify", **gemini_trace)

        # Deterministic re-check of the model's choice against the catalogue's
        # own pack/size fields — the pack and size guarantee must not depend on
        # the model getting it right on any given call.
        gate.apply(verdict, unit_size=csv_size, pack_count=pack_count)
        trace.log_stage(
            "deterministic_gate",
            overridden=verdict.overridden,
            override_reason=verdict.override_reason,
            best_index_after_gate=verdict.best_index,
        )

        if not mock:
            time.sleep(config.SLEEP_BETWEEN)

        if verdict.best_index is None:
            trace.log_stage("decision", status="no_match", reason=verdict.reason or "no verified candidates")
            return {**base_record, "status": "no_match", "confidence": 0.0, "source": "", "image": "", "reason": verdict.reason or "no verified candidates"}

        best_score = verdict.score
        best_candidate = downloaded_candidates[verdict.best_index]
        chosen = verdict.chosen or {}
        tier = chosen.get("tier") or 0

        # Confidence sets the floor; the representation tier caps the ceiling,
        # so a "pack only" shot always gets a human glance.
        if best_score >= config.AUTO_ACCEPT:
            status = "accepted"
        elif best_score >= config.REVIEW_LOW:
            status = "review"
        else:
            trace.log_stage(
                "decision",
                status="no_match",
                best_candidate_url=best_candidate.url,
                score=round(best_score, 3),
                tier=tier,
                reason=verdict.reason or "below review threshold",
            )
            return {
                **base_record,
                "status": "no_match",
                "confidence": round(best_score, 3),
                "source": best_candidate.source,
                "image": "",
                "reason": verdict.reason or "below review threshold",
            }

        tier_ceiling = config.TIER_MAX_STATUS.get(tier)
        if tier_ceiling == "review" and status == "accepted":
            status = "review"

        output_path = config.LIBRARY_DIR / f"{item_id}.jpg"
        try:
            raw = process.download_image(best_candidate)
            process.resize_to_square(raw, output_path)
        except process.ProcessError as exc:
            log.warning("resize failed for %r: %s", name, exc)
            trace.log_stage("decision", status="error", reason=str(exc))
            return {**base_record, "status": "error", "confidence": round(best_score, 3), "source": best_candidate.source, "image": "", "reason": str(exc)}

        trace.log_stage(
            "decision",
            status=status,
            best_candidate_url=best_candidate.url,
            score=round(best_score, 3),
            tier=tier,
            output_path=str(output_path.relative_to(config.BASE_DIR)),
            reason=verdict.reason,
        )

        return {
            "id": item_id,
            "name": name,
            "status": status,
            "confidence": round(best_score, 3),
            "tier": tier,
            "source": best_candidate.source,
            "image": str(output_path.relative_to(config.BASE_DIR)),
            "reason": verdict.reason,
        }
    finally:
        trace.write()


def run(csv_path: Path, mock: bool, limit: int | None, redo: bool, verbose: bool = False,
        ids: set[str] | None = None) -> None:
    products = load_products(csv_path)
    if ids is not None:
        products = [row for row in products if row["id"] in ids]
    if limit:
        products = products[:limit]

    manifest = {} if redo else load_manifest(config.MANIFEST_PATH)

    total = len(products)
    done = 0
    skipped = 0

    for row in products:
        item_id, name = row["id"], row["name"]
        if not redo and item_id in manifest and manifest[item_id].get("status") not in ("error",):
            skipped += 1
            continue

        try:
            record = process_product(
                item_id,
                name,
                mock=mock,
                csv_pack=row.get("csv_pack", ""),
                csv_size=row.get("csv_size", ""),
                verbose=verbose,
            )
        except Exception as exc:  # fail soft per item — never crash the run
            log.exception("unhandled error processing %r", name)
            record = {"id": item_id, "name": name, "status": "error", "confidence": 0.0, "source": "", "image": "", "reason": f"unhandled: {exc}"}

        save_manifest_record(config.MANIFEST_PATH, manifest, record)
        done += 1
        log.info("[%d/%d] %s -> %s (confidence=%.2f)", done + skipped, total, name, record["status"], record["confidence"])

    log.info("run complete: %d processed, %d skipped (already resolved), %d total", done, skipped, total)
    log.info("per-product traces written to %s", tracelog.TRACES_DIR)


def run_enhance(limit: int | None, redo: bool, watermark_only: bool = False) -> None:
    """Phase 1.5: logo watermark, optionally preceded by background regeneration.

    watermark_only=True does just the deterministic logo composite (free, no
    model call). Otherwise the full flow also cuts the product out and
    regenerates the background around it.

    Opt-in and separate from the core manifest-driven run — never modifies
    library/*.jpg, writes to library_enhanced/ instead."""
    # Imported lazily: rembg pulls in onnxruntime, which is slow to load and
    # only needed for --enhance / --watermark-only.
    from . import enhance

    manifest = load_manifest(config.MANIFEST_PATH)
    candidates = [
        (item_id, rec) for item_id, rec in manifest.items()
        if rec.get("status") in ("accepted", "review") and rec.get("image")
    ]
    if limit:
        candidates = candidates[:limit]

    total = len(candidates)
    done = 0
    skipped = 0

    for item_id, rec in candidates:
        output_path = config.ENHANCED_DIR / f"{item_id}.jpg"
        if not redo and output_path.exists():
            skipped += 1
            continue

        source_path = config.BASE_DIR / rec["image"]
        label = "watermark" if watermark_only else "enhance"
        try:
            if watermark_only:
                enhance.watermark_product_image(item_id, source_path)
            else:
                enhance.enhance_product_image(item_id, rec["name"], source_path)
            done += 1
            log.info("[%s %d/%d] %s -> %s", label, done + skipped, total, rec["name"], output_path)
        except enhance.EnhanceError as exc:
            log.warning("%s failed for %r: %s", label, rec["name"], exc)

    log.info("%s complete: %d processed, %d skipped (already done), %d total",
             "watermark" if watermark_only else "enhance", done, skipped, total)


def main() -> None:
    parser = argparse.ArgumentParser(description="Phase 1: build a verified product-image library from a CSV export.")
    parser.add_argument("--mock", action="store_true", help="offline mode: mock search + mock vision, no credentials needed")
    parser.add_argument("--limit", type=int, default=None, help="only process the first N rows")
    parser.add_argument(
        "--ids", type=str, default=None,
        help="comma-separated item numbers to process, ignoring every other row "
             "(e.g. from the app's /browse selection); combine with --redo to "
             "force reprocessing ids that already have a manifest entry",
    )
    parser.add_argument("--redo", action="store_true", help="ignore existing manifest and reprocess everything")
    parser.add_argument("--csv", type=Path, default=config.INPUT_CSV, help="path to the product CSV export")
    parser.add_argument(
        "--enhance", action="store_true",
        help="Phase 1.5 (opt-in, costs money): regenerate backgrounds + apply logo "
             "watermark for accepted/review library images, output to library_enhanced/",
    )
    parser.add_argument(
        "--watermark-only", action="store_true",
        help="Phase 1.5, free: composite the logo onto accepted/review library images "
             "with no segmentation and no generative call, output to library_enhanced/",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="print every stage (normalized query, search candidates, prefilter/scoring, "
             "the exact Gemini prompt + raw response) to stdout as it happens. Always also "
             "written to work/traces/<id>.json regardless of this flag.",
    )
    args = parser.parse_args()

    if args.enhance or args.watermark_only:
        run_enhance(limit=args.limit, redo=args.redo, watermark_only=args.watermark_only)
        return

    ids = {s.strip() for s in args.ids.split(",") if s.strip()} if args.ids else None
    run(csv_path=args.csv, mock=args.mock, limit=args.limit, redo=args.redo, verbose=args.verbose, ids=ids)


if __name__ == "__main__":
    main()
