#!/usr/bin/env python
"""One-time (and re-runnable) import: add every catalogue row NOT YET in the
products table as a status='not_sourced' stub, so /browse can show and select
the full catalogue, not just what the pipeline has already processed.

Reads data/master_products.xlsx by default -- build_master_sheet.py's outer
join of the pricing export (data/all products.xlsx) and the category export
(data/all_products_class.xlsx), so every id either source knows about is
covered. Neither export alone is complete (22 ids only in the pricing file,
78 only in the category file, confirmed 2026-09-19), so import_catalogue.py
switched to the merged sheet rather than either export directly. Re-run
build_master_sheet.py first if either source export changes.

Safe by construction: only pushes rows whose id is absent from `products`.
Never touches an existing row -- re-run any time the catalogue file changes
and it will only add what's new.

    python import_catalogue.py                          # data/master_products.xlsx
    python import_catalogue.py --file data/food.xlsx     # a different catalogue
    python import_catalogue.py --dry-run
"""
from __future__ import annotations

import argparse
import os
import sys
import warnings
from datetime import UTC, datetime
from pathlib import Path

import requests

BASE = Path(__file__).resolve().parent
BATCH_NAME = "catalogue"  # distinct from pipeline batch keys (drinks, food_pilot, ...)


def env(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        sys.exit(f"{name} is not set. export $(grep -v '^#' app/.env.local | xargs)")
    return v


def read_catalogue(path: Path) -> list[dict]:
    """Read a catalogue export's id/name columns.

    Two header shapes are supported: the merged master sheet's own
    ("id", "name" -- build_master_sheet.py's output), and the original
    per-batch exports' ("Item Number", "Description" -- data/food.xlsx and
    friends, still valid as an explicit --file for a one-off catalogue).
    """
    import openpyxl

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb.active
    rows = ws.iter_rows(values_only=True)
    header = [str(c).strip() if c else "" for c in next(rows)]
    id_col = "id" if "id" in header else "Item Number"
    name_col = "name" if "name" in header else "Description"
    out = []
    for r in rows:
        # strict=False: openpyxl trims trailing all-empty cells per row, so a
        # short row is normal (a row ending before the last column happened to
        # have data) rather than a malformed file worth failing the whole
        # import over.
        rec = dict(zip(header, r, strict=False))
        item_id = rec.get(id_col)
        name = rec.get(name_col)
        if item_id is None or not str(item_id).strip():
            continue
        out.append({"id": str(item_id).strip(), "name": str(name or "").strip() or "(no description)"})
    return out


def existing_ids(app_url: str, sync_token: str) -> set[str]:
    """Pull every product id already in the table via a paged query endpoint.

    Reuses /api/sync/run's sibling read path if one exists; otherwise this
    script expects the caller to pass known ids some other way. Since no
    read-all endpoint exists yet, we ask Postgres directly is out of scope for
    this script (it only knows the app's HTTP API) -- so we shell out to psql
    via DATABASE_URL_UNPOOLED instead, which every operator running this
    already has in app/.env.local.
    """
    import subprocess

    db_url = os.environ.get("DATABASE_URL_UNPOOLED") or os.environ.get("DATABASE_URL")
    if not db_url:
        sys.exit("DATABASE_URL_UNPOOLED (or DATABASE_URL) is not set")
    result = subprocess.run(
        ["psql", db_url, "-t", "-A", "-c", "select id from products"],
        capture_output=True, text=True, check=True,
    )
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def not_yet_in_table(catalogue: list[dict], have: set[str]) -> list[dict]:
    """Catalogue rows whose id is absent from the products table.

    Safety property this exists to name: never returns a row whose id IS in
    `have`, so a caller that only ever pushes this function's output can't
    accidentally overwrite an already-sourced product's real data with a
    not_sourced stub.
    """
    return [r for r in catalogue if r["id"] not in have]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--file", default="data/master_products.xlsx")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--app-url", default=os.environ.get("APP_URL", "http://localhost:3000"))
    args = ap.parse_args()

    path = BASE / args.file
    if not path.exists():
        sys.exit(f"no such file: {path}")

    catalogue = read_catalogue(path)
    print(f"{len(catalogue)} rows in {path.name}")

    have = existing_ids(args.app_url, "")
    print(f"{len(have)} products already in the table")

    missing = not_yet_in_table(catalogue, have)
    print(f"{len(missing)} not yet sourced -- would add as stubs")

    if args.dry_run:
        for r in missing[:5]:
            print(f"  {r['id']:>8} {r['name'][:50]}")
        if len(missing) > 5:
            print(f"  … and {len(missing) - 5} more")
        return

    if not missing:
        print("nothing to add")
        return

    sync_token = env("SYNC_TOKEN")
    run_id = "import-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    rows = [
        {
            "id": r["id"],
            "name": r["name"],
            "batch": BATCH_NAME,
            "status": "not_sourced",
            "severity": "unsourced",
            "flags": [],
            "confidence": None,
            "tier": None,
            "source": None,
            "reason": None,
            "cat_size": None,
            "cat_pack": None,
            "det_size": None,
            "det_pack": None,
            "det_flavour": None,
            "det_container": None,
            "source_url": None,
            "image_url": None,
            "enhanced_url": None,
            "thumb_url": None,
            "has_trace": False,
            "run_id": run_id,
        }
        for r in missing
    ]

    url = args.app_url.rstrip("/") + "/api/sync/run"
    for start in range(0, len(rows), 500):
        chunk = rows[start : start + 500]
        resp = requests.post(
            url,
            json={"run_id": run_id, "label": f"catalogue import {run_id}", "rows": chunk},
            headers={"authorization": f"Bearer {sync_token}"},
            timeout=60,
        )
        if resp.status_code >= 300:
            sys.exit(f"import rejected: {resp.status_code} {resp.text[:300]}")
        print(f"  [{min(start + 500, len(rows))}/{len(rows)}] upserted")

    print(f"\ndone — {len(rows)} stub products added, run {run_id}")


if __name__ == "__main__":
    main()
