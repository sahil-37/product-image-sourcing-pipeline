"use client";

import { useState, useTransition } from "react";

import { decide, submitOverride, undecide } from "@/lib/actions";

type Props = {
  productId: string;
  decision: string | null;
  hasEnhanced: boolean;
};

export default function ReviewControls({ productId, decision, hasEnhanced }: Props) {
  const [pending, start] = useTransition();
  // Optimistic: reflect the click immediately, reconcile when the action lands.
  const [shown, setShown] = useState<string | null>(decision);
  const [url, setUrl] = useState("");
  const [note, setNote] = useState("");
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [preview, setPreview] = useState("");

  function choose(next: string) {
    const before = shown;
    setShown(next);
    setMsg(null);
    start(async () => {
      try {
        if (next === before) {
          setShown(null);
          await undecide(productId);
        } else {
          await decide(productId, next);
        }
      } catch {
        setShown(before);
        setMsg({ ok: false, text: "Couldn't save that. Try again." });
      }
    });
  }

  function send(e: React.FormEvent) {
    e.preventDefault();
    const value = url.trim();
    if (!value) return;
    start(async () => {
      const res = await submitOverride(productId, value, note);
      if (res.ok) {
        setMsg({ ok: true, text: "Replacement queued. It will be branded and uploaded shortly." });
        setShown("needs_new_image");
        setPreview(value);
        setUrl("");
        setNote("");
      } else {
        setMsg({ ok: false, text: res.error ?? "That didn't work." });
      }
    });
  }

  return (
    <section className="panel">
      <h2 className="panel-h">Your decision</h2>

      <div className="btnrow">
        <button
          type="button"
          className="btn btn-approve"
          data-on={shown === "approved"}
          disabled={pending}
          onClick={() => choose("approved")}
        >
          {shown === "approved" ? "✓ Approved" : "Approve"}
        </button>
        <button
          type="button"
          className="btn btn-reject"
          data-on={shown === "rejected"}
          disabled={pending}
          onClick={() => choose("rejected")}
        >
          {shown === "rejected" ? "✓ Rejected" : "Reject"}
        </button>
        {shown ? <span className="hint">Click again to clear</span> : null}
      </div>

      {shown === "approved" && !hasEnhanced ? (
        <p className="warn">
          Approved, but there is no watermarked version of this image yet, so nothing has been
          queued for upload. Run the watermark step, then approve again.
        </p>
      ) : null}

      <h2 className="panel-h" style={{ marginTop: 22 }}>
        Supply a better image
      </h2>
      <p className="panel-p">
        Paste a direct link to the product photo. It will be squared, watermarked with the house
        logo, and uploaded in place of the one above.
      </p>

      <form onSubmit={send} className="form">
        <input
          className="in"
          type="url"
          inputMode="url"
          placeholder="https://…/product-photo.jpg"
          value={url}
          onChange={(e) => setUrl(e.target.value)}
          disabled={pending}
          aria-label="Replacement image URL"
        />
        <input
          className="in"
          type="text"
          placeholder="Note (optional) — what was wrong?"
          value={note}
          onChange={(e) => setNote(e.target.value)}
          disabled={pending}
          aria-label="Note"
        />
        <button className="btn btn-primary" type="submit" disabled={pending || !url.trim()}>
          {pending ? "Sending…" : "Use this image"}
        </button>
      </form>

      {msg ? <p className={msg.ok ? "ok" : "err"}>{msg.text}</p> : null}

      {preview ? (
        <div className="preview">
          <div className="preview-l">Your image — branding is applied on upload</div>
          {/* Raw, unprocessed, straight from the pasted URL: instant confirmation
              that the link resolves to the right picture. */}
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img src={preview} alt="Submitted replacement" onError={() => setPreview("")} />
        </div>
      ) : null}
    </section>
  );
}
