import { and, count, eq, ilike, isNull, sql } from "drizzle-orm";

import { db } from "@/lib/db";
import { products } from "@/lib/schema";
import { NEVER_UPLOADED_KEY, UNCATEGORIZED_KEY, uploadStatusExpr } from "@/lib/selection";

import BrowseGrid from "./BrowseGrid";

export const dynamic = "force-dynamic";

const PAGE_SIZE = 96;

/** Sum of a facet tally's counts -- "All" under the OTHER active filters. */
function sumTally(tally: Record<string, number>): number {
  return Object.values(tally).reduce((a, b) => a + b, 0);
}

type Search = {
  status?: string;
  severity?: string;
  category?: string;
  uploadStatus?: string;
  q?: string;
  page?: string;
};

export default async function Browse({
  searchParams,
}: {
  searchParams: Promise<Search>;
}) {
  const sp = await searchParams;
  const page = Math.max(1, parseInt(sp.page ?? "1", 10) || 1);
  const status = sp.status && sp.status !== "all" ? sp.status : null;
  const severity = sp.severity && sp.severity !== "all" ? sp.severity : null;
  const category = sp.category && sp.category !== "all" ? sp.category : null;
  const uploadStatus = sp.uploadStatus && sp.uploadStatus !== "all" ? sp.uploadStatus : null;
  const q = (sp.q ?? "").trim();

  const statusClause = status ? eq(products.status, status) : undefined;
  const severityClause = severity ? eq(products.severity, severity) : undefined;
  const categoryClause =
    category === UNCATEGORIZED_KEY
      ? isNull(products.category)
      : category
        ? eq(products.category, category)
        : undefined;
  const uploadStatusClause = uploadStatus ? eq(uploadStatusExpr, uploadStatus) : undefined;
  const qClause = q ? ilike(products.name, `%${q}%`) : undefined;

  const where = and(statusClause, severityClause, categoryClause, uploadStatusClause, qClause);

  // Unsourced first by default: the main reason to browse unfiltered is to
  // pick a batch to send through sourcing next, not to re-review old work.
  const STATUS_RANK = sql`case ${products.status} when 'not_sourced' then 0 else 1 end`;

  // Each facet's tally excludes its OWN clause but keeps every other active
  // filter -- severity is almost entirely a derived relabeling of status
  // (not_sourced<->unsourced, no_match<->blunder, accepted->clean/flagged,
  // review->flagged), so a status+severity combination outside that mapping
  // is a guaranteed-empty dead click. Scoping tallies this way makes that
  // structurally visible: picking a status narrows which severities even
  // show up as options, instead of two facets that look independent but
  // mostly aren't, with no warning before you land on an empty grid.
  const [rows, [{ total }], statusTallies, severityTallies, categoryTallies, uploadStatusTallies] =
    await Promise.all([
      db
        .select({ product: products, uploadStatus: uploadStatusExpr })
        .from(products)
        .where(where)
        .orderBy(STATUS_RANK, products.category, products.name)
        .limit(PAGE_SIZE)
        .offset((page - 1) * PAGE_SIZE),
      db.select({ total: count() }).from(products).where(where),
      db
        .select({ status: products.status, n: count() })
        .from(products)
        .where(and(severityClause, categoryClause, uploadStatusClause, qClause))
        .groupBy(products.status),
      db
        .select({ severity: products.severity, n: count() })
        .from(products)
        .where(and(statusClause, categoryClause, uploadStatusClause, qClause))
        .groupBy(products.severity),
      db
        .select({ category: products.category, n: count() })
        .from(products)
        .where(and(statusClause, severityClause, uploadStatusClause, qClause))
        .groupBy(products.category),
      db
        .select({ uploadStatus: uploadStatusExpr, n: count() })
        .from(products)
        .where(and(statusClause, severityClause, categoryClause, qClause))
        .groupBy(uploadStatusExpr),
    ]);

  const byStatus = Object.fromEntries(statusTallies.map((t) => [t.status, Number(t.n)]));
  const bySeverity = Object.fromEntries(severityTallies.map((t) => [t.severity, Number(t.n)]));
  // Uncategorized rows group under null -- give them their own real facet
  // option (UNCATEGORIZED_KEY) rather than silently dropping them. Dropping
  // them here would also desync the Category row's "All" count (sumTally of
  // byCategory) from the true total whenever any matching row lacks a
  // category, which is real today: 25 products predate the category import.
  const byCategory = Object.fromEntries(
    categoryTallies.map((t) => [t.category ?? UNCATEGORIZED_KEY, Number(t.n)]),
  );
  const byUploadStatus = Object.fromEntries(
    uploadStatusTallies.map((t) => [t.uploadStatus, Number(t.n)]),
  );
  const pages = Math.max(1, Math.ceil(Number(total) / PAGE_SIZE));

  const qs = (over: Partial<Search>) => {
    const u = new URLSearchParams();
    const merged = {
      status: sp.status,
      severity: sp.severity,
      category: sp.category,
      uploadStatus: sp.uploadStatus,
      q: sp.q,
      ...over,
    };
    for (const [k, v] of Object.entries(merged)) if (v) u.set(k, String(v));
    return `/browse?${u.toString()}`;
  };

  return (
    <div className="wrap">
      <header className="masthead">
        <p className="kicker">Catalogue browser</p>
        <h1>Browse &amp; select products</h1>
        <p className="lede">
          Filter by status, severity or category, select the products you want to work with, then
          source images or send them to the OMS &mdash; sourcing and uploading are separate steps
          you trigger independently.
        </p>
      </header>

      <div className="controls">
        <form className="crow" action="/browse" method="get">
          {sp.status ? <input type="hidden" name="status" value={sp.status} /> : null}
          {sp.severity ? <input type="hidden" name="severity" value={sp.severity} /> : null}
          {sp.category ? <input type="hidden" name="category" value={sp.category} /> : null}
          {sp.uploadStatus ? (
            <input type="hidden" name="uploadStatus" value={sp.uploadStatus} />
          ) : null}
          <input
            className="search"
            type="search"
            name="q"
            defaultValue={q}
            placeholder="Filter by product name…"
            aria-label="Filter by product name"
          />
          <span className="count">
            {Number(total)} match{Number(total) === 1 ? "" : "es"}
          </span>
        </form>

        <div className="facet-row">
          <span className="facet-label">Status</span>
          <a className="seg" data-on={!status} href={qs({ status: "", page: "" })}>
            {/* Sum of byStatus, not `total`: this is "if I clear only status,
            keeping severity/category/search", not the fully-filtered count. */}
            All <span className="seg-n">{sumTally(byStatus)}</span>
          </a>
          {(["accepted", "review", "no_match", "error", "not_sourced"] as const)
            .filter((s) => byStatus[s])
            .map((s) => (
              <a key={s} className="seg" data-on={status === s} href={qs({ status: s, page: "" })}>
                {s} <span className="seg-n">{byStatus[s] ?? 0}</span>
              </a>
            ))}
        </div>

        <div className="facet-row">
          <span className="facet-label">Severity</span>
          <a className="seg" data-on={!severity} href={qs({ severity: "", page: "" })}>
            All <span className="seg-n">{sumTally(bySeverity)}</span>
          </a>
          {/* Severity is almost entirely a derived relabeling of status
          (not_sourced<->unsourced, no_match<->blunder, accepted->clean/
          flagged, review->flagged) -- most status+severity combinations are
          guaranteed empty. Hiding zero-count options here, scoped by
          whatever status is already selected, stops that dead click instead
          of landing on an unexplained empty grid. */}
          {(["blunder", "flagged", "clean", "unsourced"] as const)
            .filter((s) => bySeverity[s])
            .map((s) => (
              <a key={s} className="seg" data-on={severity === s} href={qs({ severity: s, page: "" })}>
                {s === "blunder" ? "No usable image" : s === "unsourced" ? "Not sourced yet" : s}{" "}
                <span className="seg-n">{bySeverity[s] ?? 0}</span>
              </a>
            ))}
        </div>

        <div className="facet-row">
          <span className="facet-label">Category</span>
          <a className="seg" data-on={!category} href={qs({ category: "", page: "" })}>
            All <span className="seg-n">{sumTally(byCategory)}</span>
          </a>
          {/* Real categories first, alphabetical; "Uncategorized" (rows that
          predate the class import) always last -- it's a fallback bucket,
          not a merchandising category, so it shouldn't sort in among them. */}
          {Object.keys(byCategory)
            .filter((c) => c !== UNCATEGORIZED_KEY)
            .sort()
            .map((c) => (
              <a key={c} className="seg" data-on={category === c} href={qs({ category: c, page: "" })}>
                {c} <span className="seg-n">{byCategory[c]}</span>
              </a>
            ))}
          {byCategory[UNCATEGORIZED_KEY] ? (
            <a
              className="seg"
              data-on={category === UNCATEGORIZED_KEY}
              href={qs({ category: UNCATEGORIZED_KEY, page: "" })}
            >
              Uncategorized <span className="seg-n">{byCategory[UNCATEGORIZED_KEY]}</span>
            </a>
          ) : null}
        </div>

        <div className="facet-row">
          <span className="facet-label">Upload</span>
          <a className="seg" data-on={!uploadStatus} href={qs({ uploadStatus: "", page: "" })}>
            All <span className="seg-n">{sumTally(byUploadStatus)}</span>
          </a>
          {/* "uploaded"/"queued"/"failed" first (there's always at most one
          upload_jobs row driving each), "never uploaded" last -- it's the
          default state for most of the catalogue, not a merchandising-style
          category, so it shouldn't sort in among the real outcomes. */}
          {(["uploaded", "queued", "failed"] as const)
            .filter((s) => byUploadStatus[s])
            .map((s) => (
              <a
                key={s}
                className="seg"
                data-on={uploadStatus === s}
                href={qs({ uploadStatus: s, page: "" })}
              >
                {s} <span className="seg-n">{byUploadStatus[s]}</span>
              </a>
            ))}
          {byUploadStatus[NEVER_UPLOADED_KEY] ? (
            <a
              className="seg"
              data-on={uploadStatus === NEVER_UPLOADED_KEY}
              href={qs({ uploadStatus: NEVER_UPLOADED_KEY, page: "" })}
            >
              Never uploaded <span className="seg-n">{byUploadStatus[NEVER_UPLOADED_KEY]}</span>
            </a>
          ) : null}
        </div>
      </div>

      <BrowseGrid
        products={rows.map((r) => ({ ...r.product, uploadStatus: r.uploadStatus }))}
        filter={{
          status: status ?? undefined,
          severity: severity ?? undefined,
          category: category ?? undefined,
          uploadStatus: uploadStatus ?? undefined,
          q: q || undefined,
        }}
        totalMatching={Number(total)}
      />

      {pages > 1 ? (
        <div className="pager">
          {page > 1 ? (
            <a className="seg" href={qs({ page: String(page - 1) })}>
              ← Previous
            </a>
          ) : null}
          <span>
            Page {page} of {pages}
          </span>
          {page < pages ? (
            <a className="seg" href={qs({ page: String(page + 1) })}>
              Next →
            </a>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
