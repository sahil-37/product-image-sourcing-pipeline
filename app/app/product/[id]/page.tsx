import { desc, eq } from "drizzle-orm";
import Link from "next/link";
import { notFound } from "next/navigation";

import { db } from "@/lib/db";
import { versionedImageUrl } from "@/lib/imageUrl";
import { overrides, products, reviews, uploadJobs } from "@/lib/schema";

import ReviewControls from "./ReviewControls";

export const dynamic = "force-dynamic";

type Flag = { code: string; label: string; why: string };

function ago(d: Date | null): string {
  if (!d) return "";
  const s = Math.max(0, Math.floor((Date.now() - d.getTime()) / 1000));
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

export default async function ProductPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;

  const [row] = await db
    .select({ p: products, decision: reviews.decision, note: reviews.note, decidedAt: reviews.decidedAt })
    .from(products)
    .leftJoin(reviews, eq(reviews.productId, products.id))
    .where(eq(products.id, id));

  if (!row) notFound();
  const { p } = row;

  const [subs, jobs] = await Promise.all([
    db.select().from(overrides).where(eq(overrides.productId, id)).orderBy(desc(overrides.submittedAt)).limit(5),
    db.select().from(uploadJobs).where(eq(uploadJobs.productId, id)).orderBy(desc(uploadJobs.createdAt)).limit(5),
  ]);

  const flags = (p.flags as Flag[]) ?? [];
  const shown = versionedImageUrl(p.enhancedUrl || p.imageUrl, p.updatedAt);

  return (
    <div className="wrap">
      <nav className="crumb">
        <Link href="/">← All products</Link>
      </nav>

      <header className="dhead">
        <div>
          <p className="kicker">{p.batch}</p>
          <h1 className="dtitle">{p.name}</h1>
          <p className="dsub">
            <span className="mono">{p.id}</span>
            {p.source ? <> · found on {p.source}</> : null}
          </p>
        </div>
        <span className={`pill pill-${p.severity}`}>{p.severity}</span>
      </header>

      <div className="dgrid">
        <div>
          <div className="shot">
            {shown ? (
              // eslint-disable-next-line @next/next/no-img-element
              <img src={shown} alt={p.name} />
            ) : (
              <div className="noimg">No image was found for this product</div>
            )}
          </div>
          {p.sourceUrl ? (
            <a className="srclink" href={p.sourceUrl} target="_blank" rel="noopener noreferrer">
              View original source ↗
            </a>
          ) : null}
        </div>

        <div>
          <section className="panel">
            <h2 className="panel-h">Catalogue says / image shows</h2>
            <table className="cmp">
              <thead>
                <tr>
                  <th></th>
                  <th>Catalogue</th>
                  <th>In the photo</th>
                </tr>
              </thead>
              <tbody>
                <tr>
                  <th>Pack</th>
                  <td>{p.catPack ?? "—"}</td>
                  <td>{p.detPack ?? "—"}</td>
                </tr>
                <tr>
                  <th>Size</th>
                  <td>{p.catSize || "—"}</td>
                  <td>{p.detSize || "—"}</td>
                </tr>
                <tr>
                  <th>Variant</th>
                  <td>—</td>
                  <td>{p.detFlavour || "—"}</td>
                </tr>
                <tr>
                  <th>Packaging</th>
                  <td>—</td>
                  <td>{p.detContainer || "—"}</td>
                </tr>
              </tbody>
            </table>
            <dl className="meta" style={{ marginTop: 12 }}>
              <div>
                <dt>status</dt>
                <dd>{p.status}</dd>
              </div>
              <div>
                <dt>tier</dt>
                <dd>{p.tier ?? "—"}</dd>
              </div>
              <div>
                <dt>confidence</dt>
                <dd>{typeof p.confidence === "number" ? p.confidence.toFixed(2) : "—"}</dd>
              </div>
            </dl>
            {p.reason ? <p className="reason">{p.reason}</p> : null}
          </section>

          <section className="panel">
            <h2 className="panel-h">
              What wasn&rsquo;t confirmed {flags.length ? `(${flags.length})` : ""}
            </h2>
            {flags.length === 0 ? (
              <p className="panel-p">
                Every check that could run, ran, and none of them contradicted the catalogue. That
                is not proof the photo is perfect — it means nothing was detected.
              </p>
            ) : (
              <ul className="flaglist">
                {flags.map((f) => (
                  <li key={f.code}>
                    <span className="flag">{f.label}</span>
                    <span className="why">{f.why}</span>
                  </li>
                ))}
              </ul>
            )}
          </section>

          <ReviewControls
            productId={p.id}
            decision={row.decision ?? null}
            hasEnhanced={Boolean(p.enhancedUrl)}
          />

          {subs.length > 0 || jobs.length > 0 ? (
            <section className="panel">
              <h2 className="panel-h">Activity</h2>
              <ul className="events">
                {subs.map((s) => (
                  <li key={`o${s.id}`}>
                    <span className={`ev ev-${s.state}`}>{s.state}</span>
                    <span className="ev-t">replacement submitted {ago(s.submittedAt)}</span>
                    {s.error ? <span className="why">{s.error}</span> : null}
                  </li>
                ))}
                {jobs.map((j) => (
                  <li key={`j${j.id}`}>
                    <span className={`ev ev-${j.state}`}>{j.state}</span>
                    <span className="ev-t">
                      upload {j.origin === "override" ? "(replacement)" : ""} {ago(j.createdAt)}
                    </span>
                    {j.error ? <span className="why">{j.error}</span> : null}
                  </li>
                ))}
              </ul>
            </section>
          ) : null}
        </div>
      </div>
    </div>
  );
}
