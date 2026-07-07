import { readFile } from "node:fs/promises";
import path from "node:path";
import { NextResponse } from "next/server";
import { type EventSource, parseJsonl, type TelemetryEvent } from "@/lib/telemetry";

// Live read on every request (both sources are append-only logs written as the loop runs).
export const dynamic = "force-dynamic";

// Two streams under <repo>/logs/ (one level up from dashboard/ in local dev):
//   telemetry.jsonl — the §8 envelope from the trading app
//   events.jsonl    — the collab-kit autopilot trace (the autonomous loop's rounds/lanes/closeouts)
// Either or both may be absent; the dashboard merges whatever exists. Overridable for other layouts.
const LOGS_DIR = process.env.TELEMETRY_DIR ?? path.join(process.cwd(), "..", "logs");
const SOURCES: { file: string; source: EventSource }[] = [
  { file: process.env.TELEMETRY_LOG ?? path.join(LOGS_DIR, "telemetry.jsonl"), source: "app" },
  { file: process.env.AUTOPILOT_LOG ?? path.join(LOGS_DIR, "events.jsonl"), source: "autopilot" },
];

async function readSource(file: string, source: EventSource): Promise<TelemetryEvent[]> {
  try {
    return parseJsonl(await readFile(file, "utf-8"), source);
  } catch {
    return []; // missing/unreadable log is empty, never an error
  }
}

export async function GET() {
  const streams = await Promise.all(SOURCES.map((s) => readSource(s.file, s.source)));
  const events = streams.flat().sort((a, b) => (a.ts ?? "").localeCompare(b.ts ?? ""));
  const note = events.length === 0 ? `no telemetry yet under ${LOGS_DIR}` : undefined;
  return NextResponse.json({ count: events.length, events: events.slice(-1000), note });
}
