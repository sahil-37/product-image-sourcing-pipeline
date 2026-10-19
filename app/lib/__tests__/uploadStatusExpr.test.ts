import { neon } from "@neondatabase/serverless";
import { count } from "drizzle-orm";
import { drizzle } from "drizzle-orm/neon-http";
import { describe, expect, it, vi } from "vitest";

// selection.ts imports the real db client, which demands DATABASE_URL.
vi.mock("../db", () => ({ db: {} }));

import { products } from "../schema";
import { uploadStatusExpr } from "../selection";

// toSQL() only renders; nothing connects, so a placeholder URL is enough.
const db = drizzle(neon("postgresql://user:pass@localhost/db"));

describe("uploadStatusExpr", () => {
  // Regression: in a select list Drizzle rendered products.id as a bare "id",
  // which inside the upload_jobs subquery means upload_jobs.id. Every product
  // then read as never-uploaded on /browse's cards and Upload tallies.
  it("points the subquery at products.id in a select list and GROUP BY", () => {
    const { sql } = db
      .select({ s: uploadStatusExpr, n: count() })
      .from(products)
      .groupBy(uploadStatusExpr)
      .toSQL();
    expect(sql).not.toMatch(/product_id = "id"/);
    expect(sql.match(/product_id = "products"\."id"::text/g)).toHaveLength(2);
    // Select-list and GROUP BY copies must render identically for Postgres to
    // treat them as the same expression.
    const [selectPart, groupPart] = sql.split(" group by ");
    expect(groupPart).toBeDefined();
    expect(selectPart).toContain(groupPart);
  });
});
