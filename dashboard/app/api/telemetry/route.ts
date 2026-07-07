import { readFile } from "node:fs/promises";
import path from "node:path";
import { NextResponse } from "next/server";
import { parseJsonl } from "@/lib/telemetry";

// Live read on every request (the source is an append-only log the Python loop writes to).
export const dynamic = "force-dynamic";

// <repo>/logs/telemetry.jsonl, one level up from the dashboard/ working dir in local dev.
// TELEMETRY_LOG overrides it (e.g. an absolute path, or an uploaded artifact path on Vercel).
const LOG_PATH = process.env.TELEMETRY_LOG ?? path.join(process.cwd(), "..", "logs", "telemetry.jsonl");

export async function GET() {
  try {
    const text = await readFile(LOG_PATH, "utf-8");
    const events = parseJsonl(text);
    return NextResponse.json({ count: events.length, events: events.slice(-1000) });
  } catch {
    // No log yet (fresh repo, or serverless without the file mounted) — empty, not an error.
    return NextResponse.json({ count: 0, events: [], note: `no telemetry log at ${LOG_PATH}` });
  }
}
