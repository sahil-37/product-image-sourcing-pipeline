import { existsSync } from "node:fs";
import path from "node:path";

import { ZipArchive } from "archiver";
import { inArray } from "drizzle-orm";
import { NextRequest, NextResponse } from "next/server";
import { Readable } from "node:stream";

import { db } from "@/lib/db";
import { products } from "@/lib/schema";
import { resolveSelection, type Selection } from "@/lib/selection";

export const dynamic = "force-dynamic";

const REPO_ROOT = path.resolve(process.cwd(), "..");

/**
 * Bundles the selected products' watermarked image into a ZIP, each file
 * named "<item-id>.jpg" -- the shape the OMS's own bulk-upload-by-item-number
 * feature expects, so this download is a manual, Playwright-free alternative
 * to "Upload via worker".
 *
 * Only ever ships enhancedUrl (the watermarked file). It never falls back to
 * the plain sourced photo: a fallback here is exactly how an unbranded image
 * would reach the OMS with no warning -- a product with no enhancedUrl is
 * reported as skipped instead, same as a product with no image at all,
 * but under its own distinct reason so "why isn't 103245 in the zip" has an
 * answer (watermarking hasn't run yet, or it ran and failed) rather than
 * silently shipping the wrong file.
 */
export async function POST(req: NextRequest) {
  const selection = (await req.json().catch(() => null)) as Selection | null;
  if (!selection) {
    return NextResponse.json({ error: "a selection is required" }, { status: 400 });
  }
  const resolved = await resolveSelection(selection);
  if ("error" in resolved) {
    return NextResponse.json({ error: resolved.error }, { status: 400 });
  }
  const ids = resolved.ids;

  const rows = await db
    .select({
      id: products.id,
      imageUrl: products.imageUrl,
      enhancedUrl: products.enhancedUrl,
    })
    .from(products)
    .where(inArray(products.id, ids));

  const archive = new ZipArchive({ zlib: { level: 6 } });
  const noImage: string[] = [];
  const noWatermark: string[] = [];
  const notLocal: string[] = [];

  for (const r of rows) {
    if (!r.enhancedUrl) {
      // Has a sourced photo (imageUrl) but no watermarked version, or no
      // image at all -- either way, nothing safe to ship.
      (r.imageUrl ? noWatermark : noImage).push(r.id);
      continue;
    }
    // Local paths only ("/images/..."); a row still on an old Vercel Blob URL
    // is not on this filesystem, so it can't be zipped from here.
    if (!r.enhancedUrl.startsWith("/images/")) {
      notLocal.push(r.id);
      continue;
    }
    const filePath = path.join(REPO_ROOT, "app", "public", r.enhancedUrl);
    if (!existsSync(filePath)) {
      noImage.push(r.id);
      continue;
    }
    archive.file(filePath, { name: `${r.id}.jpg` });
  }

  const missingIds = ids.filter((id) => !rows.some((r) => r.id === id));
  if (noImage.length || noWatermark.length || notLocal.length || missingIds.length) {
    const note = [
      noWatermark.length
        ? `${noWatermark.length} product(s) skipped -- sourced but not yet watermarked: ${noWatermark.join(", ")}`
        : null,
      noImage.length ? `${noImage.length} product(s) skipped -- no image at all: ${noImage.join(", ")}` : null,
      notLocal.length
        ? `${notLocal.length} product(s) skipped -- image is not on local storage: ${notLocal.join(", ")}`
        : null,
      missingIds.length ? `${missingIds.length} id(s) not found: ${missingIds.join(", ")}` : null,
    ]
      .filter(Boolean)
      .join("\n");
    archive.append(note, { name: "_skipped.txt" });
  }

  archive.finalize();

  // NextResponse wants a Web ReadableStream; archiver hands back a Node one.
  const webStream = Readable.toWeb(archive) as ReadableStream;

  return new NextResponse(webStream, {
    headers: {
      "content-type": "application/zip",
      "content-disposition": `attachment; filename="product-images-${Date.now()}.zip"`,
    },
  });
}
