import { defineConfig, devices } from "@playwright/test";

// e2e smoke against a locally-served dev build. `webServer` boots `pnpm dev` automatically and reuses
// an already-running one. Run browsers once with `pnpm e2e:install` before the first `pnpm e2e`.
export default defineConfig({
  testDir: "./e2e",
  fullyParallel: true,
  reporter: "list",
  use: {
    baseURL: "http://localhost:3007",
    trace: "on-first-retry",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: {
    command: "pnpm dev",
    url: "http://localhost:3007",
    reuseExistingServer: true,
    timeout: 120_000,
  },
});
