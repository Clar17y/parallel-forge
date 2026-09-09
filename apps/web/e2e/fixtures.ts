/* eslint-disable react-hooks/rules-of-hooks -- Playwright fixture callbacks use the same `use` name. */
import { test as base, expect, type Page } from "@playwright/test";

/**
 * The bridge is deliberately outside the browser process. It creates temporary
 * repositories and talks to the real API/worker/PostgreSQL processes. Browser
 * tests only receive opaque ids and loopback URLs; credentials never enter a
 * trace, screenshot, or test assertion.
 */
export type Scenario = {
  projectId?: string;
  runId?: string;
  controlOrigin: string;
  bootstrapToken?: string;
  uiRepositoryPath?: string;
  uiGithubRepository?: string;
  gates?: Partial<Record<"plan" | "pr" | "merge", { state: string; evidenceDigest: string }>>;
};

async function control<T>(path: string, init?: RequestInit): Promise<T> {
  const origin = process.env.FORGE_E2E_CONTROL_ORIGIN;
  if (!origin) throw new Error("FORGE_E2E_CONTROL_ORIGIN is required for hosted browser acceptance");
  const response = await fetch(new URL(path, origin), { ...init, headers: { "content-type": "application/json", ...(init?.headers ?? {}) } });
  if (!response.ok) throw new Error(`acceptance control bridge returned ${response.status} for ${path}`);
  return response.json() as Promise<T>;
}

export type BrowserFixtures = {
  scenario: Scenario;
  restartScenario: Scenario;
  readyFor: (page: Page, state: string, runId: string) => Promise<void>;
  evidenceFor: (state: string, runId: string) => Promise<string>;
  restartWorker: () => Promise<{ oldPid: number; newPid: number }>;
  expedite: (runId: string) => Promise<void>;
  registerRun: (runId: string) => Promise<void>;
  stopWorker: () => Promise<void>;
  cancelCommandFor: (runId: string) => Promise<{ status: string }>;
};

export const test = base.extend<BrowserFixtures>({
  scenario: async ({}, use) => use(await control<Scenario>("/scenario/forge", { method: "POST", body: "{}" })),
  restartScenario: async ({}, use) => use(await control<Scenario>("/scenario/restart-cancel", { method: "POST", body: "{}" })),
  readyFor: async ({}, use) => use(async (page, state, runId) => {
    await expect.poll(async () => (await control<{ state: string }>(`/runs/state?runId=${encodeURIComponent(runId)}&state=${encodeURIComponent(state)}`)).state, { timeout: 120_000 }).toBe(state);
    // The dashboard must learn this transition through its live subscription.
    await expect(page.locator("header").filter({ hasText: state.replaceAll("_", " ") })).toBeVisible();
  }),
  evidenceFor: async ({}, use) => use(async (state, runId) => (await control<{ evidenceDigest: string }>(`/runs/state?runId=${encodeURIComponent(runId)}&state=${encodeURIComponent(state)}`)).evidenceDigest),
  restartWorker: async ({}, use) => use(() => control<{ oldPid: number; newPid: number }>("/worker/restart", { method: "POST", body: "{}" })),
  expedite: async ({}, use) => use(runId => control<void>(`/runs/${runId}/expedite`, { method: "POST", body: "{}" })),
  registerRun: async ({}, use) => use(runId => control<void>(`/runs/${runId}/register`, { method: "POST", body: "{}" })),
  stopWorker: async ({}, use) => use(() => control<void>("/worker/stop", { method: "POST", body: "{}" })),
  cancelCommandFor: async ({}, use) => use(runId => control<{ status: string }>(`/runs/${runId}/cancel-command`)),
});

export { expect };

export async function openRun(page: Page, runId: string, bootstrapToken?: string): Promise<void> {
  const token = bootstrapToken ?? process.env.FORGE_E2E_BOOTSTRAP_TOKEN;
  const hash = token ? `#bootstrap=${encodeURIComponent(token)}` : "";
  await page.goto(`/runs/${encodeURIComponent(runId)}${hash}`);
  await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
}

export async function approveEvidence(page: Page, buttonName: string, digest: string): Promise<void> {
  const button = page.getByRole("button", { name: buttonName, exact: true });
  await expect(button).toBeVisible();
  await button.click();
  const dialog = page.getByRole("dialog", { name: buttonName });
  await expect(dialog).toBeVisible();
  await expect(dialog).toContainText(digest);
  const runId = new URL(page.url()).pathname.split("/").pop()!;
  const projectionResponse = await page.request.get(`/api/runs/${runId}/projection`);
  expect(projectionResponse.ok()).toBe(true);
  const projection = await projectionResponse.json() as { run: { version: number }; project: { policy_version: number } };
  await expect(dialog).toContainText(`Run version ${projection.run.version}`);
  await expect(dialog).toContainText(`Policy version ${projection.project.policy_version}`);
  const artifactResponse = await page.request.get(`/api/artifacts/${digest}/text`);
  expect(artifactResponse.ok()).toBe(true);
  const artifact = await artifactResponse.json() as { digest: string; text: string };
  expect(artifact.digest).toBe(digest);
  const evidence = JSON.parse(artifact.text) as Record<string, unknown>;
  // Every digest and SHA in the frozen envelope must be rendered, not abbreviated.
  for (const value of Object.values(evidence)) {
    if (typeof value === "string" && /^[a-f0-9]{40}(?:[a-f0-9]{24})?$/.test(value)) {
      await expect(dialog).toContainText(value);
    }
  }
  if (typeof evidence.remote_remediation_limit === "number") {
    await expect(dialog).toContainText(`up to ${evidence.remote_remediation_limit} remote remediation cycles`);
  }
  await dialog.getByRole("button", { name: new RegExp(`Confirm ${buttonName.toLowerCase()}`) }).click();
  await expect(dialog).toBeHidden();
}
