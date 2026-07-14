"use client";

import { useEffect, useState } from "react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  LabelList,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { TelemetryEvent } from "@/lib/telemetry";

interface Feed {
  count: number;
  events: TelemetryEvent[];
  note?: string;
}

// Decision actions map to reserved status/identity roles — always shown with a text label beside the
// dot, so meaning is never carried by color alone (accessibility).
const ACTION_COLOR: Record<string, string> = {
  accept: "var(--status-good)",
  reject: "var(--status-critical)",
  escalate: "var(--status-warning)",
  revise: "var(--status-serious)",
  waive: "var(--series-1)",
  skip: "var(--text-muted)",
};

const compact = new Intl.NumberFormat("en", { notation: "compact", maximumFractionDigits: 1 });

export default function Page() {
  const [feed, setFeed] = useState<Feed>({ count: 0, events: [] });

  useEffect(() => {
    const load = () =>
      fetch("/api/telemetry")
        .then((r) => r.json() as Promise<Feed>)
        .then(setFeed)
        .catch(() => {}); // hold the previous render on a failed refetch — no skeleton flash
    load();
    const id = setInterval(load, 5000);
    return () => clearInterval(id);
  }, []);

  const { events } = feed;
  const byStage = Object.entries(
    events.reduce<Record<string, number>>((acc, e) => {
      acc[e.stage] = (acc[e.stage] ?? 0) + 1;
      return acc;
    }, {}),
  )
    .map(([stage, n]) => ({ stage, n }))
    .sort((a, b) => b.n - a.n);

  const waived = events.filter((e) => e.decision?.action === "waive").length;
  const rejected = events.filter((e) => e.decision?.action === "reject").length;
  const runs = new Set(events.map((e) => e.run_id)).size;
  const chartHeight = Math.max(160, byStage.length * 40 + 32); // include the x-axis band

  return (
    <main className="mx-auto max-w-6xl p-8">
      <header className="flex items-baseline justify-between">
        <div>
          <h1 className="text-2xl font-semibold text-[var(--text-primary)]">
            IBKR Trader — §8 Telemetry
          </h1>
          <p className="mt-1 text-sm text-[var(--text-muted)]">
            Live trace of the paper-trading loop
          </p>
        </div>
        <span className="text-xs text-[var(--text-muted)] tabular-nums">
          {feed.note ? feed.note : `${compact.format(feed.count)} events · updates every 5s`}
        </span>
      </header>

      {/* Stat tiles: proportional figures, muted labels, status accent reserved for the reject count. */}
      <section className="mt-6 grid grid-cols-2 gap-4 sm:grid-cols-4">
        <Kpi label="Events" value={feed.count} />
        <Kpi label="Runs" value={runs} />
        <Kpi label="Waived" value={waived} />
        <Kpi
          label="Rejected"
          value={rejected}
          accent={rejected > 0 ? "var(--status-critical)" : undefined}
        />
      </section>

      {/* Events by stage — single-series magnitude, one hue. No legend (the title names it). */}
      <section className="mt-6 rounded-xl border border-[var(--border-ring)] bg-[var(--surface-1)] p-4">
        <h2 className="mb-4 text-sm font-medium text-[var(--text-secondary)]">Events by stage</h2>
        {byStage.length === 0 ? (
          <p className="py-10 text-center text-sm text-[var(--text-muted)]">
            No telemetry yet — run the trading loop or `uv run python -m ibkr_trader.app`.
          </p>
        ) : (
          <div style={{ height: chartHeight }}>
            <ResponsiveContainer width="100%" height="100%">
              <BarChart
                layout="vertical"
                data={byStage}
                margin={{ top: 4, right: 48, bottom: 4, left: 8 }}
                barCategoryGap="32%"
              >
                <CartesianGrid horizontal={false} stroke="var(--grid)" strokeWidth={1} />
                <XAxis
                  type="number"
                  allowDecimals={false}
                  tickLine={false}
                  axisLine={{ stroke: "var(--baseline)" }}
                  tick={{ fill: "var(--text-muted)", fontSize: 11 }}
                />
                <YAxis
                  type="category"
                  dataKey="stage"
                  width={160}
                  tickLine={false}
                  axisLine={false}
                  tick={{ fill: "var(--text-secondary)", fontSize: 12 }}
                />
                <Tooltip
                  cursor={{ fill: "rgba(128,128,128,0.12)" }}
                  contentStyle={{
                    background: "var(--surface-1)",
                    border: "1px solid var(--border-ring)",
                    borderRadius: 8,
                    color: "var(--text-primary)",
                    fontSize: 12,
                  }}
                  labelStyle={{ color: "var(--text-secondary)" }}
                />
                <Bar
                  dataKey="n"
                  fill="var(--series-1)"
                  barSize={18}
                  radius={[0, 4, 4, 0]}
                  isAnimationActive={false}
                >
                  <LabelList
                    dataKey="n"
                    position="right"
                    fill="var(--text-secondary)"
                    fontSize={11}
                  />
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          </div>
        )}
      </section>

      {/* Recent events — the detail view AND the accessible table twin of the chart above. */}
      <section className="mt-6 overflow-x-auto rounded-xl border border-[var(--border-ring)] bg-[var(--surface-1)]">
        <h2 className="border-b border-[var(--border-ring)] p-4 text-sm font-medium text-[var(--text-secondary)]">
          Recent events
        </h2>
        <table className="w-full text-left text-sm tabular-nums">
          <thead className="text-[var(--text-muted)]">
            <tr>
              <th className="p-3 font-medium">ts</th>
              <th className="p-3 font-medium">stage</th>
              <th className="p-3 font-medium">role</th>
              <th className="p-3 font-medium">action</th>
            </tr>
          </thead>
          <tbody className="text-[var(--text-primary)]">
            {events
              .slice(-25)
              .reverse()
              .map((e) => (
                <tr key={e.event_id} className="border-t border-[var(--border-ring)]">
                  <td className="p-3 text-[var(--text-muted)]">{e.ts}</td>
                  <td className="p-3">{e.stage}</td>
                  <td className="p-3 text-[var(--text-secondary)]">{e.agent_role ?? "—"}</td>
                  <td className="p-3">
                    <Action action={e.decision?.action} />
                  </td>
                </tr>
              ))}
          </tbody>
        </table>
      </section>
    </main>
  );
}

function Kpi({ label, value, accent }: { label: string; value: number; accent?: string }) {
  return (
    <div className="rounded-xl border border-[var(--border-ring)] bg-[var(--surface-1)] p-5">
      <div className="flex items-center gap-2 text-sm text-[var(--text-muted)]">
        {accent ? <span className="size-2 rounded-full" style={{ background: accent }} /> : null}
        {label}
      </div>
      {/* Proportional figures (no tabular-nums) for a large standalone value. */}
      <div className="mt-1 text-3xl font-semibold text-[var(--text-primary)]">
        {compact.format(value)}
      </div>
    </div>
  );
}

function Action({ action }: { action?: string | null }) {
  if (!action) return <span className="text-[var(--text-muted)]">—</span>;
  return (
    <span className="inline-flex items-center gap-2">
      <span
        className="size-2 rounded-full"
        style={{ background: ACTION_COLOR[action] ?? "var(--text-muted)" }}
      />
      {action}
    </span>
  );
}
