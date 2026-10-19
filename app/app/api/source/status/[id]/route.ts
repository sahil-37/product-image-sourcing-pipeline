import { existsSync, readFileSync } from "node:fs";
import path from "node:path";

import { NextRequest, NextResponse } from "next/server";

export const dynamic = "force-dynamic";

const REPO_ROOT = path.resolve(process.cwd(), "..");
const LOG_DIR = path.join(REPO_ROOT, "work", "source_runs");

/** Polled by the browse page while a sourcing run is in flight. */
export async function GET(_req: NextRequest, { params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  // run_source_job.py only ever writes <run_id>.json where run_id came from
  // this same route's randomUUID -- reject anything else rather than resolve
  // an arbitrary path from client input.
  if (!/^[a-zA-Z0-9_-]+$/.test(id)) {
    return NextResponse.json({ error: "bad run id" }, { status: 400 });
  }

  const statusPath = path.join(LOG_DIR, `${id}.json`);
  if (!existsSync(statusPath)) {
    return NextResponse.json({ status: "pending" });
  }

  try {
    const data = JSON.parse(readFileSync(statusPath, "utf8"));
    return NextResponse.json(data);
  } catch {
    return NextResponse.json({ status: "pending" });
  }
}
