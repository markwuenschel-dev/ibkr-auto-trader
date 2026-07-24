import { expect, test } from "@playwright/test";

const dashboardUrl = process.env.COLLAB_DASH_URL;

test.describe("collab four-model human observability", () => {
  test.skip(!dashboardUrl, "set COLLAB_DASH_URL for the Python collab dashboard");

  test("answers the operator questions with keyboard-only coordinated views", async ({ page }) => {
    const started = performance.now();
    await page.goto(dashboardUrl!);
    await expect(page.locator("#transportState")).toHaveText("CONNECTED", { timeout: 10_000 });
    const initialLoadMs = performance.now() - started;
    expect(initialLoadMs).toBeLessThan(3_000);

    await expect(page.getByRole("main")).toBeVisible();
    const operatorTab = page.getByRole("tab", { name: "Operator timeline" });
    await expect(operatorTab).toHaveAttribute("aria-selected", "true");
    await expect(page.locator("#heroEyebrow")).toContainText("idle · 1 stuck");
    await expect(page.locator("#heroTitle")).toContainText("001 is stuck in claimed — NOT shipped");
    await expect(page.locator("#heroMetrics")).toContainText("2 / 3");
    await expect(page.locator("#operatorTimeline")).toContainText(
      "accepted result met completion criteria",
    );

    await operatorTab.focus();
    await page.keyboard.press("ArrowRight");
    const modelsTab = page.getByRole("tab", { name: "Model activity" });
    await expect(modelsTab).toBeFocused();
    await expect(modelsTab).toHaveAttribute("aria-selected", "true");
    await expect(page.locator("#view-models")).toBeVisible();
    await expect(page.locator("#view-operator")).toBeHidden();
    const modelActivity = page.locator("#modelActivity");
    for (const model of ["gpt-5.6-luna", "grok-4.5", "gemini-3.5-flash", "haiku-4.5"]) {
      await expect(modelActivity).toContainText(model);
    }
    await expect(modelActivity).toContainText("telemetry reconciled");
    await expect(modelActivity).toContainText("responses");
    await expect(modelActivity).toContainText("response complete");

    await page.keyboard.press("ArrowRight");
    const qualityTab = page.getByRole("tab", { name: "Validation & quality" });
    await expect(qualityTab).toBeFocused();
    await expect(page.locator("#view-quality")).toBeVisible();
    await expect(page.locator("#view-models")).toBeHidden();
    const quality = page.locator("#qualityWorkspace");
    await expect(quality).toContainText("candidate-rejected");
    await expect(quality).toContainText("candidate-accepted");
    await expect(quality).toContainText("failed · automated_check");
    await expect(quality).toContainText("passed · automated_check");
    await expect(quality).toContainText("keyboard-navigation · missing · critical");
    await expect(quality).toContainText("keyboard-navigation · met · critical");
    await expect(quality).toContainText("Evaluator disagreement recorded");
    await expect(quality).toContainText("named browser oracle");

    await page.keyboard.press("End");
    const diagnosticsTab = page.getByRole("tab", { name: "Diagnostics" });
    await expect(diagnosticsTab).toBeFocused();
    await expect(page.locator("#view-diagnostics")).toBeVisible();
    await expect(page.locator("#view-quality")).toBeHidden();
    await expect(page.locator("#healthgrid")).toContainText("attempt persistence · HEALTHY");
    await expect(page.locator("#healthgrid")).toContainText("langfuse · HEALTHY");
    await expect(page.locator("#runs .runrow")).toHaveCount(1);
    await page.locator("#runs .runrow").last().click();
    const replay = page.locator("#rv-body");
    await expect(replay).toContainText("Human run summary");
    await expect(replay).toContainText("Prove human-readable four-model run observability");
    await expect(replay).toContainText("accepted_result_met_completion_criteria");
    await expect(replay).toContainText("candidate-rejected");
    await expect(replay).toContainText("Keyboard persistence failed");
    await expect(replay).toContainText("candidate-accepted");
    await expect(replay).toContainText("accepted-browser-check");
    await page.locator("#rviewer").getByLabel("Close").click();

    await page.reload();
    await expect(page.locator("#transportState")).toHaveText("CONNECTED", { timeout: 10_000 });
    await expect(diagnosticsTab).toHaveAttribute("aria-selected", "true");
    await diagnosticsTab.focus();
    await page.keyboard.press("Home");
    await expect(operatorTab).toBeFocused();
    await expect(operatorTab).toHaveAttribute("aria-selected", "true");
    await expect(page.locator("#view-operator")).toBeVisible();
    await expect(page.locator("#view-diagnostics")).toBeHidden();

    const namelessControls = await page.locator("button, input, select").evaluateAll((controls) =>
      controls
        .filter((control) => {
          const labelledBy = control.getAttribute("aria-labelledby");
          const labelled = labelledBy && document.getElementById(labelledBy)?.textContent?.trim();
          const label = control.closest("label")?.textContent?.trim();
          return !(
            control.getAttribute("aria-label")?.trim() ||
            labelled ||
            label ||
            control.textContent?.trim() ||
            control.getAttribute("title")?.trim()
          );
        })
        .map((control) => control.outerHTML),
    );
    expect(namelessControls).toEqual([]);

    if (process.env.COLLAB_SCREENSHOT_PATH) {
      await page.waitForTimeout(200);
      await page.screenshot({ path: process.env.COLLAB_SCREENSHOT_PATH, fullPage: false });
    }
  });

  test("recovers an SSE interruption without duplicating the rendered attempt roster", async ({
    page,
  }) => {
    await page.goto(dashboardUrl!);
    await expect(page.locator("#transportState")).toHaveText("CONNECTED", { timeout: 10_000 });
    await page.getByRole("tab", { name: "Model activity" }).click();
    await expect(page.locator("#modelActivity .model-attempt")).toHaveCount(4);

    const interrupted = performance.now();
    await page.evaluate("stream.onerror()");
    await expect(page.locator("#transportState")).toHaveText(/STALE|RECONNECTING|DISCONNECTED/, {
      timeout: 10_000,
    });
    await expect(page.locator("#transportState")).toHaveText("CONNECTED", { timeout: 35_000 });
    const reconnectMs = performance.now() - interrupted;
    expect(reconnectMs).toBeLessThan(35_000);
    await expect(page.locator("#modelActivity .model-attempt")).toHaveCount(4);
  });
});
