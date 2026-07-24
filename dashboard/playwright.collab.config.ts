import { defineConfig, devices } from "@playwright/test";

const baseURL = process.env.COLLAB_DASH_URL;

export default defineConfig({
  testDir: "./e2e",
  testMatch: /collab-.*\.spec\.ts/,
  fullyParallel: false,
  reporter: "list",
  use: {
    baseURL,
    trace: "on-first-retry",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
});
