import { test, expect, openRun, approveEvidence } from "./fixtures";

test("persists the exact cancel command across worker restart and retains resources until explicit teardown", async ({ page, restartScenario, restartWorker, readyFor, evidenceFor, expedite, stopWorker, cancelCommandFor }) => {
  const runId = restartScenario.runId!;
  await openRun(page, runId, restartScenario.bootstrapToken);
  await readyFor(page, "AWAITING_PLAN_APPROVAL", runId);
  await approveEvidence(page, "Approve plan", await evidenceFor("AWAITING_PLAN_APPROVAL", runId));
  await expedite(runId);
  await readyFor(page, "AWAITING_PR_APPROVAL", runId);
  await stopWorker();

  const cancel = page.getByRole("button", { name: "Cancel run", exact: true });
  await cancel.click();
  const dialog = page.getByRole("dialog", { name: "Cancel run" });
  await expect(dialog).toContainText(/resources and evidence remain available until explicit teardown/i);
  await dialog.getByRole("button", { name: "Confirm cancel run" }).click();
  await expect.poll(async () => (await cancelCommandFor(runId)).status).toMatch(/PENDING|LEASED|SUCCEEDED/);

  const restart = await restartWorker();
  expect(restart.newPid).not.toBe(restart.oldPid);
  await expect(page.getByText(/CANCELLED|Cancelled/i)).toBeVisible({ timeout: 120_000 });
  await expect(page.getByText(/Worktree/i)).toBeVisible();

  const teardown = page.getByRole("button", { name: "Remove run resources", exact: true });
  await expect(teardown).toBeVisible();
  await teardown.click();
  const teardownDialog = page.getByRole("dialog", { name: "Remove run resources" });
  await expect(teardownDialog).toBeVisible();
  await expect(teardownDialog).toContainText(/branch/i);
  const identity = await teardownDialog.locator("code").innerText();
  await teardownDialog.getByLabel("Resource identity confirmation").fill(identity);
  await teardownDialog.getByRole("button", { name: "Review resource removal" }).click();
  await teardownDialog.getByRole("button", { name: "Confirm remove resources" }).click();
  await expect(page.getByText(/Already absent|REMOVED/i)).toBeVisible({ timeout: 120_000 });
  await expect(page.getByText(/branch remains/i)).toBeVisible();
});
