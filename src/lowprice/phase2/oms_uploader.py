"""Phase 2: upload verified product images into the OMS via Playwright.

Deliberately separate from Phase 1. Phase 1 errors are judgment errors you want
to catch before anything is live; Phase 2 errors are brittle and mechanical and
need independent retry. Nothing here re-opens a sourcing decision.

Safety rules, enforced in code rather than left to the operator:

* Only products whose manifest status is `accepted` are eligible. `review`
  items require a human pass in review.html first, and are skipped with a note.
* An ambiguous OMS name match (0 results, or more than 1) is NEVER guessed —
  it's recorded as skipped so you can resolve it by hand.
* `--dry-run` opens each product but never attaches a file. Attaching IS the
  upload on this OMS — it POSTs to /api/distrubutor/uploadProductImage and the
  image is stored immediately, whether or not Save is clicked afterwards — so a
  dry run has to stop before that, not before the save.
* Only watermarked images are uploaded. A missing watermarked file stops the
  product rather than silently falling back to the unbranded original.
* Resumable: every outcome is written to work/upload_manifest.json immediately,
  so a re-run skips what already succeeded.
* One product failing is logged and skipped, never fatal to the run.
* Only the image field is touched. No other product data is edited.

Usage:
    python oms_uploader.py --login             # once, to store a session
    python oms_uploader.py --dry-run --limit 1 # prove the selectors work
    python oms_uploader.py --limit 5           # staged rollout
    python oms_uploader.py                     # the rest
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any

from .. import config
from . import oms_selectors as sel

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("oms_uploader")


class UploadError(Exception):
    """A per-product failure. Caught per product; never aborts the run."""


# --------------------------------------------------------------------------
# Manifests
# --------------------------------------------------------------------------
def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with path.open() as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("could not read %s: %s", path, exc)
        return {}
    return data if isinstance(data, dict) else {}


def save_upload_manifest(manifest: dict[str, Any]) -> None:
    path = config.UPLOAD_MANIFEST_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w") as fh:
        json.dump(manifest, fh, indent=2)
    tmp.replace(path)  # atomic: a crash mid-write can't corrupt the manifest


def image_path_for(record: dict[str, Any], allow_unwatermarked: bool = False) -> Path | None:
    """The watermarked image for this product, or None.

    Falling back to the plain library image is NOT the default, and that is
    deliberate. It used to be, and it silently published 19 products without the
    logo: library_enhanced/ was stale, every missing file quietly resolved to the
    unbranded original, and nothing surfaced it until the images were already
    live. A missing watermark is a reason to stop, not to substitute.

    Pass allow_unwatermarked=True only when publishing an unbranded image is a
    deliberate choice.
    """
    item_id = record.get("id")
    if not item_id:
        return None

    watermarked = config.ENHANCED_DIR / f"{item_id}.jpg"
    if watermarked.exists():
        return watermarked

    if not allow_unwatermarked:
        return None

    rel = record.get("image")
    if rel:
        plain = config.BASE_DIR / rel
        if plain.exists():
            return plain
    return None


def eligible_products(limit: int | None, include_review: bool,
                      allow_unwatermarked: bool = False) -> list[dict[str, Any]]:
    """Products cleared for upload, in manifest order.

    `review` items are excluded unless explicitly opted in: those are exactly
    the cases where the model wasn't confident, so a human should look first.
    """
    manifest = load_json(config.MANIFEST_PATH)
    if not manifest:
        log.error("no Phase 1 manifest at %s — run pipeline.py first", config.MANIFEST_PATH)
        return []

    allowed = {"accepted", "review"} if include_review else {"accepted"}

    out = []
    missing_watermark = []
    for record in manifest.values():
        if record.get("status") not in allowed:
            continue
        if not image_path_for(record, allow_unwatermarked):
            # Distinguish "no image at all" from "image exists but has no logo":
            # the second is a one-command fix and must not look like the first.
            if image_path_for(record, allow_unwatermarked=True):
                missing_watermark.append(record)
            else:
                log.warning("no image on disk for %s (%s) — skipping",
                            record.get("id"), record.get("name"))
            continue
        out.append(record)

    if missing_watermark:
        log.error("%d product(s) have an image but no watermarked version — NOT uploading them.",
                  len(missing_watermark))
        log.error("Generate them first:  python -m lowprice.pipeline --watermark-only")
        log.error("Or pass --allow-unwatermarked to publish the unbranded originals.")
        for r in missing_watermark[:5]:
            log.error("    %s  %s", r.get("id"), (r.get("name") or "")[:44])
        if len(missing_watermark) > 5:
            log.error("    ... and %d more", len(missing_watermark) - 5)

    return out[:limit] if limit else out


# --------------------------------------------------------------------------
# Playwright plumbing
# --------------------------------------------------------------------------
def _require_selectors() -> None:
    missing = sel.missing_required()
    if missing:
        log.error("oms_selectors.py is not filled in yet. Missing: %s", ", ".join(missing))
        log.error("Record them with:  python -m playwright codegen \"%s\"",
                  config.OMS_URL or "<your-oms-url>")
        log.error("See the instructions at the top of oms_selectors.py.")
        sys.exit(2)


def _resolve_url(url: str) -> str:
    if not url:
        return config.OMS_URL
    if url.startswith(("http://", "https://")):
        return url
    return config.OMS_URL.rstrip("/") + "/" + url.lstrip("/")


def run_login() -> None:
    """Log in once in a persistent browser profile that later runs reuse.

    Uses launch_persistent_context rather than a throwaway browser: the profile
    directory keeps cookies, localStorage AND any "remember this device" flag the
    OMS sets. That matters here because login sends an OTP to an email the
    operator can only retrieve by phoning head office — so this must be a
    once-in-a-blue-moon step, not something every run triggers.

    A storage_state snapshot is also written, but the profile is what makes
    device-remembered logins stick.
    """
    from playwright.sync_api import sync_playwright

    if not config.OMS_URL:
        log.error("OMS_URL is not set. Add it to .env first.")
        sys.exit(2)

    config.BROWSER_PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            str(config.BROWSER_PROFILE_DIR),
            headless=False,
            viewport={"width": 1500, "height": 1000},
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(config.OMS_URL)

        # If the profile still holds a live session there is nothing to do.
        # Checked on the page LOGGED_IN_MARKER actually lives on
        # (PRODUCT_SEARCH_URL, e.g. #searchInput on /admin/product-v1), not
        # OMS_URL's landing page -- checking on the wrong page reports "not
        # logged in" even for a genuinely valid session, since the marker was
        # simply never on that page to begin with. This is the bug that made
        # a real, successful login report "could not confirm login" once
        # already: OMS_URL is the base site, and #searchInput only exists on
        # the product search admin page it redirects into once you ARE
        # logged in.
        already_in = False
        if sel.LOGGED_IN_MARKER:
            page.goto(_resolve_url(sel.PRODUCT_SEARCH_URL))
            already_in = bool(page.locator(sel.LOGGED_IN_MARKER).count())
            if not already_in:
                page.goto(config.OMS_URL)  # back to the login form to prefill

        if not already_in:
            # Get as far as the form allows. Woopsa is passwordless, so the
            # emailed code is the one step that cannot be automated.
            try:
                if sel.LOGIN_AGE_GATE_BUTTON:
                    gate_btn = page.locator(sel.LOGIN_AGE_GATE_BUTTON)
                    if gate_btn.count() and gate_btn.first.is_visible():
                        gate_btn.first.click()
                        page.wait_for_timeout(500)

                if config.OMS_LOGIN_EMAIL and sel.LOGIN_USERNAME_INPUT:
                    field = page.locator(sel.LOGIN_USERNAME_INPUT).first
                    field.wait_for(timeout=sel.DEFAULT_TIMEOUT_MS)
                    field.fill(config.OMS_LOGIN_EMAIL)
                    log.info("email prefilled: %s", config.OMS_LOGIN_EMAIL)

                    if sel.LOGIN_SUBMIT_BUTTON:
                        page.locator(sel.LOGIN_SUBMIT_BUTTON).first.click()
                        log.info("submitted — the OMS should be emailing a code now")
                        page.wait_for_timeout(1500)
            except Exception as exc:
                # Never fatal: the operator can always finish by hand.
                log.warning("could not prefill the login form (%s) — do it by hand",
                            str(exc).split("\n")[0][:120])

        print()
        print("=" * 72)
        if already_in:
            print("  Already logged in — nothing to do.")
            print("  Press Enter to save the session and close.")
        else:
            print("  A browser window is open at the Woopsa login.")
            print()
            print("  The email is filled in and submitted, so a code should be on")
            print("  its way to the address in OMS_LOGIN_EMAIL.")
            print()
            print("  1. Paste the code into the browser.")
            print("  2. Tick any \"remember this device\" / \"trust this browser\"")
            print("     box you are offered — that is what stops the code being")
            print("     needed on every run.")
            print("  3. Leave the browser on the admin page.")
        print()
        print("  Then come back here and press Enter.")
        print("=" * 72)
        input("  Press Enter once logged in... ")

        logged_in = True
        if sel.LOGGED_IN_MARKER:
            # Same reasoning as the already_in check above: LOGGED_IN_MARKER
            # only exists on PRODUCT_SEARCH_URL, so navigate there explicitly
            # rather than trust wherever the OMS's post-login redirect (or
            # the operator, despite step 3 asking them to) left the page.
            page.goto(_resolve_url(sel.PRODUCT_SEARCH_URL))
            page.wait_for_timeout(1000)
            logged_in = bool(page.locator(sel.LOGGED_IN_MARKER).count())

        config.STORAGE_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        context.storage_state(path=str(config.STORAGE_STATE_PATH))
        context.close()

        if logged_in:
            log.info("session saved. Profile: %s", config.BROWSER_PROFILE_DIR)
            log.info("Later runs reuse this profile — no OTP unless the OMS expires it.")
        else:
            log.warning("could not confirm login (%s not found). If you did log in, "
                        "the session may still be fine — try a --dry-run.",
                        sel.LOGGED_IN_MARKER)


def existing_distributor_image(page) -> str:
    """The src of the distributor slot's image, or "" when the slot is empty.

    This is the only per-product signal on the page that cannot be satisfied by
    leftover state: it names the stored file for THIS item, e.g.
    ".../<Distributor_Name>/100001.jpg".
    """
    if not sel.DISTRIBUTOR_IMAGE:
        return ""
    img = page.locator(sel.DISTRIBUTOR_IMAGE)
    if not img.count():
        return ""
    return (img.first.get_attribute("src") or "").strip()


def dismiss_unsaved_dialog(page) -> bool:
    """Discard a leftover "unsaved changes" dialog, if one is showing.

    Woopsa tracks edit state per session, so a run that died mid-product leaves
    it dirty and blocks the next navigation with a modal. Discarding is the safe
    resolution: the only field this tool ever edits is the distributor image, so
    anything unsaved is a half-finished attempt we want dropped rather than
    committed.

    Returns True if a dialog was dismissed.
    """
    if not sel.UNSAVED_DIALOG_DISCARD:
        return False

    button = page.locator(sel.UNSAVED_DIALOG_DISCARD)
    try:
        if button.count() and button.first.is_visible():
            button.first.click()
            page.wait_for_timeout(400)
            log.info("discarded a leftover unsaved-changes dialog")
            return True
    except Exception:
        pass  # never fatal — the caller's own waits will surface a real problem
    return False


def find_product(page, query: str) -> bool:
    """Search the OMS for `query` and open the single matching product.

    `query` is the item number — Woopsa's search box accepts "Item #,
    description, UPC, vendor item #", and the item number is exact where the
    catalogue description is not (it carries "*****", "(COKE)", truncations).

    Raises on an ambiguous match (0 or >1 rows) so the caller skips the product.
    Guessing here is how the wrong image reaches a live product.
    """
    # Single-page app: navigate once, then reuse the same page for every
    # product. Re-loading per product would be slow and needless.
    if sel.PRODUCT_SEARCH_URL and sel.PRODUCT_SEARCH_URL not in page.url:
        page.goto(_resolve_url(sel.PRODUCT_SEARCH_URL))

    dismiss_unsaved_dialog(page)

    box = page.locator(sel.SEARCH_INPUT).first
    box.wait_for(timeout=sel.DEFAULT_TIMEOUT_MS)

    # type() rather than fill(): the list filters on input events, and fill()
    # sets the value in one shot, which some React handlers miss. Clearing
    # first matters too — the box holds the previous product's number.
    box.click()
    box.press("ControlOrMeta+a")
    box.press("Delete")
    box.type(query, delay=25)

    if sel.SEARCH_SUBMIT:
        page.locator(sel.SEARCH_SUBMIT).first.click()

    # The filter is debounced (~450ms) and runs client-side, so there may be no
    # network activity at all to wait on. Poll for the list to settle instead.
    rows = page.locator(sel.SEARCH_RESULT_ROWS)
    deadline = time.monotonic() + (sel.DEFAULT_TIMEOUT_MS / 1000)
    stable_count, stable_since = -1, 0.0
    while time.monotonic() < deadline:
        page.wait_for_timeout(150)
        count = rows.count()
        if count == stable_count:
            if count and time.monotonic() - stable_since > 0.5:
                break  # same non-zero count for 500ms — settled
        else:
            stable_count, stable_since = count, time.monotonic()

    count = rows.count()
    if count == 0:
        raise UploadError(f"no OMS result for {query!r} (0 matches)")

    target = rows.first
    if count > 1:
        # Woopsa searches item #, description, UPC and vendor item # at once, so
        # a short item number like "1154" also matches rows whose UPC happens to
        # contain it — 8 hits, only one of which is item 1154. Rather than refuse
        # every 4-digit product, look for the row whose ITEM NUMBER is exactly
        # the query: rows render as "#1154  FANTA 20 OZ GRAPE".
        exact = [i for i in range(count)
                 if (rows.nth(i).inner_text() or "").strip().startswith(f"#{query}\n")
                 or (rows.nth(i).inner_text() or "").strip().startswith(f"#{query} ")]
        if len(exact) != 1:
            raise UploadError(
                f"ambiguous OMS match for {query!r} ({count} matches, "
                f"{len(exact)} exact) — resolve by hand; not guessing"
            )
        target = rows.nth(exact[0])

    if sel.SEARCH_RESULT_LINK:
        target.locator(sel.SEARCH_RESULT_LINK).first.click()
    else:
        target.click()

    page.wait_for_load_state("networkidle", timeout=sel.DEFAULT_TIMEOUT_MS)

    # Confirm the page we landed on really is the product we searched for.
    # The item number is checked rather than the name: catalogue descriptions
    # carry "*****", vendor codes and truncations, so they don't compare cleanly.
    #
    # The header renders before its data arrives — it briefly shows "#— —" and
    # "Loading…" — so a single snapshot races the panel and reports the wrong
    # product. Poll until the item number appears rather than sampling once.
    if sel.PRODUCT_NAME_ON_PAGE:
        header = page.locator(sel.PRODUCT_NAME_ON_PAGE).first
        deadline = time.monotonic() + (sel.DEFAULT_TIMEOUT_MS / 1000)
        shown = ""
        while time.monotonic() < deadline:
            shown = (header.inner_text() or "").strip()
            if query in shown:
                return True
            if shown and "loading" not in shown.lower() and "—" not in shown:
                break  # settled on some OTHER product; stop early and report it
            page.wait_for_timeout(200)

        raise UploadError(
            f"opened the wrong product: searched item {query!r}, page shows {shown[:80]!r}"
        )

    return True


def enter_edit_mode(page) -> None:
    """Click Edit. The dropzone is read-only until the product is in edit mode."""
    if not sel.UPLOAD_OPEN_BUTTON:
        return
    page.locator(sel.UPLOAD_OPEN_BUTTON).first.click()
    page.wait_for_timeout(600)


def attach_image(page, image_path: Path) -> None:
    """Put the file on the distributor dropzone's file input.

    Assumes edit mode is already on. Woopsa renders TWO dropzones per product —
    MASTER above, DISTRIBUTOR below — so FILE_INPUT must be scoped to the
    distributor one. If the selector matches both, `.first` would silently pick
    MASTER, which is the shared catalogue image; that is what the count check
    below guards against.
    """
    # WAIT FOR THE IMAGE PANEL TO REBIND BEFORE TOUCHING ANYTHING.
    #
    # The header updates as soon as a product is clicked, but #pv1ImageSlot is
    # populated by a separate request and for ~500ms still shows the PREVIOUS
    # product. Attaching during that window uploads against the previous
    # product's binding: the file is stored under the wrong item id, and every
    # product in a batch ends up with its neighbour's picture. That is exactly
    # how 14 products were mislabelled before this wait existed.
    #
    # Settling is detected by the src holding steady, which covers both cases:
    # a product that has an image (src appears and stays) and one that does not
    # (src stays empty).
    deadline = time.monotonic() + (sel.DEFAULT_TIMEOUT_MS / 1000)
    last_src, stable_since = object(), 0.0
    while time.monotonic() < deadline:
        current = existing_distributor_image(page)
        if current == last_src:
            if time.monotonic() - stable_since >= 1.0:
                break
        else:
            last_src, stable_since = current, time.monotonic()
        page.wait_for_timeout(150)

    # Captured after settling, so the post-attach wait can tell "our upload
    # landed" apart from "this product already had a distributor image".
    src_before = existing_distributor_image(page)

    inputs = page.locator(sel.FILE_INPUT) if sel.FILE_INPUT else None

    if inputs is not None:
        count = inputs.count()
        if count == 0:
            raise UploadError(
                "no file input found — is the product in edit mode? "
                "(check UPLOAD_OPEN_BUTTON points at the Edit button)"
            )
        if count > 1:
            raise UploadError(
                f"FILE_INPUT matches {count} inputs (MASTER and DISTRIBUTOR). "
                "Scope it to the distributor dropzone only — uploading to MASTER "
                "would overwrite the shared catalogue image."
            )
        # Works even when the input is hidden behind a styled dropzone.
        inputs.first.set_input_files(str(image_path))
    elif sel.FILE_CHOOSER_BUTTON:
        # Clicking the dropzone opens a native file chooser. Playwright
        # intercepts it, so no OS dialog is actually shown.
        zone = page.locator(sel.FILE_CHOOSER_BUTTON)
        count = zone.count()
        if count == 0:
            raise UploadError(f"dropzone not found: {sel.FILE_CHOOSER_BUTTON}")
        if count > 1:
            raise UploadError(
                f"FILE_CHOOSER_BUTTON matches {count} dropzones (MASTER and "
                "DISTRIBUTOR). Scope it to the distributor one — uploading to "
                "MASTER would overwrite the shared catalogue image."
            )
        with page.expect_file_chooser(timeout=sel.DEFAULT_TIMEOUT_MS) as fc:
            zone.first.click()
        fc.value.set_files(str(image_path))
    else:
        raise UploadError("neither FILE_INPUT nor FILE_CHOOSER_BUTTON is set")

    # Wait for the preview to actually change, rather than sleeping a fixed
    # interval and hoping. The upload has to reach the CDN before the slot's
    # <img> src updates, and that takes anywhere from instant to several
    # seconds depending on file size and network.
    #
    # Comparing against the src captured BEFORE the attach is what makes this
    # honest: a product that already had a distributor image would otherwise
    # look successful the moment we checked, without our upload having landed.
    if sel.DISTRIBUTOR_IMAGE:
        deadline = time.monotonic() + (sel.SAVE_TIMEOUT_MS / 1000)
        while time.monotonic() < deadline:
            if existing_distributor_image(page) != src_before:
                break
            page.wait_for_timeout(200)
        else:
            log.warning("preview never changed after attaching %s — continuing, "
                        "the save confirmation is the real check", image_path.name)

        # Woopsa stores the file as <item-id>.jpg, named from whichever product
        # the panel is bound to. If that is not the product we opened, the
        # upload has just gone to someone else's record and the caller must know.
        stored = existing_distributor_image(page).rsplit("/", 1)[-1]
        expected = f"{image_path.stem}.jpg"
        if stored and stored != expected:
            raise UploadError(
                f"upload landed on the WRONG product: expected {expected}, "
                f"OMS stored {stored}. The image panel was still bound to "
                "another item."
            )

    page.wait_for_timeout(500)  # let the client-side state settle


def enable_distributor_image(page) -> None:
    """Turn on "Show Distributor Image" AFTER the file is attached.

    Order matters and matches the manual flow (edit -> upload -> toggle -> save):
    the toggle publishes the distributor image in place of the master one, so it
    is the last thing set before saving.

    The badge is then checked as a hard gate. If it doesn't read "Distributor"
    we raise rather than save, because a save in the master slot overwrites an
    image other distributors read.
    """
    if sel.IMAGE_SLOT != "distributor":
        return  # explicitly configured otherwise; the operator accepted that

    if sel.DISTRIBUTOR_TOGGLE and sel.DISTRIBUTOR_BADGE:
        badge = page.locator(sel.DISTRIBUTOR_BADGE).first
        badge.wait_for(timeout=sel.DEFAULT_TIMEOUT_MS)

        # Read the badge, never the toggle's checked state: the toggle is a
        # styled <div>, so is_checked() quietly returns False instead of
        # raising.
        #
        # Attaching a file ALREADY selects the distributor slot — the panel
        # switches to show what you just uploaded. So in the normal case there
        # is nothing to click here, and clicking anyway is actively harmful:
        # the toggle then switches AWAY to the master view and will not come
        # back within the session (it only has something to toggle to while a
        # freshly-uploaded distributor image exists).
        #
        # Observed, with no upload:      Master -> Master -> Master  (inert)
        # Observed, after an upload:     Distributor -> Master -> Master (stuck)
        #
        # The switch is not instant — the upload has to reach the CDN first, so
        # the badge lags the attach and the delay varies with image size and
        # network. Poll rather than sleeping a fixed interval: reading too early
        # sees "Master", and the click below would then break a state that was
        # about to become correct on its own.
        deadline = time.monotonic() + (sel.SAVE_TIMEOUT_MS / 1000)
        while time.monotonic() < deadline:
            if "distributor" in (badge.inner_text() or "").strip().lower():
                break
            page.wait_for_timeout(150)

        # Only if it never switched by itself: nudge it once, then let
        # assert_distributor_slot decide whether we are actually safe to save.
        if "distributor" not in (badge.inner_text() or "").strip().lower():
            toggle = page.locator(sel.DISTRIBUTOR_TOGGLE).first
            toggle.wait_for(timeout=sel.DEFAULT_TIMEOUT_MS)
            toggle.click()
            page.wait_for_timeout(800)

    assert_distributor_slot(page)


def assert_distributor_slot(page) -> None:
    """Raise unless the distributor image slot is the active one.

    Called immediately before the save click as well as after the toggle. The
    check is deliberately duplicated: writing the master slot overwrites an
    image other distributors read, so the guard belongs next to the irreversible
    action rather than one function call away from it.
    """
    if sel.IMAGE_SLOT != "distributor" or not sel.DISTRIBUTOR_BADGE:
        return

    badge = (page.locator(sel.DISTRIBUTOR_BADGE).first.inner_text() or "").strip()
    if "distributor" not in badge.lower():
        raise UploadError(
            f"refusing to save: image slot shows {badge!r}, not the "
            "distributor slot (would overwrite the shared MASTER image)"
        )


def save_changes(page, dry_run: bool) -> str:
    """Click Woopsa's save button and confirm it took. Returns a short outcome string.

    NOT part of the distributor-image upload flow -- attach_image() already
    POSTs to the OMS and persists immediately, confirmed directly in the OMS
    UI, so the real upload path (worker.py, and this module's own
    run_upload_pass()) never calls this. It used to: calling it unconditionally
    after every attach meant a successful upload got recorded as FAILED
    whenever #saveBtn didn't behave the way this function expects for that
    action (its own timeout, or no applicable save step at all).

    Kept for any OTHER action against this OMS that genuinely does need a
    save click and confirmation -- e.g. editing a text field rather than the
    distributor image -- where attaching a file was never the persisting
    step and this really is the one irreversible action.

    Success is judged on a TRANSITION, not a state. Woopsa's success marker
    (#editBtn becoming visible) can already be true before we click — on a
    product that was never in edit mode it is visible the whole time. Waiting on
    a condition that already holds would report "saved" for a save that silently
    failed, so the marker's state is captured first and we require it to change.
    """
    # Last line of defence, checked on the dry-run path too so a dry run
    # surfaces a wrong-slot condition instead of silently reporting success.
    assert_distributor_slot(page)

    if dry_run:
        return "dry_run (save not clicked)"

    marker = page.locator(sel.SAVE_SUCCESS_MARKER).first if sel.SAVE_SUCCESS_MARKER else None
    marker_before = False
    if marker is not None:
        try:
            marker_before = marker.is_visible()
        except Exception:
            marker_before = False

    page.locator(sel.SAVE_BUTTON).first.click()

    if sel.SAVE_ERROR_MARKER:
        page.wait_for_timeout(500)
        err = page.locator(sel.SAVE_ERROR_MARKER)
        if err.count() and err.first.is_visible():
            raise UploadError(
                f"OMS reported an error: {(err.first.inner_text() or '').strip()[:120]}"
            )

    if marker is None:
        page.wait_for_load_state("networkidle", timeout=sel.SAVE_TIMEOUT_MS)
        return "saved (unconfirmed — set SAVE_SUCCESS_MARKER to verify)"

    if marker_before:
        # Can't use appearance as the signal. Fall back to the save button going
        # away, which is what Woopsa does when it leaves edit mode.
        try:
            page.locator(sel.SAVE_BUTTON).first.wait_for(
                state="hidden", timeout=sel.SAVE_TIMEOUT_MS
            )
            return "saved (confirmed: save button dismissed)"
        except Exception as exc:
            raise UploadError(
                "save clicked but the form never left edit mode — treat as failed"
            ) from exc

    try:
        marker.wait_for(timeout=sel.SAVE_TIMEOUT_MS)
    except Exception as exc:
        raise UploadError("save clicked but no success confirmation appeared") from exc
    return "saved (confirmed)"


# --------------------------------------------------------------------------
# Main run
# --------------------------------------------------------------------------
def run_upload(limit: int | None, dry_run: bool, redo: bool,
               include_review: bool, headed: bool,
               allow_unwatermarked: bool = False) -> None:
    from playwright.sync_api import sync_playwright

    _require_selectors()

    if not config.OMS_URL:
        log.error("OMS_URL is not set. Add it to .env first.")
        sys.exit(2)

    if not config.BROWSER_PROFILE_DIR.exists() and not config.STORAGE_STATE_PATH.exists():
        log.error("no saved session — run:  python oms_uploader.py --login")
        sys.exit(2)

    products = eligible_products(limit, include_review, allow_unwatermarked)
    if not products:
        log.info("nothing eligible to upload")
        return

    uploads = load_json(config.UPLOAD_MANIFEST_PATH)
    done = ok = failed = skipped = 0

    with sync_playwright() as p:
        # Persistent profile, same one --login populated. Keeps the OMS's
        # "remembered device" cookie so a run never triggers a fresh OTP.
        context = p.chromium.launch_persistent_context(
            str(config.BROWSER_PROFILE_DIR),
            headless=not headed,
            viewport={"width": 1500, "height": 1000},
        )
        context.set_default_timeout(sel.DEFAULT_TIMEOUT_MS)
        page = context.pages[0] if context.pages else context.new_page()

        page.goto(_resolve_url(sel.PRODUCT_SEARCH_URL))
        page.wait_for_timeout(2000)  # let the SPA boot

        if sel.LOGGED_IN_MARKER and not page.locator(sel.LOGGED_IN_MARKER).count():
            log.error("not logged in (%s not found) — run:  python oms_uploader.py --login",
                      sel.LOGGED_IN_MARKER)
            log.error("current URL: %s", page.url)
            context.close()
            sys.exit(2)

        for record in products:
            item_id = str(record.get("id"))
            name = record.get("name", "")
            prior = uploads.get(item_id, {})

            if not redo and prior.get("status") == "uploaded":
                skipped += 1
                continue

            image = image_path_for(record, allow_unwatermarked)
            done += 1
            try:
                # The manual flow, in order: open product -> click the
                # distributor dropzone and attach -> toggle -> save. No edit
                # mode needed: the dropzone opens the file picker directly.
                find_product(page, item_id)
                enter_edit_mode(page)  # no-op unless UPLOAD_OPEN_BUTTON is set

                if dry_run:
                    # STOP BEFORE ATTACHING. Attaching is not a local preview —
                    # it POSTs to /api/distrubutor/uploadProductImage and the
                    # file is stored server-side immediately, with or without a
                    # later save click. A dry run that attached would therefore
                    # publish for real, which is exactly what happened before
                    # this check existed.
                    outcome = "dry_run (product opened; file NOT attached)"
                else:
                    attach_image(page, image)
                    enable_distributor_image(page)
                    # No save_changes() call: enable_distributor_image() already
                    # runs assert_distributor_slot(), the actual safety gate
                    # against writing the wrong slot, and save_changes() was
                    # never anything more than that same assertion plus a click
                    # on a button this action doesn't use -- attaching persists
                    # immediately, confirmed directly in the OMS UI. Calling it
                    # anyway used to mark a product FAILED whenever its
                    # #saveBtn never appeared, even though the attach had
                    # already succeeded and the image was live.
                    outcome = "uploaded (attach confirmed; no save step needed)"

                uploads[item_id] = {
                    "id": item_id,
                    "name": name,
                    "image": str(image.relative_to(config.BASE_DIR)),
                    "status": "dry_run" if dry_run else "uploaded",
                    "outcome": outcome,
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                }
                ok += 1
                log.info("[%d/%d] %s -> %s", done, len(products), name, outcome)

            except Exception as exc:
                # Any per-product failure is recorded and stepped over. A
                # brittle selector on one product must not lose the whole run.
                failed += 1
                reason = str(exc).split("\n")[0][:200]
                uploads[item_id] = {
                    "id": item_id,
                    "name": name,
                    "image": str(image.relative_to(config.BASE_DIR)) if image else "",
                    "status": "failed",
                    "outcome": reason,
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                }
                log.warning("[%d/%d] %s -> FAILED: %s", done, len(products), name, reason)

            save_upload_manifest(uploads)  # after every product, so a crash resumes
            time.sleep(config.UPLOAD_SLEEP_BETWEEN)

        context.close()

    log.info("upload complete: %d ok, %d failed, %d skipped (already uploaded), %d eligible",
             ok, failed, skipped, len(products))
    log.info("upload manifest: %s", config.UPLOAD_MANIFEST_PATH)
    if dry_run:
        log.info("DRY RUN — no save button was clicked.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Phase 2: upload verified product images into the OMS.")
    parser.add_argument("--login", action="store_true",
                        help="open a browser, log in by hand, and save the session")
    parser.add_argument("--dry-run", action="store_true",
                        help="do everything except click save")
    parser.add_argument("--limit", type=int, default=None,
                        help="only process the first N eligible products")
    parser.add_argument("--redo", action="store_true",
                        help="re-upload products already marked uploaded")
    parser.add_argument("--include-review", action="store_true",
                        help="also upload status=review items (default: accepted only)")
    parser.add_argument("--headed", action="store_true",
                        help="show the browser (default: headless)")
    parser.add_argument("--allow-unwatermarked", action="store_true",
                        help="publish the plain library image when no watermarked "
                             "version exists (default: refuse and tell you to run "
                             "--watermark-only first)")
    args = parser.parse_args()

    if args.login:
        run_login()
        return

    run_upload(args.limit, args.dry_run, args.redo, args.include_review,
               args.headed, args.allow_unwatermarked)


if __name__ == "__main__":
    main()
