import AxeBuilder from "@axe-core/playwright";
import { test, expect, openRun, approveEvidence } from "./fixtures";

test.describe("keyboard and accessibility contract", () => {
  test("covers runs, project form, cockpit tabs, and all approval dialogs", async ({ page, restartScenario, readyFor, expedite }) => {
    const runId = restartScenario.runId!;
    await page.emulateMedia({ reducedMotion: "reduce" });
    await openRun(page, runId, restartScenario.bootstrapToken);
    for (const url of ["/runs", "/projects/new", `/runs/${encodeURIComponent(runId)}`]) {
      await page.goto(url);
      const result = await new AxeBuilder({ page }).analyze();
      expect(result.violations, `${url} has accessibility violations`).toEqual([]);
    }

    await openRun(page, runId, restartScenario.bootstrapToken);
    await expect(page.getByRole("navigation", { name: "Run sections" })).toBeVisible();
    for (const tab of ["Overview", "Plan", "Checks", "Review", "Activity", "Usage", "Security", "Changes"]) {
      const control = page.getByRole("button", { name: tab, exact: true });
      await control.click();
      await expect(control).toHaveAttribute("aria-pressed", "true");
      expect((await new AxeBuilder({ page }).analyze()).violations, `${tab} accessibility`).toEqual([]);
    }
    for (const [state, name] of [
      ["AWAITING_PLAN_APPROVAL", "Approve plan"],
      ["AWAITING_PR_APPROVAL", "Approve PR publication"],
      ["AWAITING_MERGE_APPROVAL", "Approve merge"],
    ] as const) {
      await readyFor(page, state, runId);
      const trigger = page.getByRole("button", { name, exact: true });
      // Reach the action through actual keyboard navigation, not programmatic focus.
      for (let attempt = 0; attempt < 80; attempt++) {
        if (await trigger.evaluate(element => element === document.activeElement)) break;
        await page.keyboard.press("Tab");
      }
      await expect(trigger).toBeFocused();
      await trigger.press("Enter");
      const dialog = page.getByRole("dialog", { name });
      await expect(dialog).toBeVisible();
      await expect.poll(() => dialog.evaluate(element => element.contains(document.activeElement))).toBe(true);
      const dialogA11y = await new AxeBuilder({ page }).include("dialog").analyze();
      expect(dialogA11y.violations).toEqual([]);
      const back = dialog.getByRole("button", { name: "Back" });
      for (let attempt = 0; attempt < 20; attempt++) {
        if (await back.evaluate(element => element === document.activeElement)) break;
        await page.keyboard.press("Tab");
      }
      await expect(back).toBeFocused();
      await page.keyboard.press("Enter");
      await expect(trigger).toBeFocused();
      const digest = await (await fetch(`${process.env.FORGE_E2E_CONTROL_ORIGIN}/runs/state?runId=${runId}&state=${state}`)).json() as { evidenceDigest: string };
      await approveEvidence(page, name, digest.evidenceDigest);
      await expedite(runId);
    }

    await expect(page.getByRole("status")).toBeVisible();
    await expect.poll(() => page.evaluate(() => matchMedia("(prefers-reduced-motion: reduce)").matches)).toBe(true);
    const motion = await page.locator("* ").evaluateAll(elements => elements.every(element => {
      const style = getComputedStyle(element);
      return style.animationName === "none" || style.animationDuration === "0s" || style.animationPlayState === "paused";
    }));
    expect(motion).toBe(true);
  });
});
