"use server";

import { and, eq, inArray, sql } from "drizzle-orm";
import { revalidatePath } from "next/cache";
import { cookies } from "next/headers";

import { db } from "./db";
import { overrides, products, reviews, uploadJobs } from "./schema";
import { resolveSelection, type Selection } from "./selection";

async function reviewer(): Promise<string> {
  const c = await cookies();
  return c.get("who")?.value || "stakeholder";
}

/**
 * Record a human verdict.
 *
 * Approving queues the watermarked image for upload. The job carries the exact
 * URL rather than a rule for finding one: a stale library_enhanced/ once let 19
 * products publish unbranded because a missing file quietly resolved to the
 * plain original. If there is nothing branded to send, we say so instead of
 * substituting.
 */
export async function decide(productId: string, decision: string, note?: string) {
  const who = await reviewer();

  await db
    .insert(reviews)
    .values({ productId, decision, note: note || null, reviewer: who })
    .onConflictDoUpdate({
      target: reviews.productId,
      set: { decision, note: note || null, reviewer: who, decidedAt: new Date() },
    });

  if (decision === "approved") {
    const [p] = await db
      .select({ enhanced: products.enhancedUrl, image: products.imageUrl })
      .from(products)
      .where(eq(products.id, productId));

    // Prefer the watermarked file. An override's processed image is already
    // watermarked and lands in enhancedUrl when the worker completes it.
    const url = p?.enhanced ?? null;
    if (url) {
      // The partial unique index rejects a second live job for this product,
      // so a double-click is a no-op rather than a duplicate upload.
      await db
        .insert(uploadJobs)
        .values({ productId, imageUrl: url, origin: "pipeline", state: "queued" })
        .onConflictDoNothing();
    }
  }

  revalidatePath("/");
  revalidatePath(`/product/${productId}`);
}

/** Clear a verdict, and withdraw any upload that has not started yet. */
export async function undecide(productId: string) {
  await db.delete(reviews).where(eq(reviews.productId, productId));
  await db
    .delete(uploadJobs)
    .where(and(eq(uploadJobs.productId, productId), eq(uploadJobs.state, "queued")));
  revalidatePath("/");
  revalidatePath(`/product/${productId}`);
}

const MAX_URL = 2048;

/**
 * Accept a stakeholder-supplied replacement image URL.
 *
 * Only shape is checked here. Fetching, decoding, squaring and watermarking all
 * happen in the local worker, which imports the real watermark code -- a second
 * implementation on Vercel would drift from the config the other images were
 * built with and brand these differently.
 */
export async function submitOverride(productId: string, rawUrl: string, note?: string) {
  const who = await reviewer();
  const url = (rawUrl || "").trim();

  if (!url) return { ok: false, error: "Paste an image link first." };
  if (url.length > MAX_URL) return { ok: false, error: "That link is too long." };

  let parsed: URL;
  try {
    parsed = new URL(url);
  } catch {
    return { ok: false, error: "That doesn't look like a web address." };
  }
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
    return { ok: false, error: "Only http and https links can be used." };
  }
  // Block the obvious SSRF shapes before the worker ever fetches it. The worker
  // re-checks, because this is a convenience, not the security boundary.
  const host = parsed.hostname.toLowerCase();
  const privateHost =
    host === "localhost" ||
    host === "0.0.0.0" ||
    host.endsWith(".local") ||
    host.endsWith(".internal") ||
    /^127\./.test(host) ||
    /^10\./.test(host) ||
    /^192\.168\./.test(host) ||
    /^169\.254\./.test(host) ||
    /^172\.(1[6-9]|2\d|3[01])\./.test(host) ||
    host.startsWith("[");
  if (privateHost) return { ok: false, error: "That address isn't reachable from the internet." };

  // An earlier pending submission for the same product is no longer wanted.
  await db
    .update(overrides)
    .set({ state: "superseded" })
    .where(and(eq(overrides.productId, productId), inArray(overrides.state, ["pending"])));

  await db.insert(overrides).values({
    productId,
    submittedUrl: url,
    state: "pending",
    submittedBy: who,
    note: note || null,
  });

  // The replacement is the decision -- record it so the row stops reading as
  // unreviewed while the worker catches up.
  await db
    .insert(reviews)
    .values({ productId, decision: "needs_new_image", note: note || null, reviewer: who })
    .onConflictDoUpdate({
      target: reviews.productId,
      set: { decision: "needs_new_image", reviewer: who, decidedAt: new Date() },
    });

  revalidatePath("/");
  revalidatePath(`/product/${productId}`);
  return { ok: true };
}

/** Queue an upload for a product whose image is already approved and branded. */
export async function requeueUpload(productId: string) {
  const [p] = await db
    .select({ enhanced: products.enhancedUrl })
    .from(products)
    .where(eq(products.id, productId));
  if (!p?.enhanced) return { ok: false, error: "No watermarked image to upload yet." };

  await db
    .insert(uploadJobs)
    .values({ productId, imageUrl: p.enhanced, origin: "pipeline", state: "queued" })
    .onConflictDoNothing();
  revalidatePath(`/product/${productId}`);
  return { ok: true };
}

/**
 * /browse's "Upload via worker": approve and queue a whole selection in one
 * action, on the strength of the severity/status indicators already visible
 * in the grid rather than a per-product click into /product/[id]. Selecting
 * "clean" + "accepted" IS the review decision here -- this just records it.
 *
 * Same rule as decide(): no watermarked image, no job. Reports which ids were
 * queued vs skipped rather than silently dropping either.
 */
export async function bulkApproveAndQueue(selection: Selection) {
  const who = await reviewer();
  const resolved = await resolveSelection(selection);
  if ("error" in resolved) return { queued: [], skipped: [], error: resolved.error };
  const productIds = resolved.ids;

  const rows = await db
    .select({ id: products.id, enhanced: products.enhancedUrl })
    .from(products)
    .where(inArray(products.id, productIds));

  const byId = new Map(rows.map((r) => [r.id, r.enhanced]));
  const queued: string[] = [];
  const skipped: string[] = [];

  for (const id of productIds) {
    const enhanced = byId.get(id);
    if (!enhanced) {
      skipped.push(id);
      continue;
    }
    queued.push(id);
  }

  if (queued.length > 0) {
    await db
      .insert(reviews)
      .values(queued.map((id) => ({ productId: id, decision: "approved", reviewer: who })))
      .onConflictDoUpdate({
        target: reviews.productId,
        set: { decision: "approved", reviewer: who, decidedAt: new Date() },
      });

    await db
      .insert(uploadJobs)
      .values(
        queued.map((id) => ({
          productId: id,
          imageUrl: byId.get(id)!,
          origin: "pipeline" as const,
          state: "queued" as const,
        })),
      )
      // Same as decide(): a product already queued/claimed/uploading is a
      // no-op here rather than a duplicate upload.
      .onConflictDoNothing();
  }

  revalidatePath("/browse");
  revalidatePath("/");
  return { queued, skipped };
}

/** Seconds since the most recent worker heartbeat, or null if none ever. */
export async function workerAge(): Promise<number | null> {
  const res = await db.execute<{ age: number | null }>(
    sql`select extract(epoch from now() - max(last_seen))::int as age from workers`,
  );
  return res.rows[0]?.age ?? null;
}
