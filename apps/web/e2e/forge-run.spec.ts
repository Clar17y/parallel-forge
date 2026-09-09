import { test, expect, approveEvidence, openRun } from "./fixtures";

test.describe("real Forge run approvals", () => {
  test("creates a project/task and approves the exact plan, PR, and merge evidence", async ({ page, scenario, readyFor, evidenceFor, expedite, registerRun }) => {
    await page.goto(`/projects/new#bootstrap=${encodeURIComponent(scenario.bootstrapToken ?? "")}`);
    await page.getByLabel("Project name").fill(`Browser fixture ${Date.now()}`);
    await page.getByLabel("Repository path").fill(scenario.uiRepositoryPath!);
    await page.getByLabel("GitHub repository").fill(scenario.uiGithubRepository!);
    await page.getByLabel(/I trust this project/).check();
    await page.getByLabel("Runner").selectOption("trusted_host");
    await page.getByRole("button", { name: "Add command", exact: true }).click();
    await page.getByLabel("Command name").fill("unit");
    await page.getByLabel("Executable").fill("python3");
    await page.getByRole("button", { name: "Add argument", exact: true }).click();
    await page.getByLabel("Argument 1").fill("check_readme.py");
    await page.getByRole("button", { name: "Register project" }).click();
    await expect(page).toHaveURL(/\/projects\//);
    await page.getByRole("link", { name: "New run" }).click();
    await page.getByLabel("Task title").fill("Create browser acceptance task");
    await page.getByLabel("Task description").fill("Exercise the real browser lifecycle");
    await page.getByRole("button", { name: "Create run" }).click();
    await expect(page).toHaveURL(/\/runs\//);
    const runId = new URL(page.url()).pathname.split("/").pop()!;
    await registerRun(runId);

    for (const [state, button] of [
      ["AWAITING_PLAN_APPROVAL", "Approve plan"],
      ["AWAITING_PR_APPROVAL", "Approve PR publication"],
      ["AWAITING_MERGE_APPROVAL", "Approve merge"],
    ] as const) {
      await readyFor(page, state, runId);
      const digest = await evidenceFor(state, runId);
      await approveEvidence(page, button, digest);
      await expedite(runId);
    }

    await expect(page.getByText(/COMPLETED|Completed/i)).toBeVisible({ timeout: 120_000 });
    await openRun(page, runId);
    await expect(page.getByText(/Branch/i)).toBeVisible();
  });
});
