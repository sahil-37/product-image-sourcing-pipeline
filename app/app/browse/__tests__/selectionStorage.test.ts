import { describe, expect, it } from "vitest";

import { isSelectionStaleForFilter } from "../selectionStorage";

// Regression test: a "select all N matching" selection used to persist
// across a filter change with nothing checking whether it still applied.
// Every card on the new page rendered checked (isSelected only consults the
// exclude list, never sel.filter) while an action would still silently
// resolve the OLD filter server-side -- selected-looking cards acting on a
// different set than what was displayed.
describe("isSelectionStaleForFilter", () => {
  it("is never stale for an id-mode selection, regardless of filter", () => {
    const sel = { mode: "ids" as const, ids: ["1", "2"] };
    expect(isSelectionStaleForFilter(sel, { status: "accepted" })).toBe(false);
    expect(isSelectionStaleForFilter(sel, {})).toBe(false);
  });

  it("is not stale when the filter is unchanged (a same-filter page flip)", () => {
    const sel = { mode: "filter" as const, filter: { status: "not_sourced" }, excludeIds: [] };
    expect(isSelectionStaleForFilter(sel, { status: "not_sourced" })).toBe(false);
  });

  it("is stale when the status filter changes", () => {
    const sel = { mode: "filter" as const, filter: { status: "not_sourced" }, excludeIds: [] };
    expect(isSelectionStaleForFilter(sel, { status: "accepted" })).toBe(true);
  });

  it("is stale when a category/search term is added to an otherwise equal filter", () => {
    const sel = { mode: "filter" as const, filter: { status: "not_sourced" }, excludeIds: ["3"] };
    expect(isSelectionStaleForFilter(sel, { status: "not_sourced", category: "FOOD" })).toBe(true);
  });

  it("ignores excludeIds -- only the filter itself determines staleness", () => {
    const sel = { mode: "filter" as const, filter: { status: "not_sourced" }, excludeIds: ["3", "4"] };
    expect(isSelectionStaleForFilter(sel, { status: "not_sourced" })).toBe(false);
  });
});
