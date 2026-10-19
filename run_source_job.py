#!/usr/bin/env python
"""Run the sourcing pipeline for a specific set of item ids, reporting progress
to a JSON file the app's API route polls.

Invoked by the app (app/api/source/run), not typically run by hand:

    python run_source_job.py <run_id> <id1,id2,...> [--catalogue-file PATH]

Writes work/source_runs/<run_id>.json after every product:
    {"run_id", "total", "done", "current_name", "status", "results": [...]}
"status" is "running", "done", or "error". Never raises past main() -- a
crash is recorded in the status file rather than left for the app to guess at
from a dead process.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE / "src"))

from lowprice import config  # noqa: E402
from lowprice.pipeline import (  # noqa: E402
    load_manifest,
    load_products,
    process_product,
    save_manifest_record,
)

RUNS_DIR = config.WORK_DIR / "source_runs"


def write_status(run_id: str, **fields) -> None:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    path = RUNS_DIR / f"{run_id}.json"
    current = {}
    if path.exists():
        try:
            current = json.loads(path.read_text())
        except json.JSONDecodeError:
            current = {}
    current.update(fields)
    current["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    path.write_text(json.dumps(current, indent=2))


def should_skip_resourcing(prior: dict | None, redo: bool) -> bool:
    """True if an item's prior manifest state should be left alone.

    Only accepted/review are protected: a fresh search can turn up a
    different, possibly worse result for the exact same product (this bit us
    once -- re-running an already-accepted Tide product downgraded it to
    no_match from ordinary search-result variance). no_match/error are the
    only prior states worth automatically retrying, since they mean nothing
    usable was found yet, not that something would be lost. --redo overrides
    unconditionally.
    """
    if redo or not prior:
        return False
    return prior.get("status") in ("accepted", "review")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("run_id")
    ap.add_argument("ids", help="comma-separated item numbers")
    ap.add_argument("--catalogue-file", default=str(config.INPUT_CSV))
    ap.add_argument("--redo", action="store_true")
    args = ap.parse_args()

    run_id = args.run_id
    wanted = {s.strip() for s in args.ids.split(",") if s.strip()}

    write_status(run_id, status="running", total=len(wanted), done=0,
                 current_name="", results=[])

    try:
        all_products = load_products(Path(args.catalogue_file))
        rows = [r for r in all_products if r["id"] in wanted]
        # Rows not found in the catalogue file at all -- report rather than
        # silently drop, since the caller's selection came from the DB, which
        # may include stub rows sourced from a different catalogue export.
        found_ids = {r["id"] for r in rows}
        missing = wanted - found_ids

        manifest = load_manifest(config.MANIFEST_PATH)
        results = []
        for i, row in enumerate(rows, 1):
            item_id, name = row["id"], row["name"]
            write_status(run_id, current_name=name, done=i - 1)

            prior = manifest.get(item_id)
            if should_skip_resourcing(prior, args.redo):
                results.append({"id": item_id, "name": name, "status": prior.get("status"),
                                 "confidence": prior.get("confidence"), "skipped": True})
                write_status(run_id, done=i, results=results)
                continue

            try:
                record = process_product(
                    item_id, name, mock=False,
                    csv_pack=row.get("csv_pack", ""), csv_size=row.get("csv_size", ""),
                )
            except Exception as exc:
                record = {"id": item_id, "name": name, "status": "error",
                          "confidence": 0.0, "source": "", "image": "",
                          "reason": f"unhandled: {exc}"}
            save_manifest_record(config.MANIFEST_PATH, manifest, record)

            # Free, deterministic logo composite -- matches what the older
            # CLI-driven batches shipped with (README: plain photo + watermark,
            # generative background enhancement stays off). Never fails the
            # sourcing result itself; an unbranded image is still reviewable.
            if record.get("status") in ("accepted", "review") and record.get("image"):
                try:
                    from lowprice import enhance
                    enhance.watermark_product_image(
                        item_id, config.BASE_DIR / record["image"]
                    )
                except Exception as exc:
                    print(f"watermark failed for {item_id}: {exc}", file=sys.stderr)

            results.append({"id": item_id, "name": name, "status": record["status"],
                             "confidence": record.get("confidence")})
            write_status(run_id, done=i, results=results)

        for item_id in missing:
            results.append({"id": item_id, "name": "", "status": "not_in_catalogue_file",
                             "confidence": None})

        # len(results) here, not len(rows): `missing` ids were never counted
        # into `done` during the loop (they're not in `rows`), so using
        # len(rows) would leave done permanently short of total -- a "done"
        # status that never reaches 100% for any consumer watching the ratio.
        write_status(run_id, status="done", done=len(results), current_name="", results=results)

        # Sourcing writes to work/manifest.json / library/ (the same files the
        # "drinks" batch entry in push_to_web.py's BATCHES already points at --
        # the label predates this catalogue growing beyond drinks). Sync so a
        # result is reviewable in the app without a separate manual step.
        if any(not r.get("skipped") for r in results):
            sync = subprocess.run(
                [sys.executable, str(BASE / "push_to_web.py"), "--batch", "drinks",
                 "--app-url", os.environ.get("APP_URL", "http://localhost:3000")],
                cwd=BASE, capture_output=True, text=True,
            )
            if sync.returncode != 0:
                write_status(run_id, sync_error=sync.stderr[-500:])
    except BaseException as exc:
        # BaseException, not Exception: load_products()/_read_rows() raise
        # SystemExit on a bad catalogue file (missing column, unreadable
        # workbook, ...), which isn't an Exception subclass and would
        # otherwise slip past this handler, leaving the status file stuck on
        # "running" forever with no error ever recorded -- exactly what this
        # handler exists to prevent.
        write_status(run_id, status="error", error=str(exc))
        raise


if __name__ == "__main__":
    main()
