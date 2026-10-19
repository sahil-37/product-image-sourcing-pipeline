import { sql } from "drizzle-orm";
import { NextRequest, NextResponse } from "next/server";

import { db } from "@/lib/db";

export const dynamic = "force-dynamic";

/**
 * Worker liveness.
 *
 * Overrides are not instant -- they wait for a local worker to run. A
 * stakeholder who can see the system is alive will wait; one staring at a
 * spinner with no explanation will assume it is broken. This is what lets the
 * UI say "queued - worker last seen 3m ago".
 */
export async function POST(req: NextRequest) {
  if (req.headers.get("authorization") !== `Bearer ${process.env.WORKER_TOKEN}`) {
    return NextResponse.json({ error: "unauthorized" }, { status: 401 });
  }

  const body = (await req.json().catch(() => ({}))) as {
    worker_id?: string;
    watermark_hash?: string;
    note?: string;
  };
  const id = body.worker_id || "worker";

  await db.execute(sql`
    insert into workers (id, last_seen, watermark_hash, note)
    values (${id}, now(), ${body.watermark_hash ?? null}, ${body.note ?? null})
    on conflict (id) do update set
      last_seen = now(),
      watermark_hash = excluded.watermark_hash,
      note = excluded.note
  `);

  return NextResponse.json({ ok: true, worker_id: id });
}
