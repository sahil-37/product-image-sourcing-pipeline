#!/usr/bin/env python
"""Upload already-downloaded product images through Woopsa's Bulk Image Upload.

    python bulk_upload_woopsa.py data/product-images-1791259153155 --dry-run
    python bulk_upload_woopsa.py data/product-images-1791259153155 --limit 1
    python bulk_upload_woopsa.py data/product-images-1791259153155 data/batch2-accepted-1791592633806.zip

Each source is a folder or a .zip of "<item number>.jpg" files -- exactly what
/browse's "Download ZIP" produces. Files go up 30 at a time through
Item Maintenance -> Bulk Actions -> Bulk Image, the same dialog an operator
would click through by hand.

HOW THIS DIFFERS FROM worker.py

worker.py opens one product at a time and attaches to its distributor
dropzone: ~10s per image, and it only handles products queued from the app.
This drives the page-level bulk dialog instead: 30 images per request, and
its input is whatever is already on disk. Both write the distributor image
(the stored URL is .../product-images/<Distributor_Name>/<item>-...),
never the shared master one.

WHAT COUNTS AS UPLOADED

The dialog's toast is not trusted. The POST it makes returns a per-file
verdict -- {"success": [{"file", "itemNumber", "img_url"}], "failed": [...]} --
and a file is recorded as uploaded only if it is named in that success list.
Anything else (named in failed, or missing from both) is recorded as failed
and retried on the next run.

Resumable: every chunk's outcome is written to work/bulk_upload_manifest.json
before the next chunk starts, and a re-run skips what already succeeded.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

from lowprice import config
from lowprice.phase2 import oms_selectors as sel
from lowprice.phase2 import oms_uploader as oms

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bulk_upload")

# Both limits are the dialog's own ("at most 30 images. Each file must be
# under 1 MB"). Sending more is rejected client-side before any request.
CHUNK_SIZE = 30
MAX_FILE_BYTES = 1024 * 1024

LEDGER_PATH = config.WORK_DIR / "bulk_upload_manifest.json"
UPLOAD_ENDPOINT = "bulk-upload-item-images"
# A 30-image chunk is ~3MB of multipart; generous so a slow link is not
# mistaken for a failed upload and re-sent.
UPLOAD_TIMEOUT_MS = 180_000

# Only "<digits>.jpg" is an item number. A zip also carries _skipped.txt, and
# one catalogue row's id is a product name -- neither can be matched by the OMS.
ITEM_FILE = re.compile(r"^(\d+)\.(jpe?g|png|webp)$", re.IGNORECASE)

MODAL = ".MuiModal-root"
MODAL_TITLE = "Bulk Image Upload"


# --------------------------------------------------------------------------
# Inputs
# --------------------------------------------------------------------------
def collect(sources: list[str], scratch: Path) -> dict[str, Path]:
    """item number -> image path, across every source. Later sources win."""
    found: dict[str, Path] = {}
    for src in sources:
        p = Path(src)
        if p.is_file() and p.suffix.lower() == ".zip":
            dest = scratch / p.stem
            with zipfile.ZipFile(p) as z:
                z.extractall(dest)
            p = dest
        if not p.is_dir():
            sys.exit(f"{src} is not a folder or a .zip")
        n = 0
        for f in sorted(p.rglob("*")):
            m = ITEM_FILE.match(f.name)
            if f.is_file() and m:
                found[m.group(1)] = f
                n += 1
        log.info("%s: %d image(s)", src, n)
    return found


def load_ledger() -> dict[str, dict]:
    return oms.load_json(LEDGER_PATH)


def save_ledger(ledger: dict[str, dict]) -> None:
    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = LEDGER_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(ledger, indent=2))
    tmp.replace(LEDGER_PATH)  # atomic: a crash mid-write can't corrupt it


# --------------------------------------------------------------------------
# The dialog
# --------------------------------------------------------------------------
def open_dialog(page):
    """Open Bulk Actions -> Bulk Image and return the modal locator."""
    modal = page.locator(MODAL, has_text=MODAL_TITLE)
    if not modal.count():
        page.get_by_text("Bulk Actions", exact=True).first.click()
        page.get_by_text("Bulk Image", exact=True).first.click()
    modal.wait_for(timeout=sel.DEFAULT_TIMEOUT_MS)
    return modal


def close_dialog(page) -> None:
    modal = page.locator(MODAL, has_text=MODAL_TITLE)
    if modal.count():
        modal.get_by_role("button", name="Cancel").click()
        modal.wait_for(state="hidden", timeout=sel.DEFAULT_TIMEOUT_MS)


def upload_chunk(page, files: list[Path], dry_run: bool) -> dict:
    """Send one chunk. Returns the endpoint's `data` object ({} on a dry run)."""
    modal = open_dialog(page)
    # The dialog defaults to "Item number", but it is the mode the whole run
    # depends on -- under "UPC" every file would be matched against barcodes.
    if not modal.locator('input[type=radio][value="item"]').is_checked():
        modal.locator('input[type=radio][value="item"]').check()

    modal.locator("input[type=file]").set_input_files([str(f) for f in files])

    # The counter is the dialog's own view of what it accepted: a file it
    # silently dropped (too large, wrong type) shows up here as a short count.
    want = f"{len(files)} / {CHUNK_SIZE} selected"
    try:
        modal.get_by_text(want, exact=True).wait_for(timeout=sel.DEFAULT_TIMEOUT_MS)
    except Exception as exc:
        shown = modal.get_by_text("selected").first.inner_text()
        close_dialog(page)
        raise RuntimeError(f"dialog shows {shown!r}, expected {want!r}") from exc

    if dry_run:
        close_dialog(page)
        return {}

    with page.expect_response(
        lambda r: UPLOAD_ENDPOINT in r.url and r.request.method == "POST",
        timeout=UPLOAD_TIMEOUT_MS,
    ) as resp:
        modal.get_by_role("button", name="Upload Images").click()

    r = resp.value
    if r.status >= 300:
        raise RuntimeError(f"OMS returned {r.status}: {r.text()[:200]}")
    body = r.json()
    if not body.get("success"):
        raise RuntimeError(f"OMS rejected the chunk: {str(body.get('message'))[:200]}")

    # The dialog closes itself on success; make sure it is gone before the
    # next chunk so its file input starts empty.
    try:
        page.locator(MODAL, has_text=MODAL_TITLE).wait_for(
            state="hidden", timeout=sel.DEFAULT_TIMEOUT_MS)
    except Exception:
        close_dialog(page)
    return body.get("data") or {}


