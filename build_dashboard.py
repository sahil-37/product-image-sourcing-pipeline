#!/usr/bin/env python
"""Build the review dashboard: one self-contained HTML page per batch set.

Reads the manifests and per-product traces produced by run_food_pilot.py,
derives flags with flags.py, and emits a single HTML file with thumbnails
inlined as data URIs so it can be shared as one artifact with no asset hosting.

Usage:
    .venv/bin/python build_dashboard.py                 # all known batches
    .venv/bin/python build_dashboard.py --out review.html

Read-only by design: it reports what the pipeline decided and what was not
verified. Reviewer overrides are a separate step (see --overrides note in the
page footer).
"""
from __future__ import annotations

import argparse
import base64
import html
import io
import json
from pathlib import Path

from PIL import Image

import flags as flagmod

BASE = Path(__file__).resolve().parent

# (label, manifest path, traces dir, library dir)
BATCHES = [
    ("Batch 1 · food pilot", "work/manifest_food_pilot.json", "work/traces_food_pilot", "library_food_pilot"),
    ("Batch 2 · spread sample", "work/manifest_food_batch2.json", "work/traces_food_batch2", "library_food_batch2"),
    ("Batch 3 · adversarial", "work/manifest_adversarial.json", "work/traces_adversarial", "library_adversarial"),
]

THUMB_PX = 150
THUMB_QUALITY = 62


def make_thumb(path: Path) -> str:
    """Small JPEG data URI, or '' when the image is missing/unreadable."""
    try:
        img = Image.open(path)
        img.load()
    except Exception:
        return ""
    img = img.convert("RGB")
    img.thumbnail((THUMB_PX, THUMB_PX), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=THUMB_QUALITY, optimize=True)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def collect() -> list[dict]:
    out = []
    for label, man_p, tr_p, lib_p in BATCHES:
        man_path = BASE / man_p
        if not man_path.exists():
            continue
        manifest = json.loads(man_path.read_text())
        for pid, rec in manifest.items():
            trace_path = BASE / tr_p / f"{pid}.json"
            trace = {}
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

            img_path = BASE / lib_p / f"{pid}.jpg"
            out.append({
                "batch": label,
                "id": pid,
                "name": rec.get("name", ""),
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
                "url": (trace.get("decision") or {}).get("best_candidate_url", ""),
                "thumb": make_thumb(img_path),
            })
    return out


SEV_ORDER = {"blunder": 0, "flagged": 1, "clean": 2}


def render(rows: list[dict]) -> str:
    rows = sorted(rows, key=lambda r: (SEV_ORDER.get(r["severity"], 3), r["name"]))
    counts = {"blunder": 0, "flagged": 0, "clean": 0}
    for r in rows:
        counts[r["severity"]] = counts.get(r["severity"], 0) + 1

    flag_counts: dict[str, int] = {}
    for r in rows:
        for f in r["flags"]:
            flag_counts[f["code"]] = flag_counts.get(f["code"], 0) + 1

    cards = "\n".join(render_card(r) for r in rows)
    chips = "\n".join(
        f'<button class="chip" data-flag="{html.escape(code)}">'
        f'{html.escape(flagmod.FLAG_DEFS.get(code, (code, ""))[0])}'
        f'<span class="chip-n">{n}</span></button>'
        for code, n in sorted(flag_counts.items(), key=lambda kv: -kv[1])
    )
    total = len(rows)
    pct_clean = round(counts["clean"] / total * 100) if total else 0

    return PAGE.format(
        total=total,
        clean=counts["clean"],
        flagged=counts["flagged"],
        blunder=counts["blunder"],
        pct_clean=pct_clean,
        chips=chips,
        cards=cards,
    )


