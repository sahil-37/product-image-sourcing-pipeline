import {
  bigserial,
  boolean,
  index,
  integer,
  jsonb,
  pgTable,
  real,
  text,
  timestamp,
  uniqueIndex,
} from "drizzle-orm/pg-core";
import { sql } from "drizzle-orm";

/** One pipeline push. */
export const runs = pgTable("runs", {
  id: text("id").primaryKey(),
  label: text("label"),
  pushedAt: timestamp("pushed_at", { withTimezone: true }).notNull().defaultNow(),
  rowCount: integer("row_count"),
});

/**
 * Pipeline output, one row per product. Owned entirely by push_to_web.py --
 * a sync upsert overwrites every column here, which is why human decisions
 * live in `reviews` and `overrides` instead.
 */
export const products = pgTable(
  "products",
  {
    id: text("id").primaryKey(), // catalogue item id, e.g. '100060'
    name: text("name").notNull(),
    batch: text("batch").notNull(),
    // Real merchandising category/subcategory (e.g. "FOOD" / "CHOCOLATE"),
    // from data/all_products_class.xlsx via import_category.py. Distinct from
    // `batch`, which is a pipeline run label (drinks, food_pilot, ...), not a
    // merchandising category. Owned entirely by import_category.py -- never
    // set by /api/sync/run, so a pipeline push can't clobber it, the same
    // guarantee `reviews`/`overrides` have.
    category: text("category"),
    subcategory: text("subcategory"),
    status: text("status").notNull(), // accepted | review | no_match | error
    severity: text("severity").notNull(), // clean | flagged | blunder
    flags: jsonb("flags").notNull().default(sql`'[]'::jsonb`),
    confidence: real("confidence"),
    tier: integer("tier"),
    source: text("source"),
    reason: text("reason"),
    // Catalogue says / image shows -- the comparison a reviewer actually makes.
    catSize: text("cat_size"),
    catPack: integer("cat_pack"),
    detSize: text("det_size"),
    detPack: integer("det_pack"),
    detFlavour: text("det_flavour"),
    detContainer: text("det_container"),
    sourceUrl: text("source_url"), // where the image was found
    imageUrl: text("image_url"), // blob: library/
    enhancedUrl: text("enhanced_url"), // blob: enhanced/ (watermarked)
    thumbUrl: text("thumb_url"), // blob: thumb/ (256px)
    hasTrace: boolean("has_trace").notNull().default(false),
    runId: text("run_id").references(() => runs.id),
    updatedAt: timestamp("updated_at", { withTimezone: true }).notNull().defaultNow(),
  },
  (t) => [
    index("products_severity_idx").on(t.severity),
    index("products_batch_idx").on(t.batch),
    index("products_status_idx").on(t.status),
    index("products_category_idx").on(t.category),
  ],
);

/**
 * Human verdicts. Separate table so a pipeline push can never clobber them --
 * the sync endpoint only ever touches `products`.
 */
export const reviews = pgTable("reviews", {
  productId: text("product_id")
    .primaryKey()
    .references(() => products.id),
  decision: text("decision").notNull(), // approved | rejected | needs_new_image
  note: text("note"),
  reviewer: text("reviewer"),
  decidedAt: timestamp("decided_at", { withTimezone: true }).notNull().defaultNow(),
});

/** Stakeholder-supplied replacement images, before and after processing. */
export const overrides = pgTable(
  "overrides",
  {
    id: bigserial("id", { mode: "number" }).primaryKey(),
    productId: text("product_id")
      .notNull()
      .references(() => products.id),
    submittedUrl: text("submitted_url").notNull(),
    state: text("state").notNull(), // pending | processed | failed | superseded
    processedUrl: text("processed_url"), // blob: override/ (squared + watermarked)
    sha256: text("sha256"),
    error: text("error"),
    submittedBy: text("submitted_by"),
    note: text("note"),
    submittedAt: timestamp("submitted_at", { withTimezone: true }).notNull().defaultNow(),
    processedAt: timestamp("processed_at", { withTimezone: true }),
  },
  (t) => [index("overrides_state_idx").on(t.state, t.submittedAt)],
);

/**
 * Work queue for the local Playwright worker. Vercel cannot reach the OMS --
 * it needs a persistent Chromium profile holding a remembered-device cookie --
 * so the app records intent here and the worker claims it.
 */
export const uploadJobs = pgTable(
  "upload_jobs",
  {
    id: bigserial("id", { mode: "number" }).primaryKey(),
    productId: text("product_id")
      .notNull()
      .references(() => products.id),
    /**
     * The exact image to upload. Explicit, never derived: a stale
     * library_enhanced/ once let 19 products publish unbranded because a
     * missing file silently fell back to the plain original. A 404 here fails
     * the job instead of substituting.
     */
    imageUrl: text("image_url").notNull(),
    origin: text("origin").notNull(), // pipeline | override
    state: text("state").notNull(), // queued | claimed | uploading | done | failed
    attempts: integer("attempts").notNull().default(0),
    claimedBy: text("claimed_by"),
    claimedAt: timestamp("claimed_at", { withTimezone: true }),
    leaseUntil: timestamp("lease_until", { withTimezone: true }),
    outcome: text("outcome"),
    error: text("error"),
    createdAt: timestamp("created_at", { withTimezone: true }).notNull().defaultNow(),
    updatedAt: timestamp("updated_at", { withTimezone: true }).notNull().defaultNow(),
  },
  (t) => [
    index("upload_jobs_state_idx").on(t.state, t.createdAt),
    // One live job per product, enforced by the database rather than by
    // remembering to check in application code.
    uniqueIndex("upload_jobs_one_live_per_product")
      .on(t.productId)
      .where(sql`state in ('queued','claimed','uploading')`),
  ],
);

/** Worker liveness, so the UI can say "queued - worker last seen 3m ago". */
export const workers = pgTable("workers", {
  id: text("id").primaryKey(),
  lastSeen: timestamp("last_seen", { withTimezone: true }).notNull().defaultNow(),
  watermarkHash: text("watermark_hash"), // drift check against the pushed config
  note: text("note"),
});

export type Product = typeof products.$inferSelect;
export type Review = typeof reviews.$inferSelect;
export type Override = typeof overrides.$inferSelect;
export type UploadJob = typeof uploadJobs.$inferSelect;
