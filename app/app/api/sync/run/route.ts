import { sql } from "drizzle-orm";
import { NextRequest, NextResponse } from "next/server";

import { db } from "@/lib/db";
import { products, runs } from "@/lib/schema";

/** `excluded.<col>` — the value the conflicting INSERT would have written. */
function sqlExcluded(col: string) {
  return sql.raw(`excluded.${col}`);
}

export const dynamic = "force-dynamic";
export const maxDuration = 60;

type Row = {
  id: string;
  name: string;
  batch: string;
  status: string;
  severity: string;
  flags: unknown;
  confidence: number | null;
  tier: number | null;
  source: string | null;
  reason: string | null;
  cat_size: string | null;
  cat_pack: number | null;
  det_size: string | null;
  det_pack: number | null;
  det_flavour: string | null;
  det_container: string | null;
  source_url: string | null;
  image_url: string | null;
  enhanced_url: string | null;
  thumb_url: string | null;
  has_trace: boolean;
  run_id: string;
};

export async function POST(req: NextRequest) {
  const auth = req.headers.get("authorization");
  if (auth !== `Bearer ${process.env.SYNC_TOKEN}`) {
    return NextResponse.json({ error: "unauthorized" }, { status: 401 });
  }

  const body = (await req.json()) as { run_id: string; label?: string; rows: Row[] };
  if (!body?.run_id || !Array.isArray(body.rows)) {
    return NextResponse.json({ error: "run_id and rows[] required" }, { status: 400 });
  }

  // push_to_web.py chunks a large batch into multiple POSTs sharing one
  // run_id. onConflictDoNothing() here silently dropped every chunk after
  // the first -- the runs table recorded only the first chunk's row count
  // (e.g. 200 of 1798) even though every chunk's products upsert below
  // still applied correctly. Accumulate instead, so the audit log matches
  // what actually landed.
  await db
    .insert(runs)
    .values({ id: body.run_id, label: body.label ?? null, rowCount: body.rows.length })
    .onConflictDoUpdate({
      target: runs.id,
      set: { rowCount: sql`${runs.rowCount} + ${body.rows.length}` },
    });

  if (body.rows.length === 0) return NextResponse.json({ upserted: 0 });

  const values = body.rows.map((r) => ({
    id: r.id,
    name: r.name,
    batch: r.batch,
    status: r.status,
    severity: r.severity,
    flags: r.flags ?? [],
    confidence: r.confidence,
    tier: r.tier,
    source: r.source,
    reason: r.reason,
    catSize: r.cat_size,
    catPack: r.cat_pack,
    detSize: r.det_size,
    detPack: r.det_pack,
    detFlavour: r.det_flavour,
    detContainer: r.det_container,
    sourceUrl: r.source_url,
    imageUrl: r.image_url,
    enhancedUrl: r.enhanced_url,
    thumbUrl: r.thumb_url,
    hasTrace: r.has_trace ?? false,
    runId: r.run_id,
    updatedAt: new Date(),
  }));

  // Pipeline-owned columns only. `reviews` and `overrides` are separate tables
  // and are never touched here, so a re-push cannot erase a human decision.
  await db
    .insert(products)
    .values(values)
    .onConflictDoUpdate({
      target: products.id,
      set: {
        name: sqlExcluded("name"),
        batch: sqlExcluded("batch"),
        status: sqlExcluded("status"),
        severity: sqlExcluded("severity"),
        flags: sqlExcluded("flags"),
        confidence: sqlExcluded("confidence"),
        tier: sqlExcluded("tier"),
        source: sqlExcluded("source"),
        reason: sqlExcluded("reason"),
        catSize: sqlExcluded("cat_size"),
        catPack: sqlExcluded("cat_pack"),
        detSize: sqlExcluded("det_size"),
        detPack: sqlExcluded("det_pack"),
        detFlavour: sqlExcluded("det_flavour"),
        detContainer: sqlExcluded("det_container"),
        sourceUrl: sqlExcluded("source_url"),
        imageUrl: sqlExcluded("image_url"),
        enhancedUrl: sqlExcluded("enhanced_url"),
        thumbUrl: sqlExcluded("thumb_url"),
        hasTrace: sqlExcluded("has_trace"),
        runId: sqlExcluded("run_id"),
        updatedAt: new Date(),
      },
    });

  return NextResponse.json({ upserted: values.length, run_id: body.run_id });
}
