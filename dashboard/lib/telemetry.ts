import { z } from "zod";

// The dashboard shows two event streams, normalized to ONE shape:
//   • the §8 telemetry envelope written by the trading app (src/ibkr_trader/telemetry.py)
//   • the collab-kit autopilot trace (tools/lib/trace.py): the autonomous loop's rounds, lanes,
//     signoffs and closeouts — this is what makes the unattended run watchable live.
// The two formats differ (the trace uses `role` not `agent_role`, has no trace_id/gates/event_id), so
// parsing is lenient: only `stage` is required; everything else is optional and normalized below.

const RawDecision = z
  .object({
    action: z.string().optional(),
    reason_codes: z.array(z.string()).default([]),
    confidence: z.number().nullable().optional(),
  })
  .nullable()
  .optional();

const RawEvent = z.object({
  schema_version: z.string().optional(),
  ts: z.string().optional(),
  run_id: z.string().optional(),
  span_id: z.string().optional(),
  parent_span_id: z.string().nullable().optional(),
  stage: z.string(),
  agent_role: z.string().nullable().optional(),
  role: z.string().nullable().optional(), // collab-kit trace uses `role`
  artifact: z.string().nullable().optional(),
  decision: RawDecision,
  metrics: z.record(z.string(), z.unknown()).optional(),
  failure: z.unknown().optional(),
  event_id: z.string().optional(),
});

export type EventSource = "app" | "autopilot";

export interface TelemetryEvent {
  source: EventSource;
  ts: string | null;
  run_id: string;
  stage: string;
  agent_role: string | null;
  decision: { action: string | null; reason_codes: string[] } | null;
  artifact: string | null;
  event_id: string;
}

/** Parse an append-only JSONL log (either format), normalizing to the dashboard event shape. */
export function parseJsonl(text: string, source: EventSource): TelemetryEvent[] {
  const out: TelemetryEvent[] = [];
  for (const raw of text.split("\n")) {
    const line = raw.trim();
    if (!line) continue;
    let obj: unknown;
    try {
      obj = JSON.parse(line);
    } catch {
      continue; // a torn append is observability noise, never fatal
    }
    const parsed = RawEvent.safeParse(obj);
    if (!parsed.success) continue;
    const e = parsed.data;
    out.push({
      source,
      ts: e.ts ?? null,
      run_id: e.run_id ?? "—",
      stage: e.stage,
      agent_role: e.agent_role ?? e.role ?? null,
      decision: e.decision
        ? { action: e.decision.action ?? null, reason_codes: e.decision.reason_codes }
        : null,
      artifact: e.artifact ?? null,
      event_id: e.event_id ?? `${source}:${e.span_id ?? ""}:${e.ts ?? ""}:${e.stage}`,
    });
  }
  return out;
}
