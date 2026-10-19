"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { useRouter } from "next/navigation";

import { bulkApproveAndQueue } from "@/lib/actions";
import { versionedImageUrl } from "@/lib/imageUrl";
import type { Product } from "@/lib/schema";
import type { ProductFilter, Selection } from "@/lib/selection";

import {
  isSelectionStaleForFilter,
  loadActiveRunId,
  loadSelection,
  saveActiveRunId,
  saveSelection,
  type StoredSelection,
} from "./selectionStorage";

type Flag = { code: string; label: string; why: string };

// The latest upload_jobs outcome for this product, computed server-side
// (lib/selection.ts's uploadStatusExpr) -- not a real products column, since
// a product can have many historical jobs and only the newest one is what
// "has this shipped to the OMS" means.
type ProductWithUploadStatus = Product & { uploadStatus: string };

type SourceStatus = {
  status: "pending" | "running" | "done" | "error";
  total?: number;
  done?: number;
  current_name?: string;
  error?: string;
  results?: { id: string; name: string; status: string; confidence: number | null }[];
};

export default function BrowseGrid({
  products,
  filter,
  totalMatching,
}: {
  products: ProductWithUploadStatus[];
  filter: ProductFilter;
  totalMatching: number;
}) {
  const router = useRouter();
  const [sel, setSel] = useState<StoredSelection>({ mode: "ids", ids: [] });
  const [hydrated, setHydrated] = useState(false);
  const [sourcing, setSourcing] = useState<{ runId: string } | null>(null);
  const [sourceStatus, setSourceStatus] = useState<SourceStatus | null>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // Read sessionStorage only on mount, client-side -- avoids a
  // server/client render mismatch on first paint.
  useEffect(() => {
    setSel(loadSelection());
    // Resume watching an in-flight run across a page/filter navigation that
    // remounted this component -- run_source_job.py keeps running server-side
    // regardless, so without this the progress bar just vanishes with no way
    // back to it, even though the run is still very much going.
    const runId = loadActiveRunId();
    if (runId) setSourcing({ runId });
    setHydrated(true);
  }, []);

  useEffect(() => {
    if (hydrated) saveSelection(sel);
  }, [sel, hydrated]);

  // A filter-mode selection means "everything matching THIS filter" -- if the
  // user navigates to a different filter (a new status/category/search, not
  // just a new page of the same one), a stored selection from the old filter
  // no longer describes anything real. Without this, every card on the new
  // page would render checked (isSelected only checks the exclude list, never
  // whether the id matches sel.filter) while an action would still silently
  // resolve the stale filter server-side -- selected-looking cards acting on
  // a different set than what's displayed. Same-filter page flips are
  // unaffected: filter is unchanged, so this never fires.
  useEffect(() => {
    if (!hydrated) return;
    if (isSelectionStaleForFilter(sel, filter)) {
      setSel({ mode: "ids", ids: [] });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filter, hydrated]);

  const selectedIdSet = sel.mode === "ids" ? new Set(sel.ids) : null;
  const excludeSet = sel.mode === "filter" ? new Set(sel.excludeIds) : null;

  function isSelected(id: string): boolean {
    if (sel.mode === "ids") return selectedIdSet!.has(id);
    return !excludeSet!.has(id);
  }

  function toggle(id: string) {
    setSel((prev) => {
      if (prev.mode === "ids") {
        const ids = new Set(prev.ids);
        if (ids.has(id)) ids.delete(id);
        else ids.add(id);
        return { mode: "ids", ids: [...ids] };
      }
      // Filter mode: toggling means adding/removing from the exclude list.
      const excludeIds = new Set(prev.excludeIds);
      if (excludeIds.has(id)) excludeIds.delete(id);
      else excludeIds.add(id);
      return { ...prev, excludeIds: [...excludeIds] };
    });
  }

  const allOnPageSelected = products.length > 0 && products.every((p) => isSelected(p.id));

  function toggleAllOnPage() {
    setSel((prev) => {
      if (prev.mode === "filter") {
        // Selecting/deselecting "this page" while in filter mode just
        // adjusts the exclude list for the page's ids.
        const excludeIds = new Set(prev.excludeIds);
        if (allOnPageSelected) for (const p of products) excludeIds.add(p.id);
        else for (const p of products) excludeIds.delete(p.id);
        return { ...prev, excludeIds: [...excludeIds] };
      }
      const ids = new Set(prev.ids);
      if (allOnPageSelected) for (const p of products) ids.delete(p.id);
      else for (const p of products) ids.add(p.id);
      return { mode: "ids", ids: [...ids] };
    });
  }

  function selectAllMatching() {
    setSel({ mode: "filter", filter, excludeIds: [] });
  }

  function clear() {
    setSel({ mode: "ids", ids: [] });
  }

  // Count shown in the toolbar/action bar: exact for id-mode, "N matching -
  // K removed" is collapsed to a plain count here since the exact number
  // requires the same server round trip resolveSelection already does when
  // an action actually runs.
  const count =
    sel.mode === "ids" ? sel.ids.length : Math.max(0, totalMatching - sel.excludeIds.length);

  const asSelection: Selection = useMemo(
    () =>
      sel.mode === "ids"
        ? { mode: "ids", ids: sel.ids }
        : { mode: "filter", filter: sel.filter, excludeIds: sel.excludeIds },
    [sel],
  );

  useEffect(() => {
    if (!sourcing) return;
    const poll = async () => {
      const res = await fetch(`/api/source/status/${sourcing.runId}`);
      const data: SourceStatus = await res.json();
      setSourceStatus(data);
      if (data.status === "done" || data.status === "error") {
        if (pollRef.current) clearInterval(pollRef.current);
        saveActiveRunId(null); // finished -- nothing left to resume watching
        router.refresh(); // pick up the newly-sourced rows/images
      }
    };
    poll();
    pollRef.current = setInterval(poll, 2000);
    return () => {
      if (pollRef.current) clearInterval(pollRef.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sourcing]);

  async function startSourcing() {
    const res = await fetch("/api/source/run", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(asSelection),
    });
    const data = await res.json();
    if (!res.ok) {
      setSourceStatus({ status: "error", error: data.error ?? "could not start" });
      return;
    }
    saveActiveRunId(data.run_id);
    setSourcing({ runId: data.run_id });
    setSourceStatus({ status: "pending", total: data.count, done: 0 });
  }

  const [zipBusy, setZipBusy] = useState(false);

  async function downloadZip() {
    setZipBusy(true);
    try {
      const res = await fetch("/api/download-zip", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(asSelection),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        setSourceStatus({ status: "error", error: data.error ?? "download failed" });
        return;
      }
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `product-images-${Date.now()}.zip`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } finally {
      setZipBusy(false);
    }
  }

  const [queueBusy, setQueueBusy] = useState(false);
  const [queueResult, setQueueResult] = useState<
    { queued: string[]; skipped: string[]; error?: string } | null
  >(null);

  async function uploadViaWorker() {
    setQueueBusy(true);
    setQueueResult(null);
    try {
      const result = await bulkApproveAndQueue(asSelection);
      setQueueResult(result);
      router.refresh();
    } finally {
      setQueueBusy(false);
    }
  }

  const busy = sourceStatus?.status === "running" || zipBusy || queueBusy;

  return (
    <>
      <div className="browse-toolbar">
        <label className="pick-all">
          <input type="checkbox" checked={allOnPageSelected} onChange={toggleAllOnPage} />
          Select all on this page
        </label>
        {totalMatching > products.length ? (
          <button className="linklike" type="button" onClick={selectAllMatching}>
            select all {totalMatching} matching
          </button>
        ) : null}
        {count > 0 ? (
          <>
            <span className="pick-count">
              {count} selected
              {sel.mode === "filter" ? " (matching current filter)" : ""}
            </span>
            <button className="linklike" onClick={clear} type="button">
              clear
            </button>
          </>
        ) : null}
      </div>

      {products.length === 0 ? (
        <div className="empty">No products match these filters.</div>
      ) : (
        <div className={`grid${busy ? " is-busy" : ""}`}>
          {products.map((p) => {
            const selected = isSelected(p.id);
            const flags = (p.flags as Flag[]) ?? [];
            // A sourced image with no watermarked version yet -- either
            // watermarking hasn't run (run_source_job.py only attempts it
            // for accepted/review), or it ran and failed silently (logged to
            // the run's file, never surfaced here before this badge). Ships
            // are OMS-bound through Download ZIP or Upload via worker, both
            // of which must never substitute the unbranded original for
            // this without it being visible first.
            const needsWatermark = Boolean(p.imageUrl) && !p.enhancedUrl;
            // "never_uploaded" is the default state for most of the
            // catalogue -- no badge for it, same as "no flags" below, so the
            // grid isn't wall-to-wall pills for the common case.
            const uploadPill =
              p.uploadStatus === "uploaded"
                ? { cls: "pill-uploaded", label: "uploaded" }
                : p.uploadStatus === "failed"
                  ? { cls: "pill-upload-failed", label: "upload failed" }
                  : p.uploadStatus === "queued"
                    ? { cls: "pill-upload-queued", label: "upload queued" }
                    : null;
            return (
              <div
                key={p.id}
                className={`card sev-${p.severity}${selected ? " is-selected" : ""}`}
                onClick={() => toggle(p.id)}
                role="checkbox"
                aria-checked={selected}
                tabIndex={0}
                onKeyDown={(e) => {
                  if (e.key === " " || e.key === "Enter") {
                    e.preventDefault();
                    toggle(p.id);
                  }
                }}
              >
                <input
                  className="card-check"
                  type="checkbox"
                  checked={selected}
                  onChange={() => toggle(p.id)}
                  onClick={(e) => e.stopPropagation()}
                  aria-label={`select ${p.name}`}
                />
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

                  {needsWatermark ? (
                    <span
                      className="pill pill-no-watermark"
                      title="No watermarked image yet — Download ZIP and Upload via worker both skip this product rather than ship the unbranded original"
                    >
                      no watermark
                    </span>
                  ) : null}

                  {uploadPill ? (
                    <span
                      className={`pill ${uploadPill.cls}`}
                      title={
                        p.uploadStatus === "failed"
                          ? "Most recent upload_jobs attempt for this product ended in failed — see the product page for the error"
                          : p.uploadStatus === "queued"
                            ? "Queued/claimed in upload_jobs — worker.py will pick this up"
                            : "Most recent upload_jobs attempt for this product succeeded"
                      }
                    >
                      {uploadPill.label}
                    </span>
                  ) : null}

                  <dl className="meta">
                    <div>
                      <dt>id</dt>
                      <dd>{p.id}</dd>
                    </div>
                    <div>
                      <dt>status</dt>
                      <dd>{p.status}</dd>
                    </div>
                    <div>
                      <dt>category</dt>
                      <dd>{p.category ?? "uncategorized"}</dd>
                    </div>
                  </dl>

                  <div className="flagrow">
                    {flags.length === 0 ? (
                      <span className="nof">no flags</span>
                    ) : (
                      flags.map((f) => (
                        <span key={f.code} className="flag" title={f.why}>
                          {f.label}
                        </span>
                      ))
                    )}
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      )}

      {count > 0 || sourceStatus || queueResult ? (
        <div className="action-bar" role="toolbar" aria-label="Selection actions">
          {count > 0 ? <span className="action-count">{count} selected</span> : null}
          <button
            className="btn-action"
            type="button"
            disabled={count === 0 || busy}
            onClick={startSourcing}
          >
            Source images
          </button>
          <button
            className="btn-action"
            type="button"
            disabled={count === 0 || busy}
            onClick={downloadZip}
            title="Named <item-id>.jpg, ready for the OMS's bulk-upload-by-item-number"
          >
            {zipBusy ? "Zipping…" : "Download ZIP"}
          </button>
          <button
            className="btn-action"
            type="button"
            disabled={count === 0 || busy}
            onClick={uploadViaWorker}
            title="Approves the selection and queues it for worker.py + the OMS — the automated, Playwright-driven path"
          >
            {queueBusy ? "Queuing…" : "Upload via worker"}
          </button>

          {sourceStatus ? (
            <span className="source-progress">
              {sourceStatus.status === "error"
                ? `Error: ${sourceStatus.error}`
                : sourceStatus.status === "done"
                  ? `Done — ${sourceStatus.total ?? 0} sourced`
                  : `Sourcing ${sourceStatus.done ?? 0}/${sourceStatus.total ?? "?"}` +
                    (sourceStatus.current_name ? ` — ${sourceStatus.current_name}` : "")}
              {sourceStatus.status !== "running" ? (
                <button className="linklike" type="button" onClick={() => setSourceStatus(null)}>
                  ×
                </button>
              ) : null}
            </span>
          ) : null}

          {queueResult ? (
            <span className="source-progress">
              {queueResult.error
                ? `Error: ${queueResult.error}`
                : (queueResult.queued.length > 0 ? `Queued ${queueResult.queued.length} for upload` : "") +
                  (queueResult.skipped.length > 0
                    ? ` — ${queueResult.skipped.length} skipped (no watermarked image yet: ${queueResult.skipped.join(", ")})`
                    : "")}
              <button className="linklike" type="button" onClick={() => setQueueResult(null)}>
                ×
              </button>
            </span>
          ) : null}
        </div>
      ) : null}
    </>
  );
}
