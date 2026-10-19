import { beforeEach, describe, expect, it, vi } from "vitest";

// db is mocked before importing selection.ts, so resolveSelection/
// countSelection never touch a real Postgres connection -- these are unit
// tests of the id-mode / cap / filter-composition logic, not integration
// tests of Drizzle or Neon.
let mockRows: { id: string }[] = [];
let mockCount = 0;
let lastWhere: unknown = null;

vi.mock("../db", () => ({
  db: {
    select: (cols: Record<string, unknown>) => ({
      from: () => ({
        where: (clause: unknown) => {
          lastWhere = clause;
          // countSelection selects {n: ...}; resolveSelection selects {id: ...}.
          if ("n" in cols) return Promise.resolve([{ n: mockCount }]);
          return Promise.resolve(mockRows);
        },
      }),
    }),
  },
}));

import {
  MAX_SELECTION,
  NEVER_UPLOADED_KEY,
  UNCATEGORIZED_KEY,
  countSelection,
  resolveSelection,
} from "../selection";

// Recursively flattens a Drizzle SQL clause's query chunks into the raw SQL
// text it would render -- lets a test assert "this clause says IS NULL" (or
// doesn't) without depending on Drizzle's internal AST shape beyond the one
// stable fact that plain-string chunks carry the literal SQL text.
function sqlText(node: unknown): string {
  if (node == null) return "";
  if (typeof node === "string") return node;
  const n = node as { queryChunks?: unknown[]; value?: unknown };
  if (Array.isArray(n.queryChunks)) return n.queryChunks.map(sqlText).join("");
  if (n.value !== undefined) return String(n.value);
  return "";
}

describe("resolveSelection", () => {
  beforeEach(() => {
    mockRows = [];
    mockCount = 0;
  });

  describe("mode: ids", () => {
    it("returns the ids unchanged", async () => {
      const result = await resolveSelection({ mode: "ids", ids: ["1", "2", "3"] });
      expect(result).toEqual({ ids: ["1", "2", "3"] });
    });

    it("de-duplicates", async () => {
      const result = await resolveSelection({ mode: "ids", ids: ["1", "1", "2"] });
      expect("ids" in result && result.ids.sort()).toEqual(["1", "2"]);
    });

    it("drops empty/falsy entries", async () => {
      const result = await resolveSelection({ mode: "ids", ids: ["1", "", "2"] });
      expect("ids" in result && result.ids.sort()).toEqual(["1", "2"]);
    });

    it("errors on an empty selection", async () => {
      const result = await resolveSelection({ mode: "ids", ids: [] });
      expect(result).toEqual({ error: "no products selected" });
    });

    it("errors when over the cap, without ever querying the database", async () => {
      const ids = Array.from({ length: MAX_SELECTION + 1 }, (_, i) => String(i));
      const result = await resolveSelection({ mode: "ids", ids });
      expect("error" in result).toBe(true);
      expect((result as { error: string }).error).toContain(String(MAX_SELECTION));
    });

    it("allows exactly the cap", async () => {
      const ids = Array.from({ length: MAX_SELECTION }, (_, i) => String(i));
      const result = await resolveSelection({ mode: "ids", ids });
      expect("ids" in result).toBe(true);
    });
  });

  describe("mode: filter", () => {
    it("resolves to whatever the database matches", async () => {
      mockRows = [{ id: "10" }, { id: "20" }];
      const result = await resolveSelection({ mode: "filter", filter: { category: "DRINK" } });
      expect(result).toEqual({ ids: ["10", "20"] });
    });

    it("errors when nothing matches", async () => {
      mockRows = [];
      const result = await resolveSelection({ mode: "filter", filter: { status: "no_match" } });
      expect(result).toEqual({ error: "no products match that filter" });
    });

    it("enforces the cap against the ACTUAL resolved count, not an apparent size", async () => {
      // The exclude list already trimmed this filter's match to just 2 rows
      // by the time the (mocked) query runs -- resolveSelection should not
      // reject this just because the filter alone could in principle match
      // more; it only ever sees post-exclude results.
      mockRows = [{ id: "1" }, { id: "2" }];
      const result = await resolveSelection({
        mode: "filter",
        filter: { status: "not_sourced" },
        excludeIds: ["3", "4", "5"],
      });
      expect(result).toEqual({ ids: ["1", "2"] });
    });

    it("errors when the resolved count exceeds the cap", async () => {
      mockRows = Array.from({ length: MAX_SELECTION + 1 }, (_, i) => ({ id: String(i) }));
      const result = await resolveSelection({ mode: "filter", filter: { status: "not_sourced" } });
      expect("error" in result).toBe(true);
      expect((result as { error: string }).error).toContain(String(mockRows.length));
    });

    it("UNCATEGORIZED_KEY queries category IS NULL, not category = the sentinel string", async () => {
      // The sentinel is a plain filter value on the wire (so it round-trips
      // through a URL query param and the persisted selection just like a
      // real category name), but it must never reach the database as
      // `category = '__uncategorized__'` -- that would just never match,
      // silently returning zero rows for every one of the 25 real
      // uncategorized products instead of finding them.
      mockRows = [{ id: "1" }];
      await resolveSelection({ mode: "filter", filter: { category: UNCATEGORIZED_KEY } });
      expect(sqlText(lastWhere)).toContain("is null");
      expect(sqlText(lastWhere)).not.toContain(UNCATEGORIZED_KEY);
    });

    it("a real category name still queries category = <name>, not IS NULL", async () => {
      mockRows = [{ id: "1" }];
      await resolveSelection({ mode: "filter", filter: { category: "FOOD" } });
      expect(sqlText(lastWhere)).not.toContain("is null");
    });

    it("filters by uploadStatus using the latest upload_jobs row per product", async () => {
      mockRows = [{ id: "1" }];
      await resolveSelection({ mode: "filter", filter: { uploadStatus: "failed" } });
      expect(sqlText(lastWhere)).toContain("upload_jobs");
    });

    it("uploadStatus 'all' is not filtered (matches every product, uploaded or not)", async () => {
      mockRows = [{ id: "1" }];
      await resolveSelection({ mode: "filter", filter: { uploadStatus: "all" } });
      expect(sqlText(lastWhere)).not.toContain("upload_jobs");
    });

    it(`uploadStatus ${NEVER_UPLOADED_KEY} is a real, queryable value, not skipped like 'all'`, async () => {
      mockRows = [{ id: "1" }];
      await resolveSelection({ mode: "filter", filter: { uploadStatus: NEVER_UPLOADED_KEY } });
      expect(sqlText(lastWhere)).toContain("upload_jobs");
    });
  });
});

describe("countSelection", () => {
  beforeEach(() => {
    mockCount = 0;
  });

  it("returns the database's count", async () => {
    mockCount = 42;
    const n = await countSelection({ category: "DRINK" });
    expect(n).toBe(42);
  });

  it("returns zero when nothing matches", async () => {
    mockCount = 0;
    const n = await countSelection({ status: "impossible" });
    expect(n).toBe(0);
  });
});
