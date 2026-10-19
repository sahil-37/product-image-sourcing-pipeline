import Link from "next/link";
import { and, count, desc, eq, ilike, sql } from "drizzle-orm";

import { db } from "@/lib/db";
import { versionedImageUrl } from "@/lib/imageUrl";
import { products, reviews } from "@/lib/schema";

export const dynamic = "force-dynamic";

const PAGE_SIZE = 60;

type Flag = { code: string; label: string; why: string };

type Search = {
  severity?: string;
  batch?: string;
  q?: string;
  page?: string;
};

/** Blunder first: the rows that need a human are the rows worth showing first. */
const SEVERITY_RANK = sql`case ${products.severity}
  when 'blunder' then 0 when 'flagged' then 1 else 2 end`;

export default async function Home({
  searchParams,
}: {
  searchParams: Promise<Search>;
}) {
  const sp = await searchParams;
  const page = Math.max(1, parseInt(sp.page ?? "1", 10) || 1);
  const severity = sp.severity && sp.severity !== "all" ? sp.severity : null;
  const q = (sp.q ?? "").trim();

  const where = and(
    severity ? eq(products.severity, severity) : undefined,
    sp.batch ? eq(products.batch, sp.batch) : undefined,
    q ? ilike(products.name, `%${q}%`) : undefined,
  );

  const [rows, [{ total }], tallies] = await Promise.all([
    db
      .select({
        p: products,
        decision: reviews.decision,
      })
      .from(products)
      .leftJoin(reviews, eq(reviews.productId, products.id))
      .where(where)
      .orderBy(SEVERITY_RANK, products.name)
      .limit(PAGE_SIZE)
      .offset((page - 1) * PAGE_SIZE),
    db.select({ total: count() }).from(products).where(where),
    db
      .select({ severity: products.severity, n: count() })
      .from(products)
      .groupBy(products.severity),
  ]);

  const by = Object.fromEntries(tallies.map((t) => [t.severity, Number(t.n)]));
  const all = (by.clean ?? 0) + (by.flagged ?? 0) + (by.blunder ?? 0);
  const pages = Math.max(1, Math.ceil(Number(total) / PAGE_SIZE));

  // Replacements are processed by a worker on someone's machine, so the queue
  // can legitimately sit still. Say so, rather than leaving people guessing.
  const queueRes = await db.execute<{ waiting: number; stalled: number; age: number | null }>(
    sql`select
          (select count(*) from upload_jobs where state in ('queued','claimed','uploading'))::int as waiting,
          (select count(*) from upload_jobs where state = 'failed' and error like 'auth_expired%')::int as stalled,
          (select extract(epoch from now() - max(last_seen))::int from workers) as age`,
  );
  const queue = queueRes.rows[0] ?? null;

  const qs = (over: Partial<Search>) => {
    const u = new URLSearchParams();
    const merged = { severity: sp.severity, batch: sp.batch, q: sp.q, ...over };
    for (const [k, v] of Object.entries(merged)) if (v) u.set(k, String(v));
    return `/?${u.toString()}`;
  };

  return (
    <div className="wrap">
      <header className="masthead">
        <p className="kicker">Phase 1 · automated product imagery</p>
        <h1>Catalogue Image Review</h1>
        <p className="lede">
          Every product the pipeline found an image for, and every check it could not complete.
          A flag never blocks an upload — it marks what a person should confirm.
        </p>
        <p>
          <Link className="seg" href="/browse">
            Browse &amp; select products →
          </Link>
        </p>

        <div className="stats">
          <div className="stat">
            <div className="stat-n">{all}</div>
            <div className="stat-l">Products</div>
          </div>
          <div className="stat is-clean">
            <div className="stat-n">{by.clean ?? 0}</div>
            <div className="stat-l">Clean</div>
          </div>
          <div className="stat is-flagged">
            <div className="stat-n">{by.flagged ?? 0}</div>
            <div className="stat-l">Flagged</div>
          </div>
          <div className="stat is-blunder">
            <div className="stat-n">{by.blunder ?? 0}</div>
            <div className="stat-l">No usable image</div>
          </div>
        </div>
      </header>

      {queue?.stalled ? (
        <div className="banner">
          <strong>Uploads paused.</strong> The OMS session has expired, so {queue.stalled} upload
          {queue.stalled === 1 ? "" : "s"} could not be delivered. Someone needs to sign in again
          before these will go through — your replacements are saved and will be sent once it is
          renewed.
        </div>
      ) : queue?.waiting ? (
        <div className="qnote">
          {queue.waiting} image{queue.waiting === 1 ? "" : "s"} waiting to upload ·{" "}
          {queue.age === null
            ? "the uploader has not run yet"
            : queue.age < 120
              ? "uploader is running"
              : `uploader last seen ${Math.floor(queue.age / 60)}m ago`}
        </div>
      ) : null}

      <div className="controls">
        <form className="crow" action="/" method="get">
          {sp.severity ? <input type="hidden" name="severity" value={sp.severity} /> : null}
          <input
            className="search"
            type="search"
            name="q"
            defaultValue={q}
            placeholder="Filter by product name…"
            aria-label="Filter by product name"
          />
          <Link className="seg" data-on={!severity} href={qs({ severity: "", page: "" })}>
            All
          </Link>
          <Link
            className="seg"
            data-on={severity === "blunder"}
            href={qs({ severity: "blunder", page: "" })}
          >
            No image <span className="seg-n">{by.blunder ?? 0}</span>
          </Link>
          <Link
            className="seg"
            data-on={severity === "flagged"}
            href={qs({ severity: "flagged", page: "" })}
          >
            Flagged <span className="seg-n">{by.flagged ?? 0}</span>
          </Link>
          <Link
            className="seg"
            data-on={severity === "clean"}
            href={qs({ severity: "clean", page: "" })}
          >
            Clean <span className="seg-n">{by.clean ?? 0}</span>
          </Link>
          <span className="count">
            {Number(total)} match{Number(total) === 1 ? "" : "es"}
          </span>
        </form>
      </div>

      {rows.length === 0 ? (
        <div className="empty">No products match these filters.</div>
      ) : (
        <div className="grid">
          {rows.map(({ p, decision }) => (
            <Link key={p.id} href={`/product/${p.id}`} className={`card sev-${p.severity}`}>
              <div className="thumb">
                {p.thumbUrl ? (
                  // eslint-disable-next-line @next/next/no-img-element
                  <img
                    src={versionedImageUrl(p.thumbUrl, p.updatedAt) ?? p.thumbUrl}
                    alt={p.name}
                    loading="lazy"
                    width={92}
                    height={92}
                  />
                ) : (
                  <div className="noimg">no image</div>
                )}
              </div>
              <div className="cbody">
                <div className="ctop">
                  <h3 className="cname">{p.name}</h3>
                  <span className={`pill pill-${p.severity}`}>{p.severity}</span>
                </div>

                <dl className="meta">
                  <div>
                    <dt>id</dt>
                    <dd>{p.id}</dd>
                  </div>
                  <div>
                    <dt>pack</dt>
                    <dd>
                      {p.catPack ?? "—"} → {p.detPack ?? "—"}
                    </dd>
                  </div>
                  <div>
                    <dt>size</dt>
                    <dd>
                      {p.catSize || "—"} → {p.detSize || "—"}
                    </dd>
                  </div>
                  <div>
                    <dt>tier</dt>
                    <dd>{p.tier ?? "—"}</dd>
                  </div>
                </dl>

                <div className="flagrow">
                  {decision ? <span className="decision">{decision}</span> : null}
                  {(p.flags as Flag[]).length === 0 ? (
                    <span className="nof">no flags</span>
                  ) : (
                    (p.flags as Flag[]).map((f) => (
                      <span key={f.code} className="flag" title={f.why}>
                        {f.label}
                      </span>
                    ))
                  )}
                </div>
              </div>
            </Link>
          ))}
        </div>
      )}

      {pages > 1 ? (
        <div className="pager">
          {page > 1 ? (
            <Link className="seg" href={qs({ page: String(page - 1) })}>
              ← Previous
            </Link>
          ) : null}
          <span>
            Page {page} of {pages}
          </span>
          {page < pages ? (
            <Link className="seg" href={qs({ page: String(page + 1) })}>
              Next →
            </Link>
          ) : null}
        </div>
      ) : null}

      <footer className="foot">
        <p>
          <strong>How to read this.</strong> Flags come from the verifier&rsquo;s own notes on the
          chosen photo. They record what could not be confirmed, not what is wrong — a flagged
          product is still the right product as far as every check could tell, and it ships.
        </p>
        <p>
          <strong>Clean is not a guarantee.</strong> Spot-checking found photos carrying a store
          watermark that the verifier reported as clean. &ldquo;Clean&rdquo; means nothing was
          detected, not that nothing is there. The flagged rows deserve attention first, but the
          clean ones still deserve a glance.
        </p>
      </footer>
    </div>
  );
}