def render_card(r: dict) -> str:
    e = html.escape
    thumb = (
        f'<img src="{r["thumb"]}" alt="{e(r["name"])}" loading="lazy">'
        if r["thumb"] else '<div class="noimg">no image</div>'
    )
    flag_html = "".join(
        f'<span class="flag" title="{e(f["why"])}">{e(f["label"])}</span>'
        for f in r["flags"]
    )
    conf = r["confidence"]
    conf_s = f"{conf:.2f}" if isinstance(conf, (int, float)) else "—"
    pack_row = f'{r["cat_pack"] if r["cat_pack"] is not None else "—"} → {r["det_pack"] if r["det_pack"] is not None else "—"}'
    size_row = f'{e(r["cat_size"]) or "—"} → {e(r["det_size"]) or "—"}'
    codes = " ".join(f["code"] for f in r["flags"])
    src = e(r["source"])
    link = (
        f'<a class="src" href="{e(r["url"])}" target="_blank" rel="noopener">{src or "source"} ↗</a>'
        if r["url"] else f'<span class="src">{src or "—"}</span>'
    )
    return f"""
<article class="card sev-{r['severity']}" data-sev="{r['severity']}" data-flags="{e(codes)}" data-name="{e(r['name'].lower())}">
  <div class="thumb">{thumb}</div>
  <div class="body">
    <div class="card-top">
      <h3>{e(r['name'])}</h3>
      <span class="sev sev-pill-{r['severity']}">{r['severity']}</span>
    </div>
    <dl class="meta">
      <div><dt>id</dt><dd>{e(r['id'])}</dd></div>
      <div><dt>pack</dt><dd>{pack_row}</dd></div>
      <div><dt>size</dt><dd>{size_row}</dd></div>
      <div><dt>tier</dt><dd>{r['tier'] if r['tier'] is not None else '—'}</dd></div>
      <div><dt>conf</dt><dd>{conf_s}</dd></div>
    </dl>
    <div class="detected">{e(r['det_flavour'])}{' · ' + e(r['det_container']) if r['det_container'] else ''}</div>
    <div class="flags">{flag_html or '<span class="none">no flags</span>'}</div>
    <div class="foot">{link}<span class="batch">{e(r['batch'])}</span></div>
  </div>
</article>"""


