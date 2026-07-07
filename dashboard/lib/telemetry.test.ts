import { describe, expect, it } from "vitest";
import { parseJsonl } from "@/lib/telemetry";

const validEvent = {
  schema_version: "0.1",
  trace_id: "tr-1",
  span_id: "sp-1",
  parent_span_id: null,
  run_id: "run-1",
  task_id: "PT-0",
  agent_role: "orchestrator",
  stage: "app.bootstrap",
  ts: "2026-07-07T00:00:00Z",
  decision: { action: "accept", reason_codes: ["mode:PAPER"], confidence: null },
  metrics: {},
  gates: [],
  risk: null,
  failure: null,
  event_id: "e1",
};

describe("parseJsonl", () => {
  it("keeps well-formed §8 events and drops blank/invalid lines", () => {
    const text = `${JSON.stringify(validEvent)}\n\nnot json\n{"partial":true}\n`;
    const events = parseJsonl(text);
    expect(events).toHaveLength(1);
    expect(events[0].stage).toBe("app.bootstrap");
    expect(events[0].decision?.action).toBe("accept");
  });

  it("returns [] for empty input", () => {
    expect(parseJsonl("")).toEqual([]);
  });

  it("tolerates a present-but-null risk block (fail-closed bootstrap)", () => {
    const [ev] = parseJsonl(JSON.stringify(validEvent));
    expect(ev.risk).toBeNull();
  });
});
