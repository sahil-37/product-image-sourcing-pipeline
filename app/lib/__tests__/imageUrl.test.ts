import { describe, expect, it } from "vitest";

import { versionedImageUrl } from "../imageUrl";

// The bug this exists to fix: local image paths are deterministic and
// reused on every re-push (/images/thumb/<batch>/<id>.jpg never changes for
// a given id), so a re-sourced product's new photo was served from browser
// cache -- same URL string, no reason for the browser (or React's own prop
// diffing on the <img> tag) to treat it as new. Appending `updated_at` as a
// version param makes the URL genuinely different whenever the row changes.
describe("versionedImageUrl", () => {
  it("appends a version query param derived from updatedAt", () => {
    const url = versionedImageUrl("/images/thumb/drinks/100.jpg", new Date("2026-01-01T00:00:00Z"));
    expect(url).toBe(`/images/thumb/drinks/100.jpg?v=${new Date("2026-01-01T00:00:00Z").getTime()}`);
  });

  it("accepts a date as a string, not just a Date object", () => {
    const url = versionedImageUrl("/images/thumb/drinks/100.jpg", "2026-01-01T00:00:00Z");
    expect(url).toContain("?v=");
  });

  it("returns null for a null url -- nothing to version", () => {
    expect(versionedImageUrl(null, new Date())).toBeNull();
  });

  it("returns the bare url when updatedAt is null", () => {
    expect(versionedImageUrl("/images/thumb/drinks/100.jpg", null)).toBe("/images/thumb/drinks/100.jpg");
  });

  it("returns the bare url when updatedAt doesn't parse to a valid date", () => {
    expect(versionedImageUrl("/images/thumb/drinks/100.jpg", "not-a-date")).toBe(
      "/images/thumb/drinks/100.jpg",
    );
  });

  it("uses & when the url already has a query string", () => {
    const url = versionedImageUrl("https://example.com/img.jpg?w=200", new Date("2026-01-01T00:00:00Z"));
    expect(url).toBe(`https://example.com/img.jpg?w=200&v=${new Date("2026-01-01T00:00:00Z").getTime()}`);
  });
});