PAGE = """<title>Image Review Queue</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Newsreader:opsz,wght@6..72,500;6..72,600&family=Public+Sans:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500;600&display=swap">
<style>
:root{{
  --ink:#15181b; --ink-2:#464d55; --ink-3:#727a84;
  --rule:#e0e1dd; --rule-2:#eeefeb;
  --ground:#f6f5f1; --card:#fffffe;
  --accent:#2f5d50;
  --clean:#2f6b4a; --clean-bg:#e4f0e8;
  --flagged:#8a6512; --flagged-bg:#f8efd8;
  --blunder:#a32b1c; --blunder-bg:#f7e5e1;
  --shadow:0 1px 2px rgba(21,24,27,.05),0 10px 26px -14px rgba(21,24,27,.18);
  --display:"Newsreader",Georgia,serif;
  --body:"Public Sans",-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
  --mono:"IBM Plex Mono",ui-monospace,Menlo,monospace;
}}
@media (prefers-color-scheme:dark){{
  :root:not([data-theme="light"]){{
    --ink:#ebe9e3; --ink-2:#b0aca3; --ink-3:#83807a;
    --rule:#32353a; --rule-2:#25282b;
    --ground:#121416; --card:#191c1f;
    --accent:#7fbfa9;
    --clean:#71b98d; --clean-bg:#1a2e22;
    --flagged:#d9ae4e; --flagged-bg:#332a12;
    --blunder:#e8836e; --blunder-bg:#3a1f19;
    --shadow:0 1px 2px rgba(0,0,0,.4),0 10px 26px -14px rgba(0,0,0,.65);
  }}
}}
:root[data-theme="dark"]{{
  --ink:#ebe9e3; --ink-2:#b0aca3; --ink-3:#83807a;
  --rule:#32353a; --rule-2:#25282b;
  --ground:#121416; --card:#191c1f;
  --accent:#7fbfa9;
  --clean:#71b98d; --clean-bg:#1a2e22;
  --flagged:#d9ae4e; --flagged-bg:#332a12;
  --blunder:#e8836e; --blunder-bg:#3a1f19;
  --shadow:0 1px 2px rgba(0,0,0,.4),0 10px 26px -14px rgba(0,0,0,.65);
}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--ground);color:var(--ink);font-family:var(--body);font-size:15px;line-height:1.55;-webkit-font-smoothing:antialiased}}
.wrap{{max-width:1240px;margin:0 auto;padding:0 28px}}
@media(max-width:640px){{.wrap{{padding:0 16px}}}}

header{{padding:44px 0 26px;border-bottom:1px solid var(--rule)}}
.kicker{{font-family:var(--mono);font-size:11px;letter-spacing:.14em;text-transform:uppercase;color:var(--accent);margin:0 0 12px}}
h1{{font-family:var(--display);font-weight:600;font-size:clamp(28px,4vw,40px);line-height:1.1;letter-spacing:-.015em;margin:0 0 10px}}
.lede{{color:var(--ink-2);max-width:66ch;margin:0;font-size:16px}}

.stats{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:1px;background:var(--rule);border:1px solid var(--rule);border-radius:3px;overflow:hidden;margin:26px 0 0}}
.stat{{background:var(--card);padding:15px 18px}}
.stat-n{{font-family:var(--display);font-size:27px;font-weight:600;line-height:1;font-variant-numeric:tabular-nums}}
.stat-l{{font-family:var(--mono);font-size:10px;letter-spacing:.1em;text-transform:uppercase;color:var(--ink-3);margin-top:7px}}
.stat.is-clean .stat-n{{color:var(--clean)}}
.stat.is-flagged .stat-n{{color:var(--flagged)}}
.stat.is-blunder .stat-n{{color:var(--blunder)}}

.controls{{position:sticky;top:0;z-index:20;background:var(--ground);border-bottom:1px solid var(--rule);padding:14px 0;margin-bottom:22px}}
.crow{{display:flex;flex-wrap:wrap;gap:8px;align-items:center}}
.search{{flex:1;min-width:190px;font-family:var(--body);font-size:14px;padding:8px 11px;border:1px solid var(--rule);border-radius:3px;background:var(--card);color:var(--ink)}}
.search:focus{{outline:2px solid var(--accent);outline-offset:1px}}
.chip,.seg{{font-family:var(--mono);font-size:11px;letter-spacing:.04em;padding:7px 11px;border:1px solid var(--rule);background:var(--card);color:var(--ink-2);border-radius:3px;cursor:pointer;display:inline-flex;align-items:center;gap:7px}}
.chip:hover,.seg:hover{{border-color:var(--accent);color:var(--ink)}}
.chip[aria-pressed="true"],.seg[aria-pressed="true"]{{background:var(--accent);border-color:var(--accent);color:#fff}}
:root[data-theme="dark"] .chip[aria-pressed="true"],:root[data-theme="dark"] .seg[aria-pressed="true"]{{color:#10201b}}
@media (prefers-color-scheme:dark){{:root:not([data-theme="light"]) .chip[aria-pressed="true"],:root:not([data-theme="light"]) .seg[aria-pressed="true"]{{color:#10201b}}}}
.chip-n{{font-variant-numeric:tabular-nums;opacity:.72}}
.chips{{display:flex;flex-wrap:wrap;gap:7px;margin-top:9px}}
.count{{font-family:var(--mono);font-size:11px;color:var(--ink-3);margin-left:auto}}

.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:14px;padding-bottom:44px}}
.card{{background:var(--card);border:1px solid var(--rule);border-radius:4px;box-shadow:var(--shadow);display:grid;grid-template-columns:112px 1fr;overflow:hidden;border-left:3px solid var(--rule)}}
.card.sev-clean{{border-left-color:var(--clean)}}
.card.sev-flagged{{border-left-color:var(--flagged)}}
.card.sev-blunder{{border-left-color:var(--blunder)}}
.thumb{{background:#fff;display:flex;align-items:center;justify-content:center;padding:9px;border-right:1px solid var(--rule-2)}}
.thumb img{{max-width:100%;height:auto;display:block}}
.noimg{{font-family:var(--mono);font-size:10px;color:#999;text-align:center}}
.body{{padding:13px 15px;min-width:0}}
.card-top{{display:flex;gap:9px;align-items:flex-start;justify-content:space-between}}
h3{{font-family:var(--body);font-size:14px;font-weight:600;margin:0;line-height:1.3;word-break:break-word}}
.sev{{font-family:var(--mono);font-size:9px;letter-spacing:.09em;text-transform:uppercase;padding:3px 7px;border-radius:2px;white-space:nowrap}}
.sev-pill-clean{{background:var(--clean-bg);color:var(--clean)}}
.sev-pill-flagged{{background:var(--flagged-bg);color:var(--flagged)}}
.sev-pill-blunder{{background:var(--blunder-bg);color:var(--blunder)}}
.meta{{display:flex;flex-wrap:wrap;gap:4px 14px;margin:9px 0 0}}
.meta div{{display:flex;gap:5px;align-items:baseline}}
.meta dt{{font-family:var(--mono);font-size:9.5px;letter-spacing:.07em;text-transform:uppercase;color:var(--ink-3)}}
.meta dd{{margin:0;font-family:var(--mono);font-size:11.5px;color:var(--ink-2);font-variant-numeric:tabular-nums}}
.detected{{font-size:12.5px;color:var(--ink-2);margin-top:8px;font-style:italic}}
.flags{{display:flex;flex-wrap:wrap;gap:5px;margin-top:10px}}
.flag{{font-size:11px;background:var(--flagged-bg);color:var(--flagged);padding:3px 7px;border-radius:2px;cursor:help}}
.none{{font-family:var(--mono);font-size:10.5px;color:var(--ink-3)}}
.foot{{display:flex;gap:10px;align-items:baseline;justify-content:space-between;margin-top:11px;padding-top:9px;border-top:1px solid var(--rule-2)}}
.src{{font-family:var(--mono);font-size:10.5px;color:var(--ink-3);text-decoration:none;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
a.src:hover{{color:var(--accent)}}
.batch{{font-family:var(--mono);font-size:9.5px;color:var(--ink-3);white-space:nowrap}}
.empty{{padding:50px 0;text-align:center;color:var(--ink-3);font-family:var(--mono);font-size:12px}}
footer{{padding:26px 0 52px;border-top:1px solid var(--rule);color:var(--ink-3);font-size:13px}}
footer p{{max-width:74ch;margin:0 0 8px}}
footer strong{{color:var(--ink-2)}}
</style>

<div class="wrap">
<header>
  <p class="kicker">Phase 1 · automated product imagery</p>
  <h1>Image Review Queue</h1>
  <p class="lede">Every product the pipeline sourced an image for, with the checks that could not be completed. A flag never blocks an upload &mdash; it marks what a human should confirm.</p>
  <div class="stats">
    <div class="stat"><div class="stat-n">{total}</div><div class="stat-l">Products</div></div>
    <div class="stat is-clean"><div class="stat-n">{clean}</div><div class="stat-l">Clean · {pct_clean}%</div></div>
    <div class="stat is-flagged"><div class="stat-n">{flagged}</div><div class="stat-l">Flagged for review</div></div>
    <div class="stat is-blunder"><div class="stat-n">{blunder}</div><div class="stat-l">No usable image</div></div>
  </div>
</header>

<div class="controls">
  <div class="crow">
    <input class="search" id="q" type="search" placeholder="Filter by product name…" aria-label="Filter by product name">
    <button class="seg" data-sev="all" aria-pressed="true">All</button>
    <button class="seg" data-sev="blunder" aria-pressed="false">No image</button>
    <button class="seg" data-sev="flagged" aria-pressed="false">Flagged</button>
    <button class="seg" data-sev="clean" aria-pressed="false">Clean</button>
    <span class="count" id="count"></span>
  </div>
  <div class="chips">{chips}</div>
</div>

<div class="grid" id="grid">
{cards}
</div>
<div class="empty" id="empty" hidden>No products match these filters.</div>

<footer>
  <p><strong>How to read this.</strong> Flags are derived from the verifier's own notes on the chosen photo &mdash; they record what could not be confirmed, not what is wrong. A flagged product is still the right product as far as every check could tell; it ships.</p>
  <p><strong>Known limitation.</strong> Flags can only be as good as the underlying observations. Spot-checking found cases where the verifier reported a clean studio shot for a photo that carried a store watermark, so a <em>clean</em> row is not a guarantee &mdash; it means nothing was detected, not that nothing is there.</p>
  <p><strong>Overrides.</strong> This page is read-only. To replace an image, supply the product id and a direct image URL to the pipeline operator; a future revision can capture that here and feed it back into the next run.</p>
</footer>
</div>

<script>
(function(){{
  var grid=document.getElementById('grid'), q=document.getElementById('q'),
      countEl=document.getElementById('count'), empty=document.getElementById('empty'),
      cards=Array.prototype.slice.call(grid.querySelectorAll('.card')),
      segs=Array.prototype.slice.call(document.querySelectorAll('.seg')),
      chips=Array.prototype.slice.call(document.querySelectorAll('.chip')),
      sev='all', active={{}};

  function apply(){{
    var term=(q.value||'').trim().toLowerCase(), shown=0,
        want=Object.keys(active).filter(function(k){{return active[k];}});
    cards.forEach(function(c){{
      var ok=true;
      if(sev!=='all' && c.dataset.sev!==sev) ok=false;
      if(ok && term && c.dataset.name.indexOf(term)<0) ok=false;
      if(ok && want.length){{
        var have=(c.dataset.flags||'').split(' ');
        ok=want.every(function(f){{return have.indexOf(f)>=0;}});
      }}
      c.hidden=!ok;
      if(ok) shown++;
    }});
    countEl.textContent=shown+' of '+cards.length;
    empty.hidden=shown>0;
  }}

  segs.forEach(function(b){{
    b.addEventListener('click',function(){{
      sev=b.dataset.sev;
      segs.forEach(function(o){{o.setAttribute('aria-pressed', String(o===b));}});
      apply();
    }});
  }});
  chips.forEach(function(b){{
    b.addEventListener('click',function(){{
      var f=b.dataset.flag;
      active[f]=!active[f];
      b.setAttribute('aria-pressed', String(!!active[f]));
      apply();
    }});
  }});
  q.addEventListener('input',apply);
  apply();
}})();
</script>
"""


def main() -> None:
    ap = argparse.ArgumentParser(description="Build the image review dashboard.")
    ap.add_argument("--out", default="review_dashboard.html", help="output HTML path")
    args = ap.parse_args()

    rows = collect()
    if not rows:
        raise SystemExit("no manifests found — run the pipeline first")
    out = BASE / args.out
    out.write_text(render(rows))
    size_mb = out.stat().st_size / 1e6
    print(f"wrote {out} — {len(rows)} products, {size_mb:.1f} MB")


if __name__ == "__main__":
    main()
