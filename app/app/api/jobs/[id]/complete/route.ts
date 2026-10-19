import { eq } from "drizzle-orm";
import { NextRequest, NextResponse } from "next/server";

import { db } from "@/lib/db";
import { uploadJobs } from "@/lib/schema";

export const dynamic = "force-dynamic";

/** The worker reports a terminal state for one upload job. */
export async function POST(
  req: NextRequest,
  { params }: { params: Promise<{ id: string }> },
) {
  if (req.headers.get("authorization") !== `Bearer ${process.env.WORKER_TOKEN}`) {
    return NextResponse.json({ error: "unauthorized" }, { status: 401 });
  }

  const { id } = await params;
  const jobId = Number(id);
  if (!Number.isFinite(jobId)) {
    return NextResponse.json({ error: "bad job id" }, { status: 400 });
  }

  const body = (await req.json().catch(() => ({}))) as {
    outcome?: string;
    error?: string;
  };

  // An expired session is not a per-product failure -- every job will fail the
  // same way until someone re-authenticates by hand. The UI keys its "uploads
  // paused" banner off this exact string.
  const failed = Boolean(body.error);

  await db
    .update(uploadJobs)
    .set({
      state: failed ? "failed" : "done",
      outcome: body.outcome ?? null,
      error: body.error ?? null,
      updatedAt: new Date(),
    })
    .where(eq(uploadJobs.id, jobId));

  return NextResponse.json({ ok: true, id: jobId });
}
