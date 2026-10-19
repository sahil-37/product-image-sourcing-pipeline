/**
 * Appends a cache-busting version to a local image URL, so a re-sourced
 * product's new photo displays immediately instead of the browser (and even
 * React, which skips re-rendering an <img> whose src prop is byte-identical)
 * serving the stale one.
 *
 * Local image paths are deterministic and reused on every re-push --
 * `/images/thumb/<batch>/<id>.jpg` is the exact same string before and after
 * a re-source, by design (push_to_web.py's local_put(): "Overwriting the
 * same path is how a re-push naturally stays idempotent"). That's correct
 * for the file on disk, but it means the URL alone can never signal "this
 * changed" to a client that's already seen it -- nothing about the string
 * is different. `updated_at` is bumped on every sync upsert regardless of
 * whether the image bytes actually changed, so it's a safe, always-available
 * version token: at worst a rare products row that just isn't a local path
 * (e.g. a stale Blob URL, or a stakeholder-submitted http(s) override) gets
 * an inert query param appended, which every server treats as ignorable.
 */
export function versionedImageUrl(url: string | null, updatedAt: Date | string | null): string | null {
  if (!url) return null;
  if (!updatedAt) return url;
  const t = updatedAt instanceof Date ? updatedAt.getTime() : new Date(updatedAt).getTime();
  if (Number.isNaN(t)) return url;
  return `${url}${url.includes("?") ? "&" : "?"}v=${t}`;
}
