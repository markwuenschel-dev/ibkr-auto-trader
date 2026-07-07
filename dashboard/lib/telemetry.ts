import { z } from "zod";

// Mirror of the Python §8 telemetry envelope (src/ibkr_trader/telemetry.py). Kept in lockstep: if the
// emitter's SCHEMA_VERSION changes, update this schema. Unknown/extra fields are stripped (object is
// non-strict by default), so the dashboard is forward-compatible with new emitter fields it ignores.

const Decision = z.object({
  action: z.string(),
  reason_codes: z.array(z.string()).default([]),
  confidence: z.number().nullable().optional(),
});

const Gate = z.object({
  name: z.string(),
  status: z.string(),
  severity: z.string().nullable().optional(),
});

export const TelemetryEvent = z.object({
  schema_version: z.string(),
  trace_id: z.string(),
  span_id: z.string(),
  parent_span_id: z.string().nullable(),
  run_id: z.string(),
  task_id: z.string().nullable(),
  agent_role: z.string().nullable(),
  stage: z.string(),
  ts: z.string(),
  decision: Decision.nullable(),
  metrics: z.record(z.string(), z.unknown()).default({}),
  gates: z.array(Gate).default([]),
  risk: z.unknown(), // present-but-null in the fail-closed bootstrap; calibrated later (§6/§11)
  failure: z.unknown(),
  event_id: z.string(),
});

export type TelemetryEvent = z.infer<typeof TelemetryEvent>;

/** Parse an append-only JSONL telemetry log, keeping only well-formed §8 events. */
export function parseJsonl(text: string): TelemetryEvent[] {
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
    const parsed = TelemetryEvent.safeParse(obj);
    if (parsed.success) out.push(parsed.data);
  }
  return out;
}
