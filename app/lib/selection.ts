import { and, eq, ilike, isNull, notInArray, sql } from "drizzle-orm";

import { db } from "./db";
import { products } from "./schema";

// Raised from 500: a single sourcing batch now routinely clears 1,000+
// accepted products (e.g. the 2026-10 1,500-item run, 1,300 accepted), and
// hitting the old cap meant "Download ZIP" / "Upload via worker" on "select
// all matching" just errored instead of partially working -- splitting a
// batch into multiple manual selections was the only workaround.
export const MAX_SELECTION = 2000;

// A sentinel `category` value meaning "rows where category IS NULL" -- 25
// products predate the category import and have no real category to filter
// on. Never a real category name (Sales Category values from the class
// export are plain uppercase words like "FOOD"/"DRINK", never this), so
// there's no collision to worry about.
export const UNCATEGORIZED_KEY = "__uncategorized__";

// A sentinel `uploadStatus` value meaning "no upload_jobs row exists yet for
// this product" -- distinct from "failed" or "queued", both of which DO have
// a row. Never a real state value (upload_jobs.state is one of queued/
// claimed/uploading/done/failed), so no collision.
export const NEVER_UPLOADED_KEY = "__never_uploaded__";

/**
 * The most recent upload_jobs row per product, collapsed to one of four
 * user-facing buckets. A raw scalar subquery rather than a join: a product
 * can have many historical jobs (retries, re-uploads after a fix), and only
 * the latest one is what "has this shipped to the OMS" actually means --
 * a join would multiply rows or require its own dedup logic at every call
 * site instead of once here.
 *
 * The outer column is written out as "products"."id" on purpose. Drizzle
 * renders ${products.id} unqualified ("id") when this expression sits in a
 * select list, and inside this subquery a bare "id" resolves to
 * upload_jobs.id -- so every row read as never-uploaded on /browse's cards
 * and Upload tallies, while WHERE (where Drizzle does qualify it) filtered
 * correctly.
 *
 * The fallback key is inlined (sql.raw of a constant) rather than bound as a
 * parameter because /browse groups by this expression, and
 * Postgres only matches a select-list expression to its GROUP BY copy when
 * the two render identically -- bound, they become $1 and $2 and the query
 * fails with "subquery uses ungrouped column".
 */
export const uploadStatusExpr = sql<string>`coalesce((
  select case upload_jobs.state
    when 'done' then 'uploaded'
    when 'failed' then 'failed'
    else 'queued'
  end
  from upload_jobs
  where upload_jobs.product_id = "products"."id"::text
  order by upload_jobs.created_at desc
  limit 1
), ${sql.raw(`'${NEVER_UPLOADED_KEY}'`)}::text)`;

export type ProductFilter = {
  status?: string;
  severity?: string;
  category?: string;
  uploadStatus?: string;
  q?: string;
};

/**
 * A /browse selection as the client sends it: either an explicit id list, or
 * "everything matching this filter" plus an exclude list (built by checking a
 * few boxes off after clicking "select all N matching"). Filter-based
 * selections don't materialize a list on the client -- a "select all 5,274
 * not_sourced" click never holds 5,274 ids in the browser, it holds the
 * filter that produced that count.
 */
export type Selection =
  | { mode: "ids"; ids: string[] }
  | { mode: "filter"; filter: ProductFilter; excludeIds?: string[] };

function filterClause(f: ProductFilter, excludeIds?: string[]) {
  return and(
    f.status && f.status !== "all" ? eq(products.status, f.status) : undefined,
    f.severity && f.severity !== "all" ? eq(products.severity, f.severity) : undefined,
    f.category === UNCATEGORIZED_KEY
      ? isNull(products.category)
      : f.category && f.category !== "all"
        ? eq(products.category, f.category)
        : undefined,
    f.q?.trim() ? ilike(products.name, `%${f.q.trim()}%`) : undefined,
    f.uploadStatus && f.uploadStatus !== "all" ? eq(uploadStatusExpr, f.uploadStatus) : undefined,
    excludeIds?.length ? notInArray(products.id, excludeIds) : undefined,
  );
}

/**
 * Resolves a Selection to concrete product ids, enforcing MAX_SELECTION
 * against the actual resolved count -- not the filter's apparent size -- so
 * an exclude list that trims a large filter under the cap is allowed through.
 */
export async function resolveSelection(
  sel: Selection,
): Promise<{ ids: string[] } | { error: string }> {
  if (sel.mode === "ids") {
    const ids = [...new Set(sel.ids.map(String).filter(Boolean))];
    if (ids.length === 0) return { error: "no products selected" };
    if (ids.length > MAX_SELECTION) {
      return { error: `${ids.length} products selected — at most ${MAX_SELECTION} at a time` };
    }
    return { ids };
  }

  const rows = await db
    .select({ id: products.id })
    .from(products)
    .where(filterClause(sel.filter, sel.excludeIds));
  const ids = rows.map((r) => r.id);
  if (ids.length === 0) return { error: "no products match that filter" };
  if (ids.length > MAX_SELECTION) {
    return {
      error: `${ids.length} products match this filter — at most ${MAX_SELECTION} at a time. ` +
        `Narrow the filter, or switch to selecting individual products.`,
    };
  }
  return { ids };
}

/** Count of products matching a filter, for the "select all N matching" label. */
export async function countSelection(filter: ProductFilter, excludeIds?: string[]): Promise<number> {
  const [{ n }] = await db
    .select({ n: sql<number>`count(*)::int` })
    .from(products)
    .where(filterClause(filter, excludeIds));
  return n;
}
