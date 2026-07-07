# IBKR Trader — Observability Dashboard

The §8 telemetry dashboard for the paper-trading loop. Reads the append-only
`../logs/telemetry.jsonl` the Python side writes and renders run stats + a live event view.

## Stack (latest at scaffold time, 2026-07-07)

| Tool | Version | Note |
|---|---|---|
| Next.js | 16.2.10 | App Router, live API route (Vercel target) |
| React / react-dom | 19.2.7 | |
| TypeScript | 6.0.3 | |
| Tailwind CSS | 4.3.2 | v4 CSS-first config (`app/globals.css` + `@tailwindcss/postcss`) |
| Recharts | 3.9.2 | charts |
| zod | 4.4.3 | validates the §8 envelope (`lib/telemetry.ts`) |
| oxlint | 1.73.0 | `pnpm lint` |
| oxfmt | 0.58.0 | `pnpm format` — **pre-1.0 / experimental** |

## Run

Requires Node ≥ 20.9 and pnpm (both on your machine; this repo's sandbox couldn't run them).

```bash
cd dashboard
pnpm install
pnpm dev          # http://localhost:3000
pnpm lint         # oxlint
pnpm format       # oxfmt
pnpm build        # production build (also the Vercel build)
```

To point at a specific log: `TELEMETRY_LOG=/abs/path/telemetry.jsonl pnpm dev`.

## Decisions & caveats

- **Charts = Recharts, not the Tremor npm package.** `@tremor/react` (3.18) still requires Tailwind
  **v3**, which conflicts with latest Tailwind **v4** — and "latest versions" was the priority. Recharts
  is what Tremor is built on; the KPI tiles here are Tremor-style Tailwind components. To use the Tremor
  component library instead, pin `tailwindcss@^3.4` and add `@tremor/react`.
- **oxfmt is experimental (pre-1.0)** — expect formatting gaps. `oxlint` is production-ready. Swap oxfmt
  for Prettier if it bites.
- **Deploy = Vercel.** The API route is `dynamic` (server-rendered). On Vercel the serverless runtime
  won't have the local `logs/` file — wire `TELEMETRY_LOG` to a real source (uploaded artifact, blob, or
  a small DB) when you deploy. Local dev reads the repo's `logs/telemetry.jsonl` directly.
- Not yet added: **Vitest + Playwright** (agreed testing stack) — next pass.
