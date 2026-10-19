import { randomUUID } from "node:crypto";
import { spawn } from "node:child_process";
import { mkdirSync, openSync } from "node:fs";
import path from "node:path";

import { NextRequest, NextResponse } from "next/server";

import { resolveSelection, type Selection } from "@/lib/selection";

export const dynamic = "force-dynamic";

// The repo root, one level up from app/. run_source_job.py lives there.
const REPO_ROOT = path.resolve(process.cwd(), "..");
const PYTHON = path.join(REPO_ROOT, ".venv", "bin", "python");
const SCRIPT = path.join(REPO_ROOT, "run_source_job.py");
const LOG_DIR = path.join(REPO_ROOT, "work", "source_runs");
// Every stub row imported by import_catalogue.py came from this file, and
// it's the superset of every other catalogue export we have -- so it's the
// right default for an app-triggered run regardless of which batch a
// selected product happens to already belong to.
const CATALOGUE_FILE = path.join(REPO_ROOT, "data", "all products.xlsx");

/**
 * Kicks off a sourcing run for the given item ids as a detached subprocess and
 * returns immediately with a run_id to poll via /api/source/status/[id].
 *
 * Runs on this same machine, same as worker.py and push_to_web.py -- the
 * pipeline needs Serper/Gemini credentials and writes to gitignored local
 * directories (library/, work/), neither of which exist on Vercel. This only
 * works because the app itself is also running locally right now (see
 * README's explanation of why worker.py can't run in the cloud -- the same
 * reasoning applies here).
 */
export async function POST(req: NextRequest) {
  const selection = (await req.json().catch(() => null)) as Selection | null;
  if (!selection) {
    return NextResponse.json({ error: "a selection is required" }, { status: 400 });
  }
  // resolveSelection enforces MAX_SELECTION against the actual matched count,
  // guarding against an accidental "select all 5,659" costing real API spend
  // regardless of whether the selection came in as explicit ids or a filter.
  const resolved = await resolveSelection(selection);
  if ("error" in resolved) {
    return NextResponse.json({ error: resolved.error }, { status: 400 });
  }
  const ids = resolved.ids;

  const runId = "src-" + randomUUID().slice(0, 8);
  mkdirSync(LOG_DIR, { recursive: true });
  const logFd = openSync(path.join(LOG_DIR, `${runId}.log`), "a");

  const child = spawn(
    PYTHON,
    [SCRIPT, runId, ids.join(","), "--catalogue-file", CATALOGUE_FILE],
    { cwd: REPO_ROOT, detached: true, stdio: ["ignore", logFd, logFd] },
  );
  child.unref();

  return NextResponse.json({ run_id: runId, count: ids.length });
}
