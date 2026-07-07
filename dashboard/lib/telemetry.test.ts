import { describe, expect, it } from "vitest";
import { parseJsonl } from "@/lib/telemetry";

// §8 envelope (trading app)
const appEvent = {
  schema_version: "0.1",
  span_id: "sp-1",
  parent_span_id: null,
  run_id: "run-1",
  agent_role: "orchestrator",
  stage: "app.bootstrap",
  ts: "2026-07-07T00:00:00Z",
  decision: { action: "accept", reason_codes: ["mode:PAPER"], confidence: null },
  event_id: "e1",
};

// collab-kit autopilot trace (uses `role` + `artifact`, no trace_id/gates/event_id)
const autopilotEvent = {
  schema_version: "0.1",
  ts: "2026-07-07T00:01:00Z",
  run_id: "ibkr-auto-trader",
  span_id: "r1:builder",
  stage: "autopilot.round",
  role: "builder",
  artifact: "handoff:027",
  decision: { action: "reply", reason_codes: ["to:reviewer"] },
};

describe("parseJsonl", () => {
  it("normalizes an §8 app event and drops blank/invalid lines", () => {
    const text = `${JSON.stringify(appEvent)}\n\nnot json\n{"no":"stage"}\n`;
    const events = parseJsonl(text, "app");
    expect(events).toHaveLength(1);
    expect(events[0].stage).toBe("app.bootstrap");
    expect(events[0].decision?.action).toBe("accept");
    expect(events[0].source).toBe("app");
  });

  it("normalizes a collab-kit autopilot trace event (role -> agent_role)", () => {
    const [ev] = parseJsonl(JSON.stringify(autopilotEvent), "autopilot");
    expect(ev.stage).toBe("autopilot.round");
    expect(ev.agent_role).toBe("builder"); // mapped from `role`
    expect(ev.decision?.action).toBe("reply");
    expect(ev.source).toBe("autopilot");
    expect(ev.event_id).toContain("autopilot:"); // synthesized (no event_id in the trace)
  });

  it("returns [] for empty input", () => {
    expect(parseJsonl("", "app")).toEqual([]);
  });
});
