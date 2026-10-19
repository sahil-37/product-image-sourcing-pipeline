#!/usr/bin/env python
"""Standing safety check: find products that should have a watermarked image
but don't, from any code path -- not just run_source_job.py's own run.

Watermarking is meant to be an automatic, unconditional step for every
accepted/review product with a sourced image (run_source_job.py calls it
right after every accept, unconditionally). If a product is missing its
watermark anyway, that's either a watermarking failure that was silently
swallowed (only printed to a run's log file, never surfaced), or -- as
happened once already, see git history around 2026-09-19 -- state written
by some other path (an ad-hoc debug script, a manual DB fix) that bypassed
the normal sourcing flow entirely and never called watermark_product_image
at all. Either way, "sourced but unbranded" should never sit silently: an
unbranded image reaching the OMS (via Download ZIP or Upload via worker) is
exactly the failure this whole pipeline's two-stage gate elsewhere exists to
prevent for wrong images -- a missing watermark deserves the same treatment.

Usage:
    python check_watermarks.py                  # report only
    python check_watermarks.py --fix             # also backfill from local library/
    python check_watermarks.py --app-url URL     # non-default app (default: localhost:3000)
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE / "src"))


def env(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        sys.exit(f"{name} is not set. export $(grep -v '^#' app/.env.local | xargs)")
    return v


def parse_psql_rows(stdout: str) -> list[dict]:
    """Parse psql -t -A -F'|' output for (id, batch, image_url, name) rows.

    `name` is deliberately the LAST column the query selects, not a middle
    one: it's the only free-text field here (a catalogue description), so
    it's the only one that could ever contain a literal "|" and break a
    fixed-position split. id/batch/image_url are all structurally safe
    (numeric id, a fixed small set of internal batch keys, a controlled
    local path) -- splitting the first three fields off the front and taking
    everything left over as `name` means an embedded "|" in a product name
    is naturally absorbed into name instead of misparsing the row.
    """
    rows = []
    for line in stdout.splitlines():
        if not line.strip():
            continue
        item_id, batch, image_url, name = line.split("|", 3)
        rows.append({"id": item_id, "name": name, "batch": batch, "image_url": image_url})
    return rows


def find_missing_watermarks(app_url: str) -> list[dict]:
    """Products that are accepted/review, have a sourced image, but no
    watermarked one -- queried straight from Postgres, so it catches a gap
    regardless of which code path produced it."""
    import subprocess

    db_url = os.environ.get("DATABASE_URL_UNPOOLED") or os.environ.get("DATABASE_URL")
    if not db_url:
        sys.exit("DATABASE_URL_UNPOOLED (or DATABASE_URL) is not set")
    result = subprocess.run(
        ["psql", db_url, "-t", "-A", "-F", "|", "-c",
         "select id, batch, image_url, name from products "
         "where status in ('accepted','review') "
         "and image_url is not null and enhanced_url is null"],
        capture_output=True, text=True, check=True,
    )
    return parse_psql_rows(result.stdout)


def backfill(rows: list[dict]) -> list[str]:
    """Watermark from the local library/<id>.jpg source. Only fixes products
    whose source photo genuinely exists on disk under the flat library/ dir
    (the current pipeline's own output location) -- a row whose source lives
    somewhere else (an old per-batch library_*/ dir, or nowhere at all) is
    left for push_to_web.py's normal sync to sort out, not guessed at here.
    """
    from lowprice import enhance

    fixed = []
    for r in rows:
        source_path = BASE / "library" / f"{r['id']}.jpg"
        if not source_path.exists():
            print(f"  {r['id']:>8} skipped — no source at {source_path}")
            continue
        enhance.watermark_product_image(r["id"], source_path)
        fixed.append(r["id"])
        print(f"  {r['id']:>8} watermarked")
    return fixed


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--fix", action="store_true", help="backfill from local library/, then re-check")
    ap.add_argument("--app-url", default=os.environ.get("APP_URL", "http://localhost:3000"))
    args = ap.parse_args()

    rows = find_missing_watermarks(args.app_url)
    if not rows:
        print("0 products missing a watermark")
        return

    print(f"{len(rows)} product(s) accepted/review with a sourced image but no watermark:")
    for r in rows:
        print(f"  {r['id']:>8} {r['batch']:<14} {r['name'][:50]}")

    if not args.fix:
        print("\nRun with --fix to backfill from local library/, then sync with push_to_web.py")
        return

    print("\nBackfilling...")
    fixed = backfill(rows)
    if fixed:
        print(f"\n{len(fixed)} watermarked. Now run push_to_web.py to sync enhanced_url to the app.")


if __name__ == "__main__":
    main()
