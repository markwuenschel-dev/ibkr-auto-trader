"use client";

import { useEffect, useState } from "react";
import { Bar, BarChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import type { TelemetryEvent } from "@/lib/telemetry";

interface Feed {
  count: number;
  events: TelemetryEvent[];
  note?: string;
}

export default function Page() {
  const [feed, setFeed] = useState<Feed>({ count: 0, events: [] });

  useEffect(() => {
    const load = () =>
      fetch("/api/telemetry")
        .then((r) => r.json() as Promise<Feed>)
        .then(setFeed)
        .catch(() => {});
    load();
    const id = setInterval(load, 5000); // live-ish: poll the append-only log
    return () => clearInterval(id);
  }, []);

  const { events } = feed;
  const byStage = Object.entries(
    events.reduce<Record<string, number>>((acc, e) => {
      acc[e.stage] = (acc[e.stage] ?? 0) + 1;
      return acc;
    }, {}),
  ).map(([stage, n]) => ({ stage, n }));

  const waived = events.filter((e) => e.decision?.action === "waive").length;
  const rejected = events.filter((e) => e.decision?.action === "reject").length;
  const runs = new Set(events.map((e) => e.run_id)).size;

  return (
    <main className="mx-auto max-w-6xl p-8">
      <header>
        <h1 className="text-2xl font-semibold">IBKR Trader — §8 Telemetry</h1>
        <p className="mt-1 text-sm text-neutral-400">
          Live trace of the paper-trading loop. {feed.note ? `(${feed.note})` : null}
        </p>
      </header>

      <section className="mt-6 grid grid-cols-1 gap-4 sm:grid-cols-4">
        <Kpi label="Events" value={feed.count} />
        <Kpi label="Runs" value={runs} />
        <Kpi label="Waived" value={waived} />
        <Kpi label="Rejected" value={rejected} />
      </section>

      <section className="mt-6 rounded-xl border border-neutral-800 bg-neutral-900 p-4">
        <h2 className="mb-4 text-sm font-medium text-neutral-300">Events by stage</h2>
        <div className="h-72">
          <ResponsiveContainer width="100%" height="100%">
            <BarChart data={byStage}>
              <XAxis dataKey="stage" stroke="#888" fontSize={11} />
              <YAxis stroke="#888" fontSize={11} allowDecimals={false} />
              <Tooltip
                contentStyle={{ background: "#141414", border: "1px solid #262626", borderRadius: 8 }}
              />
              <Bar dataKey="n" fill="#4f9cf9" radius={[4, 4, 0, 0]} />
            </BarChart>
          </ResponsiveContainer>
        </div>
      </section>

      <section className="mt-6 rounded-xl border border-neutral-800 bg-neutral-900">
        <h2 className="border-b border-neutral-800 p-4 text-sm font-medium text-neutral-300">
          Recent events
        </h2>
        <table className="w-full text-left text-sm">
          <thead className="text-neutral-500">
            <tr>
              <th className="p-3 font-medium">ts</th>
              <th className="p-3 font-medium">stage</th>
              <th className="p-3 font-medium">role</th>
              <th className="p-3 font-medium">action</th>
            </tr>
          </thead>
          <tbody>
            {events
              .slice(-25)
              .reverse()
              .map((e) => (
                <tr key={e.event_id} className="border-t border-neutral-800/60">
                  <td className="p-3 tabular-nums text-neutral-400">{e.ts}</td>
                  <td className="p-3">{e.stage}</td>
                  <td className="p-3 text-neutral-400">{e.agent_role ?? "—"}</td>
                  <td className="p-3">{e.decision?.action ?? "—"}</td>
                </tr>
              ))}
          </tbody>
        </table>
      </section>
    </main>
  );
}

function Kpi({ label, value }: { label: string; value: number }) {
  return (
    <div className="rounded-xl border border-neutral-800 bg-neutral-900 p-5">
      <div className="text-sm text-neutral-400">{label}</div>
      <div className="mt-1 text-3xl font-semibold tabular-nums">{value}</div>
    </div>
  );
}
