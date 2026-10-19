# Wholesale product-image automation

Replaces the manual "search Google Images, hand-pick, download, upload to OMS"
workflow with two decoupled programs and a human review gate between them:

```
CSV export ─▶ [PHASE 1: build verified image library] ─▶ review.html (human gate) ─▶ [PHASE 2: OMS uploader]
```

Phase 1 is implemented here. Phase 2 (Playwright OMS uploader) is scaffolded
separately once you've recorded your OMS's selectors via `playwright codegen`
(see below) — see "Phase 2" section for what's needed to build it.

The hard problem is **product-image confidence**, not downloading images:
never attach the wrong flavour, size, pack count, or old packaging to a
product. Every design choice here is in service of that.

## Quick start (no credentials needed)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

python pipeline.py --mock --limit 5
python review.py
open review.html   # or just open the file in a browser
```

This runs entirely offline against the bundled `work/mock_images/` fixtures
and the sample `products.csv`, producing `library/*.jpg` and `review.html`.

## Real run

1. Copy `.env.example` to `.env` and fill in credentials (see "Credentials"
   below).
2. Put your product export at `drinklist.xlsx` (the configured default) or pass
   `--csv path/to/file.csv`. **Both `.xlsx` and `.csv` are supported.** Columns
   (all configurable in `config.py`):
   - `Description` (`NAME_COL`) — **required**, the full product description
   - `Item Number` (`ID_COL`) — optional, used for filenames; falls back to a
     slug of the description
   - `Pack` (`PACK_COL`) — optional, numeric pack count; feeds the search query
     and the pack-count gate
   - `Size / UOM` (`SIZE_COL`) — optional, per-unit size (`1 LITER`, `12 OZ`);
     feeds the size gate
3. Run:

```bash
python pipeline.py --limit 20   # staged test on the first 20 rows
python pipeline.py              # full run — auto-resumes if interrupted
python pipeline.py --redo       # ignore the existing manifest, reprocess everything
python pipeline.py --limit 5 -v # staged test with full per-stage logging to stdout
python review.py                # regenerate review.html from the manifest
```

**Run a staged subset first.** Each product costs one Serper search plus one
Gemini vision call, so start with `--limit` and inspect `review.html` and
`work/traces/` before committing to the full export.

The pipeline writes `work/manifest.json` after **every** product, so Ctrl-C
or a crash loses nothing — re-running picks up where it left off. The same
command is used for the one-time backfill and for ongoing weekly top-ups
(new rows in the CSV get processed; already-resolved rows are skipped).

Open `review.html` after each run. Cards are sorted so `review`, `no_match`,
and `error` float to the top — that's the only place you need to look.
**Only images in `library/` (i.e. `accepted` or `review`-tier products you've
manually approved) should ever reach Phase 2.**

## How Phase 1 works

For each product, in this exact order:

1. **Normalize** (`normalize.py`) — expand catalogue shorthand (`chy`→`cherry`,
   `pk`→`pack`, ...), extract size (`330ml`) and pack (`24 pack`, `x24`)
   tokens, build a cleaned search query. The **original product name is never
   mutated** — it's the join key used for OMS lookup and filenames.
2. **Search** (`search.py`) — Serper (`google.serper.dev/images`), up to
   `CANDIDATES_PER_PRODUCT` candidates.
3. **Pre-filter** (`process.py`, free) — drop known watermark/stock-photo
   domains, drop images that are too small or too elongated.
4. **Score + rank** (`normalize.score_candidate`, free, **text only**) — token
   overlap with the product name (matches in the candidate's `title` count at
   full weight; `snippet`/`source`/`url` at `W_SECONDARY_FIELD_FRAC`, since
   `title` is the field carrying brand + variant + size + pack), size/pack
   match bonuses, trusted/junk domain bonus/penalty, a penalty when a
   candidate's text suggests a variety/mixed pack but the product isn't one,
   image area as a tiny tie-breaker. Only the top `MAX_VERIFY` (default 5)
   candidates ever reach the paid vision step.
5. **Verify** (`verify.py`) — send **all `MAX_VERIFY` shortlisted candidates
   together** in a single call to `GEMINI_VISION_MODEL` (default
   `gemini-3.1-flash-lite`) at temperature 0, along with the **original,
   unmodified** product description and the catalogue's own `Pack` and
   `Size / UOM` values.

   This step emulates the manual process — look at the full description, look
   at the photos, skip anything watermarked, visually match flavour /
   container / size / pack count — and forces it into an explicit ordered
   sequence so a lighter model can't skip a check:

   - **Step 1 OBSERVE** — per candidate, record what the image actually shows:
     `detected_flavour`, `detected_container`, `detected_unit_size`,
     `shows_individual`, `shows_pack`, `detected_pack_count`, `label_legible`,
     `is_clean`, `has_overlay`. Attributes are read off the packaging in the
     photo — never pre-extracted by a separate call.
   - **Step 2 HARD GATES** — `flavour_ok` (strictest; a different flavour is
     never acceptable however similar), `container_ok`, `size_ok` (after unit
     conversion), `packcount_ok` (a pack showing the *wrong* count fails; a
     single unit asserts no count), `clean_ok`. Failing any one disqualifies.
   - **Step 3 RANK** eligible candidates by representation tier: **1** = the
     individual unit *and* the pack together, **2** = clean single-unit shot,
     **3** = pack only with legible labelling.
   - **Step 4 CHOOSE** the best (lowest) tier, or `null` if nothing is eligible.
   - **Step 5 SELF-CONSISTENCY** — re-read the Step 1 notes for the chosen
     candidate and return `null` instead if any detected attribute conflicts.

   The reply is a strict Pydantic `response_schema`, so the full per-candidate
   observation/gate/tier record comes back well-typed and lands in the trace.
6. **Deterministic gate** (`gate.py`, free, **no model involved**) — re-check
   the model's chosen candidate against the catalogue's real `Pack` and
   `Size / UOM` values in code, and revoke the choice (`best_index` → `null`)
   on a contradiction: a pack showing a different count, a unit size that
   doesn't match after conversion (1 L = 1000 mL = 33.8 OZ), a candidate the
   model itself flagged as failing a gate, or one carrying a watermark. This
   makes the pack/size guarantee independent of the model getting it right on
   any given call — same "deterministic core, LLM only for judgment" principle
   used elsewhere in the pipeline. Missing or unreadable values are treated as
   *no evidence* and pass; the gate revokes on contradiction, it does not
   demand proof.
7. **Tier** — confidence sets the floor (≥ `AUTO_ACCEPT` 0.80 → `accepted`;
   ≥ `REVIEW_LOW` 0.50 → `review`; below → `no_match`) and the representation
   tier caps the ceiling via `TIER_MAX_STATUS`, so a tier-3 "pack only" shot
   always lands in `review` for a human glance rather than auto-accepting.
   Nothing below threshold is ever saved.
8. **Resize** (`process.py`) — accepted/review images are converted to RGB and
   fit onto a centered white `OUTPUT_SIZE`×`OUTPUT_SIZE` (1000px) canvas, saved
   as `library/<id>.jpg`. Every image lands as JPEG regardless of source
   format.

All thresholds, weights, domain lists, and model names live in `config.py` —
nothing is hardcoded in the logic modules.

## Logging / tracing

Every run writes a full per-product trace to `work/traces/<id>.json`, whether
or not `--verbose` is passed: the normalized query and catalogue attributes,
every raw search candidate, prefilter keeps/drops, the full ranked score list,
which candidates were sent to the model, download failures, the **exact system
and user prompts**, the **raw model response text**, the parsed per-candidate
observations/gates/tiers, the deterministic-gate outcome, and the final
decision.

`-v` / `--verbose` additionally prints the same stages to stdout live as the
run proceeds — useful on a small `--limit` run, noisy on a large one.

## Phase 1.5 — background regeneration + logo watermark (optional)

A separate, **opt-in** stage (`enhance.py`, run via `pipeline.py --enhance`)
that takes already-accepted/review `library/*.jpg` images and produces
studio-style versions in `library_enhanced/`, without ever touching `library/`
itself:

1. **Segment** (`rembg`) — cut the product out of the accepted photo. The
   product pixels are never sent to a generative model.
2. **Generate background** (Gemini `GEMINI_IMAGE_MODEL`, default
   `gemini-3.1-flash-lite-image`, via `client.interactions.create`) —
   prompted for an empty studio background only ("do not include any
   product"), with light flavour-driven set dressing pulled from the product
   name via `FLAVOR_BACKGROUND_HINTS` in `config.py` (e.g. a lime product gets
   blurred lime wedges in the background).
3. **Composite** (Pillow) — the original, untouched product cutout is pasted
   back on top of the generated background programmatically. The model never
   sees or regenerates the product itself — only empty background pixels.
4. **Watermark** (Pillow) — your logo (`logo.png` in the project root) is
   composited at a fixed position/size/opacity (`WATERMARK_POSITION`,
   `WATERMARK_WIDTH_FRAC`, `WATERMARK_OPACITY` in `config.py`). Placement is
   deterministic math, never left to a generative model — generative models
   are unreliable at pixel-exact logo placement.

This costs real per-image money (one Gemini vision-selection call is already
paid for in Phase 1; this adds one image-generation call per enhanced
product) and regenerates pixels around (not of) third-party product
photography, so it's off by default:

```bash
python pipeline.py --enhance             # enhance all accepted/review products
python pipeline.py --enhance --limit 5   # staged test
python pipeline.py --enhance --redo      # re-enhance even if already done
```

Requires `GEMINI_API_KEY` and a `logo.png` in the project root.

## Credentials

### Serper
1. Get an API key at https://serper.dev.
2. Set in `.env`:
   ```
   SERPER_API_KEY=your-api-key
   ```
   Pricing is pay-as-you-go per query — check current rates on the Serper
   dashboard; a 1k–10k product backfill is a one-time, low-dollar cost.

   **Never paste a real key into chat, a commit, or any tracked file.** If a
   key is ever exposed, rotate it immediately from the Serper dashboard.

### Gemini
```
GEMINI_API_KEY=your-api-key
GEMINI_VISION_MODEL=gemini-3.1-flash-lite
GEMINI_IMAGE_MODEL=gemini-3.1-flash-lite-image
```
Used for the Phase 1 candidate-selection step (`verify.py`) and, if you run
`--enhance`, the Phase 1.5 background-generation step (`enhance.py`).

Never commit `.env` or real keys — `.env.example` is the template; real
credentials come only from environment variables.

## Testing without credentials

```bash
python pipeline.py --mock --limit 5
```

`--mock` uses `search.py`'s mock mode (reads `work/mock_images/index.json`,
filtered by simple word overlap against the query so an off-catalogue product
correctly yields zero candidates) and `verify.py`'s mock mode (a fixed
"accepted" verdict). This proves the plumbing — CSV parsing, scoring, tiering,
resize-to-square, manifest writing, resumability, and review page generation —
without spending a cent.

To sanity-check the text scorer directly:

```bash
python3 -c "
from normalize import normalize_name
p = normalize_name('COCA COLA ZERO CHY 330ML CN 24')
print(p.original, '->', p.query, p.sizes, p.packs)
"
```

## Phase 2 — OMS uploader

Because the OMS is UI-only (no API, no bulk import, no image-URL field), Phase 2
is Playwright browser automation.

**The framework is built. It needs your OMS's selectors before it can run** —
those depend on your system's DOM and can't be guessed.

### One-time setup

1. Put your OMS URL in `.env`:

   ```
   OMS_URL=https://your-oms.example.com
   ```

2. Record one upload by hand:

   ```bash
   python -m playwright codegen "$OMS_URL"
   ```

   Log in, search a product by name, open it, upload an image, save. Copy the
   selectors from the generated script into **`oms_selectors.py`** — that is the
   only file you should need to edit. Instructions are at the top of it.

3. Save a browser session (handles SSO/MFA by letting you log in by hand):

   ```bash
   python oms_uploader.py --login
   ```

### Running

```bash
python oms_uploader.py --dry-run --limit 1   # prove the selectors work
python oms_uploader.py --limit 5             # staged rollout
python oms_uploader.py                       # the rest
```

Useful flags: `--headed` to watch it work, `--redo` to re-upload,
`--include-review` to also push `status=review` items (off by default).

### Safety behavior (enforced in code, not left to the operator)

- **Only `accepted` products are eligible.** `review` items need a human pass in
  `review.html` first, and are skipped unless you pass `--include-review`.
- **Ambiguous OMS name matches are never guessed.** 0 results or >1 results →
  the product is recorded as failed and skipped. Verified against a mock page:
  proceeds only on exactly one match.
- **`--dry-run` suppresses exactly one thing** — the final save click. Everything
  else (search, open, attach file) still runs, so it genuinely exercises the
  selectors.
- **Resumable.** Every outcome is written to `work/upload_manifest.json`
  immediately (atomic replace), so a crash or Ctrl-C resumes where it stopped
  and already-uploaded products are skipped.
- **One failure never aborts the run** — it's logged, recorded, stepped over.
- **Only the image field is touched.** No other product data is edited.
- **Prefers the watermarked image** (`library_enhanced/<id>.jpg`), falling back
  to the plain `library/<id>.jpg`.
- **Confirms the save.** Set `SAVE_SUCCESS_MARKER` so a silent validation
  failure isn't recorded as a success.

## Non-goals (by design)

- The two phases are not merged into one script — sourcing errors are
  judgment errors to catch before anything is live; upload errors are
  brittle/mechanical and need independent retry.
- No visual/pixel scoring before the vision step — text signals only for
  ranking; the vision model is the only place that looks at pixels.
- No watermark removal — watermark risk is avoided by filtering source
  domains, not by editing images.
- No databases, agent frameworks, or extra LLM calls where a rule suffices.
