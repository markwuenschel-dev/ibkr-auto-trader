import { expect, test } from "@playwright/test";

test("dashboard renders the §8 telemetry header and KPI tiles", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: /§8 Telemetry/ })).toBeVisible();
  await expect(page.getByText("Events", { exact: true })).toBeVisible();
  await expect(page.getByText("Runs", { exact: true })).toBeVisible();
});
