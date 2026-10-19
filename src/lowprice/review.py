"""manifest -> review.html contact sheet. Problems float to the top — this is
the human gate before Phase 2; only files in library/ get uploaded.
"""
from __future__ import annotations

import html
import json

from . import config

_STATUS_PRIORITY = {"review": 0, "no_match": 1, "error": 2, "no_candidates": 3, "accepted": 4}

_CARD_TEMPLATE = """
<div class="card status-{status}">
  <div class="thumb">{thumb}</div>
  <div class="meta">
    <div class="name">{name}</div>
    <div class="badge badge-{status}">{status}</div>
    <div class="row">confidence: {confidence}</div>
    <div class="row">source: {source}</div>
    <div class="row reason">{reason}</div>
  </div>
</div>
"""

_PAGE_TEMPLATE = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Phase 1 review</title>
<style>
  body {{ font-family: -apple-system, Segoe UI, sans-serif; background: #f5f5f5; margin: 0; padding: 24px; }}
  h1 {{ margin: 0 0 4px; }}
  .summary {{ color: #555; margin-bottom: 20px; }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 16px; }}
  .card {{ background: #fff; border-radius: 8px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,0.15); }}
  .thumb {{ width: 100%; aspect-ratio: 1; background: #eee; display: flex; align-items: center; justify-content: center; }}
  .thumb img {{ width: 100%; height: 100%; object-fit: contain; }}
  .thumb .placeholder {{ color: #999; font-size: 13px; }}
  .meta {{ padding: 10px 12px; }}
  .name {{ font-weight: 600; font-size: 14px; margin-bottom: 6px; }}
  .badge {{ display: inline-block; font-size: 11px; font-weight: 700; text-transform: uppercase; padding: 2px 8px; border-radius: 10px; margin-bottom: 6px; }}
  .badge-accepted {{ background: #d4f4dd; color: #1a7a34; }}
  .badge-review {{ background: #fff3cd; color: #8a6500; }}
  .badge-no_match {{ background: #fde2e1; color: #a3271e; }}
  .badge-no_candidates {{ background: #eee; color: #666; }}
  .badge-error {{ background: #f1d9ff; color: #6b1fa3; }}
  .row {{ font-size: 12px; color: #444; margin-bottom: 2px; word-break: break-word; }}
  .reason {{ color: #888; font-style: italic; }}
</style>
</head>
<body>
  <h1>Phase 1 review</h1>
  <div class="summary">{summary}</div>
  <div class="grid">
    {cards}
  </div>
</body>
</html>
"""


def _sort_key(record: dict) -> tuple:
    return (_STATUS_PRIORITY.get(record.get("status", ""), 99), -(record.get("confidence") or 0))


def render_card(record: dict) -> str:
    status = record.get("status", "unknown")
    name = html.escape(record.get("name", ""))
    confidence = record.get("confidence", 0)
    source = html.escape(record.get("source", "") or "-")
    reason = html.escape(record.get("reason", "") or "")

    image_rel = record.get("image", "")
    if image_rel:
        thumb = f'<img src="{html.escape(image_rel)}" alt="{name}">'
    else:
        thumb = '<div class="placeholder">no image</div>'

    return _CARD_TEMPLATE.format(
        status=status,
        thumb=thumb,
        name=name,
        confidence=f"{confidence:.2f}" if isinstance(confidence, (int, float)) else confidence,
        source=source,
        reason=reason,
    )


def build_review_html(manifest: dict[str, dict]) -> str:
    records = sorted(manifest.values(), key=_sort_key)

    counts: dict[str, int] = {}
    for r in records:
        counts[r.get("status", "unknown")] = counts.get(r.get("status", "unknown"), 0) + 1
    summary = " | ".join(f"{status}: {count}" for status, count in sorted(counts.items()))

    cards_html = "\n".join(render_card(r) for r in records)
    return _PAGE_TEMPLATE.format(summary=html.escape(summary), cards=cards_html)


def main() -> None:
    if not config.MANIFEST_PATH.exists():
        raise SystemExit(f"no manifest found at {config.MANIFEST_PATH}; run pipeline.py first")

    manifest = json.loads(config.MANIFEST_PATH.read_text())
    page = build_review_html(manifest)
    config.REVIEW_HTML_PATH.write_text(page)
    print(f"wrote {config.REVIEW_HTML_PATH} ({len(manifest)} products)")


if __name__ == "__main__":
    main()
