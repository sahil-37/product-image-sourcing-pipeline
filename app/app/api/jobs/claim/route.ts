import { sql } from "drizzle-orm";
import { NextRequest, NextResponse } from "next/server";

import { db } from "@/lib/db";

export const dynamic = "force-dynamic";

const LEASE_MINUTES = 10;

/**
 * Lease work to the local worker.
 *
 * Vercel cannot reach the OMS -- that needs Playwright driving a persistent
 * Chromium profile whose remembered-device cookie is the only thing standing
 * between us and an OTP that has to be retrieved by phone. So the worker pulls;
 * nothing here ever calls the worker.
 *
 * FOR UPDATE SKIP LOCKED stops two workers grabbing the same row. The lease
 * means a worker that dies mid-upload returns its jobs to the pool after ten
 * minutes instead of wedging them forever.
 */
export async function POST(req: NextRequest) {
  if (req.headers.get("authorization") !== `Bearer ${process.env.WORKER_TOKEN}`) {
    return NextResponse.json({ error: "unauthorized" }, { status: 401 });
  }

  const body = (await req.json().catch(() => ({}))) as { worker_id?: string; max?: number };
  const workerId = body.worker_id || "worker";
  const max = Math.min(Math.max(1, body.max ?? 5), 25);

  await db.execute(sql`
    insert into workers (id, last_seen) values (${workerId}, now())
    on conflict (id) do update set last_seen = now()
  `);

  const jobs = await db.execute<{
    id: number;
    product_id: string;
    image_url: string;
    origin: string;
    attempts: number;
  }>(sql`
    update upload_jobs set
      state = 'claimed',
      claimed_by = ${workerId},
      claimed_at = now(),
      lease_until = now() + interval '${sql.raw(String(LEASE_MINUTES))} minutes',
      attempts = attempts + 1,
      updated_at = now()
    where id in (
      select id from upload_jobs
      where state = 'queued'
         or (state in ('claimed','uploading') and lease_until < now())
      order by created_at
      limit ${max}
      for update skip locked
    )
    returning id, product_id, image_url, origin, attempts
  `);

  // Replacement images the worker must fetch, square, watermark and store.
  // Done there rather than here so it uses the real watermark code: a second
  // implementation would drift from the config the other images were built with.
  const pending = await db.execute<{
    id: number;
    product_id: string;
    submitted_url: string;
  }>(sql`
    update overrides set state = 'claimed', processed_at = null
    where id in (
      select id from overrides where state = 'pending'
      order by submitted_at limit ${max}
      for update skip locked
    )
    returning id, product_id, submitted_url
  `);

  return NextResponse.json({ jobs: jobs.rows ?? jobs, overrides: pending.rows ?? pending });
}
