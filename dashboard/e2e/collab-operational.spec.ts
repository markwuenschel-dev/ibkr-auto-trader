import { expect, test } from "@playwright/test";
import { mkdirSync, rmSync, writeFileSync } from "node:fs";
import { join } from "node:path";

const dashboardUrl = process.env.COLLAB_DASH_URL;
const fixture = process.env.COLLAB_FIXTURE_DIR;

test.describe("collab operational mission control", () => {
  test.skip(
    !dashboardUrl || !fixture,
    "set COLLAB_DASH_URL and COLLAB_FIXTURE_DIR for the Python dashboard",
  );

  test("streams escalation, detail history, health, reload, and reconnect without color-only status", async ({
    page,
  }) => {
    const escalationDir = join(fixture!, "autopilot", "escalations");
    rmSync(join(escalationDir, "001.md"), { force: true });
    await page.goto(dashboardUrl!);
    await expect(page.locator("#transportState")).toHaveText("CONNECTED", { timeout: 10_000 });
    await expect(page.locator("#opcounts")).toContainText("queued");
    await expect(page.locator("#healthgrid")).toContainText("source reads");
    await expect(page.locator("#healthgrid")).toContainText("langfuse");
    await expect(page.locator("#pipe")).toContainText("queued");

    mkdirSync(escalationDir, { recursive: true });
    const metadata = {
      schema_version: "1.0",
      reason: "verification_incomplete",
      severity: "warning",
      run_uid: "browser-run",
      attempts: 2,
      required_action: "retry_or_adopt",
    };
    writeFileSync(
      join(escalationDir, "001.md"),
      `<!-- escalation:001 -->\n<!-- escalation-meta:${JSON.stringify(metadata)} -->\n# stopped\n<!-- /escalation:001 -->\n`,
      "utf8",
    );

    await expect(page.locator("#opcounts .opchip", { hasText: "escalated" })).toContainText(
      "1 item",
      {
        timeout: 5_000,
      },
    );
    await expect(page.locator("#pipe")).toContainText("verification_incomplete");
    await expect(page.locator("#pipe")).toContainText("action: retry_or_adopt");
    await page.locator("#pipe .hchip").first().click();
    await expect(page.locator("#v-body")).toContainText("ESCALATION");
    await expect(page.locator("#v-body")).toContainText("LIFECYCLE HISTORY");
    await expect(page.locator("#v-body")).toContainText("SOURCE EVIDENCE (redacted)");
    if (process.env.COLLAB_SCREENSHOT_PATH) {
      await page.screenshot({ path: process.env.COLLAB_SCREENSHOT_PATH, fullPage: true });
    }
    await page.locator("#viewer").getByLabel("Close").click();

    rmSync(join(escalationDir, "001.md"));
    await expect(page.locator("#opcounts .opchip", { hasText: "queued" })).toContainText("1 item", {
      timeout: 5_000,
    });
    await page.reload();
    await expect(page.locator("#transportState")).toHaveText("CONNECTED", { timeout: 10_000 });
    await expect(page.locator("#pipe")).toContainText("queued");

    await page.evaluate("stream.onerror()");
    await expect(page.locator("#transportState")).toHaveText(/STALE|RECONNECTING|DISCONNECTED/, {
      timeout: 10_000,
    });
    await expect(page.locator("#transportState")).toHaveText("CONNECTED", { timeout: 35_000 });
  });
});
