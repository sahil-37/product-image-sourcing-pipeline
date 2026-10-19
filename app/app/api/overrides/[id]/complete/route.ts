import { eq } from "drizzle-orm";
import { NextRequest, NextResponse } from "next/server";

import { db } from "@/lib/db";
import { overrides, products, uploadJobs } from "@/lib/schema";

export const dynamic = "force-dynamic";

/**
 * The worker reports the result of processing a stakeholder-supplied image.
 *
 * On success the processed image is already squared and watermarked, so it
 * becomes the product's enhanced image and is immediately queued for upload --
 * the stakeholder asked for a replacement, which is an implicit approval.
 */
export async function POST(
  req: NextRequest,
  { params }: { params: Promise<{ id: string }> },
) {
  if (req.headers.get("authorization") !== `Bearer ${process.env.WORKER_TOKEN}`) {
    return NextResponse.json({ error: "unauthorized" }, { status: 401 });
  }

  const { id } = await params;
  const overrideId = Number(id);
  if (!Number.isFinite(overrideId)) {
    return NextResponse.json({ error: "bad override id" }, { status: 400 });
  }

  const body = (await req.json().catch(() => ({}))) as {
    processed_url?: string;
    sha256?: string;
    error?: string;
  };

  const [row] = await db.select().from(overrides).where(eq(overrides.id, overrideId));
  if (!row) return NextResponse.json({ error: "not found" }, { status: 404 });

  if (body.error || !body.processed_url) {
    await db
      .update(overrides)
      .set({ state: "failed", error: body.error ?? "no processed image returned", processedAt: new Date() })
      .where(eq(overrides.id, overrideId));
    return NextResponse.json({ ok: true, state: "failed" });
  }

  await db
    .update(overrides)
    .set({
      state: "processed",
      processedUrl: body.processed_url,
      sha256: body.sha256 ?? null,
      error: null,
      processedAt: new Date(),
    })
    .where(eq(overrides.id, overrideId));

  // The human-supplied image is now the product's image, in both slots: it is
  // already watermarked, and it is what should be shown and shipped.
  await db
    .update(products)
    .set({ enhancedUrl: body.processed_url, imageUrl: body.processed_url, updatedAt: new Date() })
    .where(eq(products.id, row.productId));

  await db
    .insert(uploadJobs)
    .values({
      productId: row.productId,
      imageUrl: body.processed_url,
      origin: "override",
      state: "queued",
    })
    .onConflictDoNothing();

  return NextResponse.json({ ok: true, state: "processed" });
}
