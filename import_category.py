#!/usr/bin/env python
"""One-time (and re-runnable) import: set category/subcategory on existing
products from a merchandising class export, so /browse can filter by real
category (e.g. "FOOD", "DRINK", "TOBACCO") instead of the pipeline's batch
label (drinks, food_pilot, catalogue, ...), which is a sourcing-run grouping,
not a merchandising one.

The export's header row names 5 columns but every row carries 8 values, so
read_classes() reads by position, not by header name:
    col0 item number         col1 description        col2 Sales Category code
    col3 PRICE CLASS (e.g. "FOOD") -- confirmed this is the real main
         category field despite the name, not a pricing tier
    col4 price-class code    col5 SUB-CATEGORY (e.g. "CHOCOLATE")
    col6 OTP flag            col7 OTP level

Only ever touches category/subcategory on rows that already exist in the
products table (via /api/category/import, a route with no path to any other
column) -- never creates a product, never touches status/image/etc.

    python import_category.py                                  # data/all_products_class.xlsx
    python import_category.py --file data/other_export.xlsx
    python import_category.py --dry-run
"""
from __future__ import annotations

import argparse
import os
import sys
import warnings
from pathlib import Path

import requests

BASE = Path(__file__).resolve().parent


def env(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        sys.exit(f"{name} is not set. export $(grep -v '^#' app/.env.local | xargs)")
    return v


def read_classes(path: Path) -> list[dict]:
    """Read the class export into {id, category, subcategory} rows.

    Positional, not header-driven (see module docstring) -- the printed
    header names only the first 5 of 8 actual columns. Rows with no item
    number, or with a blank Price Class (the real category field -- see
    module docstring), are dropped: a blank category means nothing to set,
    not "clear the category to blank".
    """
    import openpyxl

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb.active
    rows = ws.iter_rows(values_only=True)
    next(rows)  # header -- unreliable column names, read by position instead
    out = []
    for r in rows:
        # openpyxl trims a row's trailing all-empty cells, so a row that's
        # blank from subcategory onward comes back shorter than 6 cells, not
        # padded with None -- pad it ourselves rather than dropping it as if
        # it were malformed.
        if len(r) < 4:
            continue
        r = r + (None,) * (6 - len(r))
        item_id, category, subcategory = r[0], r[3], r[5]
        if item_id is None or not str(item_id).strip():
            continue
        category = str(category).strip() if category else ""
        if not category:
            continue
        out.append({
            "id": str(item_id).strip(),
            "category": category,
            "subcategory": str(subcategory).strip() if subcategory else None,
        })
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--file", default="data/all_products_class.xlsx")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--app-url", default=os.environ.get("APP_URL", "http://localhost:3000"))
    args = ap.parse_args()

    path = BASE / args.file
    if not path.exists():
        sys.exit(f"no such file: {path}")

    rows = read_classes(path)
    print(f"{len(rows)} rows with a category in {path.name}")

    categories = sorted({r["category"] for r in rows})
    print(f"{len(categories)} distinct categories: {', '.join(categories)}")

    if args.dry_run:
        for r in rows[:5]:
            print(f"  {r['id']:>8} {r['category']:<24} {r['subcategory'] or ''}")
        if len(rows) > 5:
            print(f"  … and {len(rows) - 5} more")
        return

    sync_token = env("SYNC_TOKEN")
    url = args.app_url.rstrip("/") + "/api/category/import"
    total_updated = 0
    for start in range(0, len(rows), 500):
        chunk = rows[start : start + 500]
        resp = requests.post(
            url,
            json={"rows": chunk},
            headers={"authorization": f"Bearer {sync_token}"},
            timeout=60,
        )
        if resp.status_code >= 300:
            sys.exit(f"import rejected: {resp.status_code} {resp.text[:300]}")
        data = resp.json()
        total_updated += data.get("updated", 0)
        print(f"  [{min(start + 500, len(rows))}/{len(rows)}] {data}")

    print(f"\ndone — {total_updated} products categorized")


if __name__ == "__main__":
    main()
