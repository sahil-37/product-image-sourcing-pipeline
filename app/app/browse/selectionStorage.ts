import type { ProductFilter } from "@/lib/selection";

// Selection persists across page/filter navigation (a fresh server render on
// every link click otherwise wiped it) but not across tabs or browser
// restarts -- a working selection, not permanent state.
export const STORAGE_KEY = "lowprice.browse.selection";

/**
 * Either a plain set of ids (built by clicking individual cards / "select all
 * on this page"), or "every product matching the given filter" plus an
 * exclude list (built by clicking "select all N matching" and then
 * deselecting a few) -- mirrors lib/selection.ts's Selection type so the
 * client never has to materialize thousands of ids just to represent
 * "everything not yet sourced".
 */
export type StoredSelection =
  | { mode: "ids"; ids: string[] }
  | { mode: "filter"; filter: ProductFilter; excludeIds: string[] };

export function loadSelection(): StoredSelection {
  try {
    const raw = sessionStorage.getItem(STORAGE_KEY);
    if (raw) return JSON.parse(raw);
  } catch {
    // ignore -- private browsing, corrupted value, etc.
  }
  return { mode: "ids", ids: [] };
}

export function saveSelection(sel: StoredSelection) {
  try {
    sessionStorage.setItem(STORAGE_KEY, JSON.stringify(sel));
  } catch {
    // storage full or unavailable -- selection just won't survive navigation
  }
}

/**
 * True when a stored filter-mode selection no longer describes the page
 * being shown -- the user changed status/category/search since picking
 * "select all N matching", so the old filter's selection means nothing here.
 * id-mode selections are never stale this way: they're explicit ids, good on
 * any page/filter.
 *
 * This is the fix for a real bug: without this check, isSelected() on a new,
 * differently-filtered page only consults the exclude list -- never whether
 * an id actually matches sel.filter -- so every card rendered checked while
 * an action would still silently resolve the OLD filter server-side,
 * operating on a different product set than what was visibly selected.
 */
export function isSelectionStaleForFilter(sel: StoredSelection, filter: ProductFilter): boolean {
  return sel.mode === "filter" && JSON.stringify(sel.filter) !== JSON.stringify(filter);
}

// Same problem as selection, same fix: a sourcing run is a detached
// background process on the server (run_source_job.py) that keeps running
// regardless of what the browser does, but BrowseGrid's sourcing/sourceStatus
// state used to be plain useState -- reset to nothing every time /browse's
// server component re-renders, which includes clicking to a different page
// or filter, not just a hard refresh. The progress bar would vanish while
// the run was very much still going, with no way back to it short of
// remembering the run id and guessing at a URL. Only the run id needs
// persisting -- status itself is always re-fetched fresh from the server.
const ACTIVE_RUN_KEY = "lowprice.browse.activeSourceRun";

export function loadActiveRunId(): string | null {
  try {
    return sessionStorage.getItem(ACTIVE_RUN_KEY);
  } catch {
    return null;
  }
}

export function saveActiveRunId(runId: string | null) {
  try {
    if (runId) sessionStorage.setItem(ACTIVE_RUN_KEY, runId);
    else sessionStorage.removeItem(ACTIVE_RUN_KEY);
  } catch {
    // storage full or unavailable -- the run keeps going server-side either
    // way, this only affects whether the UI can find its way back to it
  }
}
