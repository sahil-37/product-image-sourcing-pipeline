#!/usr/bin/env python
"""Push pipeline output to the review app: images to local disk, rows to Postgres.

The pipeline runs locally and writes to gitignored directories; the app (also
run locally now -- see README) serves images straight out of its own
app/public/images/ via Next's static file handling. This is the bridge.

    python push_to_web.py --dry-run              # report what would go up
    python push_to_web.py                        # all known batches
    python push_to_web.py --batch food_pilot     # one batch

Images used to go to Vercel Blob; that store is suspended (billing), and we
moved to local disk rather than a new paid cloud account. Set IMAGE_STORE=blob
to switch back if Blob billing is reactivated -- blob_put() is kept for that.

IMPORTANT: this only ever writes the `products` and `runs` tables. Human
decisions live in `reviews` and `overrides` precisely so that a careless
re-push cannot erase them.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import requests
from PIL import Image

import flags as flagmod

BASE = Path(__file__).resolve().parent

# (batch key, label, manifest, traces dir, library dir, enhanced dir or None)
BATCHES = [
    ("food_pilot", "Batch 1 · food pilot",
     "work/manifest_food_pilot.json", "work/traces_food_pilot",
     "library_food_pilot", "library_enhanced_food_pilot"),
    ("food_batch2", "Batch 2 · spread sample",
     "work/manifest_food_batch2.json", "work/traces_food_batch2",
     "library_food_batch2", "library_enhanced_food_batch2"),
    ("adversarial", "Batch 3 · adversarial",
     "work/manifest_adversarial.json", "work/traces_adversarial",
     "library_adversarial", "library_enhanced_adversarial"),
    ("drinks", "Drinks",
     "work/manifest.json", "work/traces", "library", "library_enhanced"),
]

THUMB_PX = 256
THUMB_QUALITY = 70
BLOB_API = "https://blob.vercel-storage.com"

# Where app/public/images/ lives -- must match app/public/images/ exactly, since
# that is the directory Next.js serves statically at /images/... with no code
# of its own. Images end up reachable at {app-url}/images/<path>.
IMAGE_STORE = os.environ.get("IMAGE_STORE", "local")  # local | blob
LOCAL_IMAGE_DIR = BASE / "app" / "public" / "images"


def env(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        sys.exit(f"{name} is not set. Copy app/.env.local.example and fill it in, "
                 f"then: export $(grep -v '^#' app/.env.local | xargs)")
    return v


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def blob_put(path: str, raw: bytes, token: str, content_type: str = "image/jpeg",
             attempts: int = 4) -> str:
    """Upload to Vercel Blob, returning the public CDN URL.

    Retries on transport errors and 5xx. A few hundred sequential TLS uploads
    reliably turn up a dropped connection or a bad record MAC; without a retry
    one blip aborts the whole push after most of the work is already done.
    """
    last: Exception | None = None
    for i in range(attempts):
        try:
            resp = requests.put(
                f"{BLOB_API}/{path}",
                data=raw,
                headers={
                    "authorization": f"Bearer {token}",
                    "x-content-type": content_type,
                    "x-add-random-suffix": "0",  # stable URLs, so a re-push overwrites
                    "x-api-version": "7",
                },
                timeout=60,
            )
            if resp.status_code < 300:
                return resp.json()["url"]
            # 4xx is a real rejection; retrying will not help.
            if resp.status_code < 500:
                raise RuntimeError(
                    f"blob put {path} failed: {resp.status_code} {resp.text[:200]}")
            last = RuntimeError(f"{resp.status_code} {resp.text[:120]}")
        except requests.RequestException as exc:
            last = exc
        if i < attempts - 1:
            time.sleep(2 ** i)  # 1s, 2s, 4s
    raise RuntimeError(f"blob put {path} failed after {attempts} attempts: {last}")


def local_put(path: str, raw: bytes) -> str:
    """Write under app/public/images/ and return the path Next.js serves it at.

    No token, no network call, no retry needed -- same machine, same disk.
    Overwriting the same path is how a re-push naturally stays idempotent,
    same as blob_put's x-add-random-suffix: 0 did.
    """
    dest = LOCAL_IMAGE_DIR / path
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(raw)
    return f"/images/{path}"


def image_put(path: str, raw: bytes, token: str, content_type: str = "image/jpeg") -> str:
    if IMAGE_STORE == "blob":
        return blob_put(path, raw, token, content_type)
    return local_put(path, raw)


def make_thumb(raw: bytes) -> bytes:
    img = Image.open(io.BytesIO(raw))
    img.load()
    img = img.convert("RGB")
    img.thumbnail((THUMB_PX, THUMB_PX), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=THUMB_QUALITY, optimize=True)
    return buf.getvalue()


def collect(only_batch: str | None) -> list[dict]:
    """Manifest + trace + derived flags, one dict per product.

    Mirrors build_dashboard.collect() but keeps the local file paths so the
    caller can upload them, instead of inlining base64 thumbnails.
    """
    out: list[dict] = []
    for key, label, man_p, tr_p, lib_p, enh_p in BATCHES:
        if only_batch and key != only_batch:
            continue
        man_path = BASE / man_p
        if not man_path.exists():
            continue
        manifest = json.loads(man_path.read_text())

        for pid, rec in manifest.items():
            trace_path = BASE / tr_p / f"{pid}.json"
            trace: dict = {}
            if trace_path.exists():
                try:
                    trace = json.loads(trace_path.read_text())
                except json.JSONDecodeError:
                    trace = {}

            assess = flagmod.assess_from_trace(rec, trace)
            pv = (trace.get("gemini_verify") or {}).get("parsed_verdict") or {}
            obs_list = pv.get("observations") or []
            bi = pv.get("best_index")
            obs = obs_list[bi] if isinstance(bi, int) and 0 <= bi < len(obs_list) else {}
            norm = trace.get("normalize") or {}

            out.append({
                "id": pid,
                "name": rec.get("name", ""),
                "batch": key,
                "batch_label": label,
                "status": rec.get("status", ""),
                "severity": assess.severity,
                "flags": assess.details,
                "confidence": rec.get("confidence"),
                "tier": rec.get("tier"),
                "source": rec.get("source", ""),
                "reason": rec.get("reason", ""),
                "cat_size": norm.get("catalogue_unit_size") or "",
                "cat_pack": norm.get("catalogue_pack_count"),
                "det_size": obs.get("detected_unit_size") or "",
                "det_pack": obs.get("detected_pack_count"),
                "det_flavour": obs.get("detected_flavour") or "",
                "det_container": obs.get("detected_container") or "",
                "source_url": (trace.get("decision") or {}).get("best_candidate_url", ""),
                "_image_path": BASE / lib_p / f"{pid}.jpg",
                "_enhanced_path": (BASE / enh_p / f"{pid}.jpg") if enh_p else None,
                "has_trace": bool(trace),
            })
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Push pipeline output to the review app.")
    ap.add_argument("--batch", help="only this batch key (see BATCHES)")
    ap.add_argument("--dry-run", action="store_true", help="report, upload nothing")
    ap.add_argument("--app-url", default=os.environ.get("APP_URL", "http://localhost:3000"))
    args = ap.parse_args()

    rows = collect(args.batch)
    if not rows:
        sys.exit("no products found — has the pipeline run?")

    have_img = sum(1 for r in rows if r["_image_path"].exists())
    print(f"{len(rows)} products, {have_img} with an image")
    by_sev: dict[str, int] = {}
    for r in rows:
        by_sev[r["severity"]] = by_sev.get(r["severity"], 0) + 1
    print("severity:", by_sev)

    if args.dry_run:
        print("\n--dry-run: nothing uploaded")
        for r in rows[:5]:
            print(f"  {r['id']:>8} {r['severity']:8} {r['name'][:44]}")
        if len(rows) > 5:
            print(f"  … and {len(rows) - 5} more")
        return

    blob_token = env("BLOB_READ_WRITE_TOKEN") if IMAGE_STORE == "blob" else ""
    sync_token = env("SYNC_TOKEN")
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")

    uploaded = skipped = 0
    payload: list[dict] = []

    for i, r in enumerate(rows, 1):
        img_url = enh_url = thumb_url = None
        p: Path = r["_image_path"]
        if p.exists():
            raw = p.read_bytes()
            img_url = image_put(f"library/{r['batch']}/{r['id']}.jpg", raw, blob_token)
            thumb_url = image_put(f"thumb/{r['batch']}/{r['id']}.jpg", make_thumb(raw), blob_token)
            uploaded += 1
        else:
            skipped += 1

        ep: Path | None = r["_enhanced_path"]
        if ep is not None and ep.exists():
            enh_url = image_put(f"enhanced/{r['batch']}/{r['id']}.jpg", ep.read_bytes(), blob_token)

        payload.append({
            k: v for k, v in r.items() if not k.startswith("_") and k != "batch_label"
        } | {"image_url": img_url, "enhanced_url": enh_url, "thumb_url": thumb_url,
             "run_id": run_id})

        if i % 10 == 0 or i == len(rows):
            print(f"  [{i}/{len(rows)}] uploaded", flush=True)

    # Chunked so a large catalogue does not hit a request size limit.
    url = args.app_url.rstrip("/") + "/api/sync/run"
    for start in range(0, len(payload), 200):
        chunk = payload[start:start + 200]
        body = {"run_id": run_id, "label": f"push {run_id}", "rows": chunk}
        for attempt in range(3):
            try:
                resp = requests.post(
                    url, json=body,
                    headers={"authorization": f"Bearer {sync_token}"},
                    timeout=120,
                )
                if resp.status_code < 300:
                    break
                if resp.status_code < 500:
                    sys.exit(f"sync rejected: {resp.status_code} {resp.text[:300]}")
                err = f"{resp.status_code} {resp.text[:160]}"
            except requests.RequestException as exc:
                err = str(exc)[:160]
            if attempt == 2:
                sys.exit(f"sync failed after 3 attempts: {err}")
            time.sleep(2 ** attempt)
        print(f"  synced {start + len(chunk)}/{len(payload)}")

    print(f"\ndone — {uploaded} images uploaded, {skipped} without an image, run {run_id}")
    print(f"open {args.app_url}")


if __name__ == "__main__":
    main()
