#!/usr/bin/env python
"""Merge the two source-of-truth exports into one master reference sheet.

data/all products.xlsx has Pack/Size/Retail/Price but 22 ids that are
missing from the category export; data/all_products_class.xlsx has
category/subcategory/OTP data (under misleadingly-named columns -- see
import_category.py's docstring) but is missing 78 different ids and no
Pack/Size/Retail/Price at all. Neither file alone is complete; this outer-
joins both on Item Number so every id either file has ends up in one place,
with whichever columns are actually known for it.

Read-only relative to both inputs -- writes a new file, never modifies
either source export. Re-run any time either export is refreshed.

    python build_master_sheet.py
    python build_master_sheet.py --out data/master_products.xlsx
"""
from __future__ import annotations

import argparse
import warnings
from pathlib import Path

BASE = Path(__file__).resolve().parent


def read_catalogue_rows(path: Path) -> dict[str, dict]:
    """data/all products.xlsx: Item Number, Pack, Size / UOM, Description,
    Retail, Price -- keyed by item id."""
    import openpyxl

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb.active
    rows = ws.iter_rows(values_only=True)
    header = [str(c).strip() if c else "" for c in next(rows)]
    out = {}
    for r in rows:
        rec = dict(zip(header, r, strict=False))
        item_id = rec.get("Item Number")
        if item_id is None or not str(item_id).strip():
            continue
        out[str(item_id).strip()] = {
            "name": str(rec.get("Description") or "").strip(),
            "pack": rec.get("Pack"),
            "size": str(rec.get("Size / UOM") or "").strip() or None,
            "retail": rec.get("Retail"),
            "price": rec.get("Price"),
        }
    return out


def read_class_rows(path: Path) -> dict[str, dict]:
    """data/all_products_class.xlsx, read positionally -- see
    import_category.py's docstring for why the header names don't match
    the actual columns. Keyed by item id."""
    import openpyxl

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb.active
    rows = ws.iter_rows(values_only=True)
    next(rows)  # header -- unreliable names, read by position
    out = {}
    for r in rows:
        if len(r) < 4:
            continue
        r = r + (None,) * (8 - len(r))
        item_id, name, category, subcategory, otp_level = r[0], r[1], r[3], r[5], r[7]
        if item_id is None or not str(item_id).strip():
            continue
        out[str(item_id).strip()] = {
            "name": str(name or "").strip(),
            "category": str(category).strip() if category else None,
            "subcategory": str(subcategory).strip() if subcategory else None,
            "otp_level": str(otp_level).strip() if otp_level and str(otp_level).strip() != "N/A" else None,
        }
    return out


def merge_rows(catalogue: dict[str, dict], classes: dict[str, dict]) -> list[dict]:
    """Outer join on item id: every id from EITHER source appears once, with
    whichever fields that source actually had. Neither source's description
    is preferred over the other's -- catalogue's is used when present (it's
    the pipeline's own source of truth for naming), falling back to the
    class export's for the 78 ids the catalogue doesn't have at all.
    """
    all_ids = set(catalogue) | set(classes)
    merged = []
    for item_id in all_ids:
        cat = catalogue.get(item_id, {})
        cls = classes.get(item_id, {})
        merged.append({
            "id": item_id,
            "name": cat.get("name") or cls.get("name") or "",
            "pack": cat.get("pack"),
            "size": cat.get("size"),
            "retail": cat.get("retail"),
            "price": cat.get("price"),
            "category": cls.get("category"),
            "subcategory": cls.get("subcategory"),
            "otp_level": cls.get("otp_level"),
            "in_catalogue_file": item_id in catalogue,
            "in_class_file": item_id in classes,
        })
    # Numeric-first sort so ids that are plain numbers sort as numbers, not
    # lexicographically ("100" before "20") -- falls back to string sort for
    # the rare non-numeric id (e.g. "MEMOBOOK 75 SHEETS").
    def sort_key(r):
        try:
            return (0, int(r["id"]))
        except ValueError:
            return (1, r["id"])
    merged.sort(key=sort_key)
    return merged


def write_master_sheet(rows: list[dict], out_path: Path) -> None:
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Master"
    columns = [
        "id", "name", "pack", "size", "retail", "price",
        "category", "subcategory", "otp_level",
        "in_catalogue_file", "in_class_file",
    ]
    ws.append(columns)
    for r in rows:
        ws.append([r[c] for c in columns])
    out_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out_path)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--catalogue-file", default="data/all products.xlsx")
    ap.add_argument("--class-file", default="data/all_products_class.xlsx")
    ap.add_argument("--out", default="data/master_products.xlsx")
    args = ap.parse_args()

    catalogue_path = BASE / args.catalogue_file
    class_path = BASE / args.class_file
    out_path = BASE / args.out

    catalogue = read_catalogue_rows(catalogue_path)
    classes = read_class_rows(class_path)
    print(f"{len(catalogue)} ids in {catalogue_path.name}")
    print(f"{len(classes)} ids in {class_path.name}")

    both = set(catalogue) & set(classes)
    only_catalogue = set(catalogue) - set(classes)
    only_class = set(classes) - set(catalogue)
    print(f"{len(both)} in both, {len(only_catalogue)} only in {catalogue_path.name}, "
          f"{len(only_class)} only in {class_path.name}")

    rows = merge_rows(catalogue, classes)
    write_master_sheet(rows, out_path)
    print(f"\n{len(rows)} rows written to {out_path}")


if __name__ == "__main__":
    main()
