import { sql } from "drizzle-orm";
import { NextRequest, NextResponse } from "next/server";

import { db } from "@/lib/db";

export const dynamic = "force-dynamic";
export const maxDuration = 60;

type Row = { id: string; category: string | null; subcategory: string | null };

/**
 * Sets category/subcategory on existing products, from import_category.py's
 * read of data/all_products_class.xlsx.
 *
 * Deliberately separate from /api/sync/run: that route is owned by
 * push_to_web.py and its `set` clause overwrites every pipeline column on
 * every push, so a category column added there would need to be threaded
 * through the whole pipeline just to avoid being wiped back to null on the
 * next sync. This route's UPDATE ... FROM (VALUES ...) can only ever touch
 * category/subcategory, structurally -- same isolation reviews/overrides
 * already have from the pipeline sync. It also can't create a product row:
 * the FROM-join only ever matches ids already in the table, so a typo'd or
 * stale id in the class file is silently unmatched rather than creating a
 * stub product with no name/status/etc.
 */
export async function POST(req: NextRequest) {
  const auth = req.headers.get("authorization");
  if (auth !== `Bearer ${process.env.SYNC_TOKEN}`) {
    return NextResponse.json({ error: "unauthorized" }, { status: 401 });
  }

  const body = (await req.json().catch(() => null)) as { rows: Row[] } | null;
  if (!body || !Array.isArray(body.rows)) {
    return NextResponse.json({ error: "rows[] required" }, { status: 400 });
  }
  if (body.rows.length === 0) return NextResponse.json({ updated: 0 });

  const valuesList = sql.join(
    body.rows.map(
      (r) => sql`(${r.id}, ${r.category}, ${r.subcategory})`,
    ),
    sql`, `,
  );

  const result = await db.execute(sql`
    update products
    set category = v.category, subcategory = v.subcategory
    from (values ${valuesList}) as v(id, category, subcategory)
    where products.id = v.id
  `);

  return NextResponse.json({ updated: result.rowCount ?? 0, requested: body.rows.length });
}
