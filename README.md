# Product Image Sourcing Pipeline

Finds a correct product photo for every item in a wholesale distributor's
catalogue, puts each pick in front of a human, and publishes the approved
images to the distributor's order-management system (OMS).

Built for a US wholesale distributor with a catalogue of ~5,700 SKUs and no
product imagery. In production it sourced and verified images for 4,863 of
5,738 products (85%) and published 4,185 of them to the live storefront, with
every image checked before it went out.

The hard problem is **image confidence**, not downloading images. A search for
"COKE 20OZ" returns 2-liter bottles, 12-packs, old labels, and Diet Coke. The
system has to refuse the wrong flavour, size, pack count, or packaging, and
say *why*, rather than attach the closest-looking picture.

## How it works

```
 catalogue.xlsx
       │
       ▼
┌──────────────────────────┐     ┌──────────────────────────┐     ┌──────────────────────────┐
│ 1. Source                │     │ 2. Review                │     │ 3. Publish               │
│ search → vision verify → │ ──▶ │ Next.js app on Postgres  │ ──▶ │ Playwright drives the    │
│ deterministic gate →     │     │ flags, filters, bulk     │     │ OMS: per-product worker  │
│ watermark                │     │ select, ZIP export       │     │ or 30-at-a-time bulk     │
└──────────────────────────┘     └──────────────────────────┘     └──────────────────────────┘
```

**1. Source** (`src/lowprice/`). For each catalogue row:

1. **Normalize** the description into a search query (strip vendor codes and
   bookkeeping marks from the query only; the model always sees the original).
2. **Search** with the Serper image API and keep every result.
3. **Filter** out stock-photo domains. Resolution is deliberately not a
   filter: a small correct photo beats a large wrong one.
4. **Verify** all candidates in one Gemini vision call. The model records what
   it observes on each photo (brand, flavour, size, pack count, container),
   applies hard gates, ranks, and either picks one or declines.
5. **Gate** the pick again in plain code against the catalogue's own size and
   pack count. Code can revoke the model's choice on a contradiction; it never
   accepts on missing evidence.
6. **Watermark** the approved image with the distributor's logo.

Every decision is classified as `accepted`, `review`, or `no_match`, with
machine-readable flags explaining why (for example *pack shown for a
single-unit row*, *size not legible*, *not official product photography*).
The full prompt, per-candidate observations and gate results are kept as a
JSON trace per product.

**2. Review** (`app/`). A Next.js app backed by Postgres (Neon, Drizzle ORM)
where the client browses every product with its chosen image, filters by
status, flag severity, category and upload state, overrides picks, and
selects products in bulk to download or publish.

**3. Publish** (`worker.py`, `bulk_upload_woopsa.py`). The OMS has no API, so
publishing drives its admin UI with Playwright:

- `worker.py` polls the app for queued uploads and attaches images one
  product at a time.
- `bulk_upload_woopsa.py` uses the OMS's own bulk-upload dialog to send 30
  images per request, about 13 seconds per batch of 30.

## Engineering decisions worth noting

- **Two independent checks before anything is accepted.** The vision model
  and a deterministic gate must agree; the gate only ever revokes.
- **Ambiguity is never guessed.** If an OMS search returns zero or several
  matches for an item number, the product is skipped and reported, not
  uploaded to the closest match.
- **Writes only the distributor's own image slot.** The OMS also has a shared
  master slot other distributors read; the uploader verifies which slot is
  active before every write and refuses otherwise.
- **Success is verified from the server's response, not the UI.** The bulk
  uploader marks an image as published only when the OMS's response names it
  in its per-file success list.
- **Resumable everywhere.** Sourcing runs, uploads and audit logs are written
  after every item, so a crash or an expired session resumes where it
  stopped.
- **Hung requests can't stall a run.** Every vision call has a hard deadline,
  added after a dead connection froze a production run for three hours.
- **Generation was tried and rejected.** AI background replacement is
  implemented in `enhance.py` but off: across 34 test products it was
  inconsistent and often overpowered the product. Real photos on white won.

## Tech stack

| Area | Tools |
|---|---|
| Pipeline | Python 3.11+, Gemini vision (`google-genai`), Serper image search, Pillow, Pydantic |
| Review app | Next.js 15, React 19, TypeScript, Drizzle ORM, Neon Postgres |
| Publishing | Playwright (persistent browser profile, no stored credentials) |
| Tests | pytest, Vitest, ruff |

## Run it

```bash
make install                      # .venv, dependencies, Chromium
cp .env.example .env              # add SERPER_API_KEY and GEMINI_API_KEY
INPUT_CSV=data/sample_catalogue.csv make source LIMIT=3
make review                       # static review page for the picks
make test
```

The real catalogue and logo are confidential; `data/` holds a ten-row made-up
sample and a placeholder logo instead (see [`data/README.md`](data/README.md)).
Each row costs one search call and one vision call, so start with a small
`LIMIT`.

The review app needs a Postgres `DATABASE_URL` in `app/.env.local` (see
`app/.env.local.example`), then `npm install && npm run dev` in `app/`.

Publishing needs an account on the target OMS, so it won't run as-is; the
OMS-specific selectors are isolated in
[`oms_selectors.py`](src/lowprice/phase2/oms_selectors.py).

## Layout

```
src/lowprice/
    config.py             every threshold, model name and path
    pipeline.py           sourcing driver
    phase1/               normalize · search · process · verify · gate
    phase2/               OMS uploader and selectors
    watermark.py          logo compositing
    review.py             static review page
app/                      Next.js review app
worker.py                 per-product upload worker
bulk_upload_woopsa.py     30-at-a-time bulk uploader
run_source_job.py         sourcing runs launched from the app
push_to_web.py            syncs results to the app database
tests/                    pytest suite
docs/reference.md         detailed design notes
```
