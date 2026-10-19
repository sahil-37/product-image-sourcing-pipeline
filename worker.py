#!/usr/bin/env python
"""Local worker: watermarks stakeholder-supplied images and uploads to the OMS.

    python worker.py --dry-run        # claim and process, but never attach
    python worker.py                  # for real
    python worker.py --once           # one pass instead of polling

WHY THIS RUNS LOCALLY AND NOT ON VERCEL

Two things pin it here:

1. The OMS upload needs Playwright driving a *persistent* Chromium profile.
   Auth is passwordless -- a remembered-device cookie living in
   work/oms_browser_profile/ -- and re-logging in needs an OTP that has to be
   retrieved by phoning head office. Serverless has no persistent filesystem,
   so a cloud worker would trigger that phone call on every cold start.

2. Watermarking must use the real apply_watermark(). It is driven by a dozen
   config constants (style, tile opacity, angle, gap, emboss relief). A second
   implementation in a serverless function or in sharp would drift, and
   stakeholder-supplied images would carry a visibly different logo from the
   other several hundred.

So the app records intent and this polls for it. Nothing in the cloud ever
calls this machine, which means no tunnel, no inbound firewall rule, and
nothing breaks when the laptop is closed -- work simply waits.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import ipaddress
import json
import logging
import os
import socket
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

import requests

from lowprice import config, watermark
from lowprice.phase2 import oms_uploader as oms

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("worker")

BASE = Path(__file__).resolve().parent
BLOB_API = "https://blob.vercel-storage.com"

# Vercel Blob is suspended (billing) -- images now live on local disk, under
# the same app/public/images/ that push_to_web.py writes to and Next.js
# serves statically. Set IMAGE_STORE=blob to switch back if reactivated.
IMAGE_STORE = os.environ.get("IMAGE_STORE", "local")  # local | blob
LOCAL_IMAGE_DIR = BASE / "app" / "public" / "images"

POLL_SECONDS = 30
MAX_IMAGE_BYTES = 10 * 1024 * 1024
FETCH_TIMEOUT = 20

# Marks an expired OMS session. Every job fails identically until a human
# re-authenticates, so the app keys its "uploads paused" banner off this and
# stops presenting it as a per-product problem.
AUTH_EXPIRED = "auth_expired"


def env(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        sys.exit(f"{name} is not set. Try:  set -a && . ./app/.env.local && set +a")
    return v


# --------------------------------------------------------------------------
# Untrusted input
# --------------------------------------------------------------------------
def assert_public_url(url: str) -> None:
    """Reject anything that is not a public http(s) address.

    Stakeholder-supplied URLs are untrusted input arriving from a browser. The
    app checks the obvious shapes, but it is the fetch that actually matters,
    and the fetch happens here -- so this is the real boundary, resolving DNS
    rather than pattern-matching the hostname.
    """
    p = urlparse(url)
    if p.scheme not in ("http", "https"):
        raise ValueError("only http and https links can be fetched")
    if not p.hostname:
        raise ValueError("no host in that link")
    try:
        infos = socket.getaddrinfo(p.hostname, None)
    except socket.gaierror as exc:
        raise ValueError(f"could not resolve {p.hostname}") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            raise ValueError("that address is not reachable from the public internet")


def fetch_image(url: str) -> bytes:
    """Download with a size cap, streaming so a huge file is cut off early."""
    assert_public_url(url)
    with requests.get(url, timeout=FETCH_TIMEOUT, stream=True,
                      headers={"user-agent": "lowprice-worker/1.0"}) as r:
        r.raise_for_status()
        declared = r.headers.get("content-length")
        if declared and int(declared) > MAX_IMAGE_BYTES:
            raise ValueError("that image is larger than 10MB")
        buf = io.BytesIO()
        for chunk in r.iter_content(65536):
            buf.write(chunk)
            if buf.tell() > MAX_IMAGE_BYTES:
                raise ValueError("that image is larger than 10MB")
        return buf.getvalue()


def blob_put(path: str, raw: bytes, token: str, attempts: int = 4) -> str:
    last: Exception | None = None
    for i in range(attempts):
        try:
            resp = requests.put(
                f"{BLOB_API}/{path}",
                data=raw,
                headers={
                    "authorization": f"Bearer {token}",
                    "x-content-type": "image/jpeg",
                    "x-add-random-suffix": "0",
                    "x-api-version": "7",
                },
                timeout=60,
            )
            if resp.status_code < 300:
                return resp.json()["url"]
            if resp.status_code < 500:
                raise RuntimeError(f"{resp.status_code} {resp.text[:160]}")
            last = RuntimeError(f"{resp.status_code} {resp.text[:160]}")
        except requests.RequestException as exc:
            last = exc
        if i < attempts - 1:
            time.sleep(2 ** i)
    raise RuntimeError(f"blob put failed after {attempts} attempts: {last}")


def blob_get(url: str, attempts: int = 3) -> bytes:
    """Blob is eventually consistent -- a freshly written URL can 404 briefly."""
    last: Exception | None = None
    for i in range(attempts):
        try:
            r = requests.get(url, timeout=60)
            if r.status_code < 300:
                return r.content
            last = RuntimeError(f"{r.status_code} fetching {url[:80]}")
        except requests.RequestException as exc:
            last = exc
        if i < attempts - 1:
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(str(last))


def local_get(path: str) -> bytes:
    """Read straight off disk -- worker and app share a filesystem now.

    Rejects an absolute http(s) URL rather than silently mis-resolving it: a
    row pushed before the switch to local storage still holds an old Vercel
    Blob URL, and that needs a re-push (IMAGE_STORE=blob or a fresh push), not
    a worker that tries to treat the URL text as a filesystem path.
    """
    if path.startswith(("http://", "https://")):
        raise RuntimeError(
            f"{path} is a remote URL, not a local path -- this product was "
            "pushed before the move to local image storage; re-run push_to_web.py")
    rel = path[len("/images/"):] if path.startswith("/images/") else path
    p = LOCAL_IMAGE_DIR / rel
    if not p.exists():
        raise RuntimeError(f"no local image at {p}")
    return p.read_bytes()


def local_put(path: str, raw: bytes) -> str:
    dest = LOCAL_IMAGE_DIR / path
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(raw)
    return f"/images/{path}"


def image_get(url: str) -> bytes:
    if IMAGE_STORE == "blob":
        return blob_get(url)
    return local_get(url)


def image_put(path: str, raw: bytes, token: str) -> str:
    if IMAGE_STORE == "blob":
        return blob_put(path, raw, token)
    return local_put(path, raw)


# --------------------------------------------------------------------------
# App API
# --------------------------------------------------------------------------
class App:
    def __init__(self, base_url: str, token: str, worker_id: str):
        self.base = base_url.rstrip("/")
        self.h = {"authorization": f"Bearer {token}"}
        self.worker_id = worker_id

    def claim(self, max_jobs: int) -> dict:
        r = requests.post(f"{self.base}/api/jobs/claim", json={
            "worker_id": self.worker_id, "max": max_jobs},
            headers=self.h, timeout=60)
        r.raise_for_status()
        return r.json()

    def complete_job(self, job_id: int, outcome: str | None, error: str | None) -> None:
        requests.post(f"{self.base}/api/jobs/{job_id}/complete",
                      json={"outcome": outcome, "error": error},
                      headers=self.h, timeout=60).raise_for_status()

    def complete_override(self, oid: int, processed_url: str | None,
                          sha: str | None, error: str | None) -> None:
        requests.post(f"{self.base}/api/overrides/{oid}/complete",
                      json={"processed_url": processed_url, "sha256": sha, "error": error},
                      headers=self.h, timeout=60).raise_for_status()

    def heartbeat(self, wm_hash: str, note: str | None = None) -> None:
        try:
            requests.post(f"{self.base}/api/worker/heartbeat", json={
                "worker_id": self.worker_id, "watermark_hash": wm_hash, "note": note},
                headers=self.h, timeout=30)
        except requests.RequestException:
            pass  # liveness is a nicety; never let it stop real work


def watermark_config_hash() -> str:
    """Fingerprint of the settings that define the logo composite.

    Sent with each heartbeat so a drift between what produced the library and
    what this worker would produce is visible rather than discovered in the
    client's catalogue.
    """
    keys = ["WATERMARK_STYLE", "WATERMARK_POSITION", "WATERMARK_MARGIN_FRAC",
            "WATERMARK_WIDTH_FRAC", "WATERMARK_OPACITY", "WATERMARK_CENTER_WIDTH_FRAC",
            "WATERMARK_CENTER_OPACITY", "WATERMARK_TILE_WIDTH_FRAC",
            "WATERMARK_TILE_OPACITY", "WATERMARK_TILE_ANGLE", "WATERMARK_TILE_GAP",
            "WATERMARK_EMBOSS_OPACITY", "OUTPUT_SIZE", "JPEG_QUALITY"]
    blob = json.dumps({k: getattr(config, k, None) for k in keys}, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


# --------------------------------------------------------------------------
# Work
# --------------------------------------------------------------------------
def process_override(app: App, ov: dict, blob_token: str) -> None:
    oid, pid, url = ov["id"], ov["product_id"], ov["submitted_url"]
    log.info("override %s for product %s", oid, pid)
    try:
        raw = fetch_image(url)
        # Square first, then watermark: the logo fractions are relative to
        # canvas width, so branding an un-normalised image would size the mark
        # differently from every other product.
        out = watermark.watermark_bytes(raw)
        sha = hashlib.sha256(out).hexdigest()
        image_url = image_put(f"override/{pid}/{sha[:16]}.jpg", out, blob_token)
        app.complete_override(oid, image_url, sha, None)
        log.info("  processed -> %s", image_url)
    except Exception as exc:
        msg = str(exc).split("\n")[0][:200]
        log.warning("  failed: %s", msg)
        app.complete_override(oid, None, None, msg)


def upload_jobs(app: App, jobs: list[dict], dry_run: bool) -> None:
    """Run the existing Playwright flow over claimed jobs, in one browser."""
    from playwright.sync_api import sync_playwright

    oms._require_selectors()
    if not config.OMS_URL:
        for j in jobs:
            app.complete_job(j["id"], None, "OMS_URL is not set")
        return
    if not config.BROWSER_PROFILE_DIR.exists() and not config.STORAGE_STATE_PATH.exists():
        for j in jobs:
            app.complete_job(j["id"], None, f"{AUTH_EXPIRED}: no saved session")
        return

    uploads = oms.load_json(config.UPLOAD_MANIFEST_PATH)

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            str(config.BROWSER_PROFILE_DIR), headless=True,
            viewport={"width": 1500, "height": 1000},
        )
        context.set_default_timeout(oms.sel.DEFAULT_TIMEOUT_MS)
        page = context.pages[0] if context.pages else context.new_page()
        try:
            page.goto(oms._resolve_url(oms.sel.PRODUCT_SEARCH_URL))
            page.wait_for_timeout(2000)

            if oms.sel.LOGGED_IN_MARKER and not page.locator(oms.sel.LOGGED_IN_MARKER).count():
                # Not a per-product failure: every job would fail the same way.
                log.error("OMS session expired — run: python -m lowprice.phase2.oms_uploader --login")
                for j in jobs:
                    app.complete_job(j["id"], None, f"{AUTH_EXPIRED}: session needs renewal")
                return

            for j in jobs:
                pid = j["product_id"]
                try:
                    raw = image_get(j["image_url"])
                    # MUST be named "<item-id>.jpg". attach_image derives the
                    # expected stored filename from image_path.stem and compares
                    # it against what the OMS reports back -- that guard is what
                    # catches an upload landing on the previous product while
                    # the image panel was still rebinding. A temp name like
                    # tmp8fa3.jpg makes the comparison meaningless and the
                    # attach fails downstream.
                    tmpdir = Path(tempfile.mkdtemp(prefix="lowprice-upload-"))
                    img_path = tmpdir / f"{pid}.jpg"
                    img_path.write_bytes(raw)
                    try:
                        oms.find_product(page, pid)
                        oms.enter_edit_mode(page)
                        if dry_run:
                            # Attaching IS the upload: it POSTs straight to the
                            # OMS and stores server-side, save button or not. A
                            # dry run must stop before that, not before saving.
                            outcome = "dry_run (product opened; file NOT attached)"
                        else:
                            oms.attach_image(page, img_path)
                            oms.enable_distributor_image(page)
                            # No save_changes() call: enable_distributor_image()
                            # already runs assert_distributor_slot(), the actual
                            # safety gate against writing the wrong slot, and
                            # save_changes() was never anything more than that
                            # same assertion plus a click on a button this
                            # action doesn't use -- attaching persists
                            # immediately, confirmed directly in the OMS UI. The
                            # real bug this replaces: save_changes() timing out
                            # (its #saveBtn never appears for this action) was
                            # marking a product FAILED even when the attach had
                            # already succeeded and the image was live.
                            outcome = "uploaded (attach confirmed; no save step needed)"
                    finally:
                        img_path.unlink(missing_ok=True)
                        tmpdir.rmdir()

                    # A dry run must leave the job claimable for a real pass --
                    # completing it here is what silently "consumed" job 103314
                    # on 2026-09-12 with no file ever attached.
                    if not dry_run:
                        app.complete_job(j["id"], outcome, None)
                    uploads[pid] = {
                        "id": pid, "image": j["image_url"],
                        "status": "dry_run" if dry_run else "uploaded",
                        "outcome": outcome, "origin": j.get("origin", "pipeline"),
                        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    }
                    log.info("  %s -> %s", pid, outcome)
                except Exception as exc:
                    reason = str(exc).split("\n")[0][:200]
                    if not dry_run:
                        app.complete_job(j["id"], None, reason)
                    uploads[pid] = {
                        "id": pid, "image": j["image_url"],
                        "status": "dry_run_failed" if dry_run else "failed",
                        "outcome": reason, "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    }
                    log.warning("  %s -> FAILED: %s", pid, reason)

                # Local audit trail, written after every product so a crash
                # leaves a record of exactly how far the run got.
                oms.save_upload_manifest(uploads)
                time.sleep(config.UPLOAD_SLEEP_BETWEEN)
        finally:
            context.close()


def one_pass(app: App, blob_token: str, dry_run: bool, max_jobs: int) -> int:
    claimed = app.claim(max_jobs)
    jobs = claimed.get("jobs") or []
    ovs = claimed.get("overrides") or []

    for ov in ovs:
        process_override(app, ov, blob_token)

    if jobs:
        upload_jobs(app, jobs, dry_run)

    return len(jobs) + len(ovs)


def main() -> None:
    ap = argparse.ArgumentParser(description="Local upload/watermark worker.")
    ap.add_argument("--app-url", default=os.environ.get("APP_URL", "http://localhost:3000"))
    ap.add_argument("--dry-run", action="store_true",
                    help="open each product but never attach the image")
    ap.add_argument("--once", action="store_true", help="one pass, then exit")
    ap.add_argument("--max", type=int, default=5, help="jobs to claim per pass")
    ap.add_argument("--worker-id", default=os.environ.get("WORKER_ID", socket.gethostname()))
    args = ap.parse_args()

    app = App(args.app_url, env("WORKER_TOKEN"), args.worker_id)
    blob_token = env("BLOB_READ_WRITE_TOKEN") if IMAGE_STORE == "blob" else ""
    wm_hash = watermark_config_hash()

    log.info("worker %s -> %s%s", args.worker_id, args.app_url, "  [DRY RUN]" if args.dry_run else "")
    log.info("watermark config %s", wm_hash)

    while True:
        try:
            app.heartbeat(wm_hash)
            n = one_pass(app, blob_token, args.dry_run, args.max)
            if n:
                log.info("handled %d item(s)", n)
        except KeyboardInterrupt:
            log.info("stopped")
            return
        except Exception as exc:
            log.warning("pass failed: %s", str(exc).split("\n")[0][:200])

        if args.once:
            return
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