# --------------------------------------------------------------------------
# /browse upload status
# --------------------------------------------------------------------------
def record_in_app(item_ids: list[str]) -> None:
    """Add a done upload_jobs row per product so /browse shows it as uploaded.

    Best effort: the ledger is the source of truth for this script, and a
    database hiccup must not fail an upload that already landed on the OMS.
    """
    url = os.environ.get("DATABASE_URL")
    if not url or not item_ids:
        return
    # item_ids are digits-only (ITEM_FILE), so inlining them is safe.
    ids = ",".join(f"'{i}'" for i in item_ids)
    sql = f"""
        insert into upload_jobs (product_id, image_url, origin, state, outcome)
        select id, coalesce(enhanced_url, image_url, ''), 'bulk_dialog', 'done',
               'uploaded (bulk image dialog)'
        from products where id in ({ids})
    """
    try:
        subprocess.run(["psql", url, "-q", "-c", sql], check=True,
                       capture_output=True, text=True, timeout=60)
    except Exception as exc:
        log.warning("could not record upload status in the app db: %s",
                    str(exc).split("\n")[0][:160])


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def run(sources: list[str], dry_run: bool, limit: int | None, redo: bool,
        headed: bool, no_db: bool, start_at: int | None = None) -> None:
    from playwright.sync_api import sync_playwright

    if not config.OMS_URL:
        sys.exit("OMS_URL is not set. Add it to .env first.")
    if not config.BROWSER_PROFILE_DIR.exists():
        sys.exit("no saved session -- run:  python -m lowprice.phase2.oms_uploader --login")

    ledger = load_ledger()
    with tempfile.TemporaryDirectory(prefix="lowprice-bulk-") as scratch:
        images = collect(sources, Path(scratch))

        todo: list[tuple[str, Path]] = []
        already = too_big = before_start = 0
        for item_id, path in sorted(images.items(), key=lambda kv: int(kv[0])):
            if start_at is not None and int(item_id) < start_at:
                before_start += 1
            elif not redo and ledger.get(item_id, {}).get("status") == "uploaded":
                already += 1
            elif path.stat().st_size >= MAX_FILE_BYTES:
                too_big += 1
                log.warning("%s is %d KB -- over the dialog's 1 MB limit, skipping",
                            path.name, path.stat().st_size // 1024)
            else:
                todo.append((item_id, path))

        chunks = [todo[i:i + CHUNK_SIZE] for i in range(0, len(todo), CHUNK_SIZE)]
        if limit:
            chunks = chunks[:limit]
        if before_start:
            log.info("%d image(s) below item %d left out (--start-at)", before_start, start_at)
        log.info("%d image(s) found: %d already uploaded, %d too large, %d to send in %d chunk(s)%s",
                 len(images), already, too_big, sum(len(c) for c in chunks), len(chunks),
                 "  [DRY RUN]" if dry_run else "")
        if not chunks:
            return

        ok = failed = 0
        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                str(config.BROWSER_PROFILE_DIR), headless=not headed,
                viewport={"width": 1500, "height": 1000},
            )
            context.set_default_timeout(sel.DEFAULT_TIMEOUT_MS)
            page = context.pages[0] if context.pages else context.new_page()
            try:
                page.goto(oms._resolve_url(sel.PRODUCT_SEARCH_URL))
                page.wait_for_timeout(2000)  # let the SPA boot
                if not page.locator(sel.LOGGED_IN_MARKER).count():
                    sys.exit("OMS session expired -- run:  "
                             "python -m lowprice.phase2.oms_uploader --login")

                for n, chunk in enumerate(chunks, 1):
                    ts = time.strftime("%Y-%m-%dT%H:%M:%S")
                    try:
                        data = upload_chunk(page, [path for _, path in chunk], dry_run)
                    except Exception as exc:
                        reason = str(exc).split("\n")[0][:200]
                        log.warning("[chunk %d/%d] FAILED: %s", n, len(chunks), reason)
                        for item_id, path in chunk:
                            ledger[item_id] = {"id": item_id, "file": path.name,
                                               "status": "failed", "outcome": reason, "ts": ts}
                        failed += len(chunk)
                        save_ledger(ledger)
                        # A failed chunk can leave the dialog half-filled or
                        # the page mid-request; start the next one clean.
                        page.goto(oms._resolve_url(sel.PRODUCT_SEARCH_URL))
                        page.wait_for_timeout(2000)
                        continue

                    if dry_run:
                        log.info("[chunk %d/%d] dry run: dialog accepted %d file(s); nothing sent",
                                 n, len(chunks), len(chunk))
                        continue

                    landed = {str(s.get("itemNumber")): s for s in data.get("success") or []}
                    rejected = {Path(str(f.get("file", ""))).stem: f
                                for f in data.get("failed") or []}
                    done_ids = []
                    for item_id, path in chunk:
                        if item_id in landed:
                            ledger[item_id] = {
                                "id": item_id, "file": path.name, "status": "uploaded",
                                "outcome": landed[item_id].get("action", "uploaded"),
                                "img_url": landed[item_id].get("img_url"), "ts": ts}
                            done_ids.append(item_id)
                        else:
                            why = rejected.get(item_id)
                            reason = (json.dumps(why)[:200] if why
                                      else "not named in the OMS response")
                            ledger[item_id] = {"id": item_id, "file": path.name,
                                               "status": "failed", "outcome": reason, "ts": ts}
                            log.warning("  %s -> FAILED: %s", item_id, reason)
                    ok += len(done_ids)
                    failed += len(chunk) - len(done_ids)
                    save_ledger(ledger)  # after every chunk, so a crash resumes
                    if not no_db:
                        record_in_app(done_ids)
                    log.info("[chunk %d/%d] %d uploaded, %d failed", n, len(chunks),
                             len(done_ids), len(chunk) - len(done_ids))
                    time.sleep(config.UPLOAD_SLEEP_BETWEEN)
            finally:
                context.close()

    if dry_run:
        log.info("DRY RUN -- nothing was uploaded.")
    else:
        log.info("done: %d uploaded, %d failed. Ledger: %s", ok, failed, LEDGER_PATH)
        if failed:
            log.info("re-run the same command to retry the failures.")


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Upload downloaded product images via Woopsa's Bulk Image Upload dialog.")
    ap.add_argument("sources", nargs="+",
                    help='folders or .zip files of "<item number>.jpg" images')
    ap.add_argument("--dry-run", action="store_true",
                    help="load each chunk into the dialog, then cancel instead of uploading")
    ap.add_argument("--limit", type=int, default=None, help="only send the first N chunks of 30")
    ap.add_argument("--start-at", type=int, default=None, metavar="ITEM",
                    help="leave out item numbers below this one (images are sent "
                         "in item-number order)")
    ap.add_argument("--redo", action="store_true",
                    help="re-upload images the ledger already marks uploaded")
    ap.add_argument("--headed", action="store_true", help="show the browser (default: headless)")
    ap.add_argument("--no-db", action="store_true",
                    help="do not record upload status for /browse")
    args = ap.parse_args()
    run(args.sources, args.dry_run, args.limit, args.redo, args.headed, args.no_db, args.start_at)


if __name__ == "__main__":
    main()
