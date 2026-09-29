// Recovery through rendered controls, the authenticated public API, and PostgreSQL.
import { createRequire } from 'node:module';
import { join } from 'node:path';
import {
  createControlBridge,
  loadLoopbackOrigins,
  loseFirstResponse,
  waitForRunHeader,
} from './subscription_browser_helpers.mjs';

const require = createRequire(new URL('../../apps/web/package.json', import.meta.url));
const { chromium, expect } = require('@playwright/test');
const { web, control } = loadLoopbackOrigins();
const bridge = createControlBridge(control);
const secrets = [];

async function openRecovery(page, runId, taskId, { navigate = true } = {}) {
  if (navigate) await page.goto(new URL(`/runs/${runId}`, web).href);
  await waitForRunHeader(page, 'IMPLEMENTING');
  const attention = page.getByRole('alert', { name: 'Workflow recovery attention' });
  await expect(attention).toBeVisible({ timeout: 20000 });
  await attention.getByRole('button', { name: 'Review recovery' }).click();
  await expect(page.getByRole('button', { name: 'Tasks', exact: true })).toHaveAttribute('aria-pressed', 'true');
  const inspector = page.getByRole('region', { name: 'Subscription tasks' });
  await expect(inspector.getByText('Primary needs recovery')).toBeVisible();
  await inspector.getByRole('button', { name: 'Recover run' }).click();
  const inspect = inspector.getByRole('button', { name: `Inspect primary task ${taskId}` });
  await expect(inspect).toHaveAttribute('aria-expanded', 'true');
  const recovery = inspector.getByRole('region', { name: 'Task recovery' });
  await expect(recovery).toBeVisible();
  return { inspector, recovery };
}

let browser;
try {
  const configuration = await bridge('/configuration');
  secrets.push(configuration.bootstrap);
  const { run_id: runId, task_id: taskId, attempt_id: attemptId } = configuration;
  const path = `**/api/runs/${runId}/subscription-tasks/${taskId}/attempts/${attemptId}/recovery`;
  browser = await chromium.launch({ headless: true });
  const context = await browser.newContext();
  await context.route('**/*', route => new URL(route.request().url()).origin === web.origin
    ? route.continue() : route.abort('blockedbyclient'));

  const first = await context.newPage();
  const pageErrors = [];
  const projectionInFlight = new Set();
  const eventRequests = [];
  const navigations = [];
  const projectionPath = `/api/runs/${runId}/projection`;
  const eventsPath = `/api/runs/${runId}/events`;
  const isProjection = request => request.method() === 'GET'
    && new URL(request.url()).pathname === projectionPath;
  first.on('request', request => {
    if (isProjection(request)) projectionInFlight.add(request);
    if (request.method() === 'GET' && new URL(request.url()).pathname === eventsPath) {
      eventRequests.push(request.url());
    }
  });
  first.on('requestfinished', request => projectionInFlight.delete(request));
  first.on('requestfailed', request => projectionInFlight.delete(request));
  first.on('framenavigated', frame => {
    if (frame === first.mainFrame()) navigations.push(frame.url());
  });
  first.on('pageerror', error => pageErrors.push(String(error.message)));
  await first.goto(new URL(`/subscription-profiles#bootstrap=${encodeURIComponent(configuration.bootstrap)}`, web).href);
  await expect(first.getByRole('heading', { name: 'Subscription profiles' })).toBeVisible();
  expect(new URL(first.url()).hash).toBe('');
  const initialProjectionResponse = first.waitForResponse(response => isProjection(response.request())
    && response.status() === 200);
  await first.goto(new URL(`/runs/${runId}`, web).href);
  await waitForRunHeader(first, 'IMPLEMENTING');
  const initialProjection = await initialProjectionResponse;
  await initialProjection.finished();
  expect((await initialProjection.json()).subscription_recovery_attention).toBe(false);
  await expect.poll(() => projectionInFlight.size).toBe(0);
  await expect(first.getByRole('button', { name: 'Refresh run' })).toBeEnabled();
  await expect(first.getByRole('button', { name: 'Overview', exact: true })).toHaveAttribute('aria-pressed', 'true');
  const attention = first.getByRole('alert', { name: 'Workflow recovery attention' });
  await expect(attention).toHaveCount(0);
  const initialEventRequests = eventRequests.length;
  const initialNavigations = navigations.length;
  expect(initialEventRequests).toBeGreaterThan(0);
  if (process.env.FORGE_E2E_EVIDENCE_ROOT) {
    await first.screenshot({ path: join(process.env.FORGE_E2E_EVIDENCE_ROOT, 'overview-before-recovery.png'), fullPage: true });
  }
  const diagnostic = await bridge('/diagnostic/settle', 'POST');
  expect(diagnostic.disposition).toBe('role_rejected');
  await expect(attention).toBeVisible({ timeout: 20000 });
  await expect(first.getByText('Events: connected', { exact: true })).toBeVisible();
  await expect(first.getByRole('button', { name: 'Overview', exact: true })).toHaveAttribute('aria-pressed', 'true');
  expect(eventRequests).toHaveLength(initialEventRequests);
  expect(navigations).toHaveLength(initialNavigations);
  await bridge('/worker/heartbeat', 'POST');
  const firstView = await openRecovery(first, runId, taskId, { navigate: false });
  expect((await first.locator('body').innerText()).trim().length).toBeGreaterThan(0);
  await expect(first.locator('[data-nextjs-dialog]')).toHaveCount(0);
  expect(pageErrors).toEqual([]);
  if (process.env.FORGE_E2E_EVIDENCE_ROOT) {
    await first.screenshot({ path: join(process.env.FORGE_E2E_EVIDENCE_ROOT, 'recovery-attention.png'), fullPage: true });
  }
  await expect(firstView.recovery).toContainText('approved-plan');
  await expect(firstView.recovery.getByRole('button', { name: 'Repair approved-plan instructions' })).toBeEnabled();

  const second = await context.newPage();
  const secondView = await openRecovery(second, runId, taskId);
  await bridge('/worker/heartbeat', 'POST');
  await secondView.recovery.getByRole('button', { name: 'Repair approved-plan instructions' }).click();
  const stalePreview = secondView.recovery.locator('[aria-label="Recovery preview"]');
  await expect(stalePreview).toBeVisible();
  await expect(stalePreview).toContainText('1');
  await stalePreview.getByLabel('Recovery reason').fill('Recover a retained incompatible primary result');

  await bridge('/worker/heartbeat', 'POST');
  await firstView.recovery.getByRole('button', { name: 'Repair approved-plan instructions' }).click();
  const preview = firstView.recovery.locator('[aria-label="Recovery preview"]');
  await expect(preview).toBeVisible();
  await expect(preview).toContainText('approved');
  await expect(preview).toContainText('Changes:');
  await expect(preview).toContainText('Retained evidence:');
  await expect(preview).toContainText('Provider attempts: 1 · Repair units: 1');
  await preview.getByRole('button', { name: 'Apply recovery' }).click();
  await expect(firstView.recovery).toContainText('Operator reason is required');
  await preview.getByLabel('Recovery reason').fill('Correct the approved-plan contract after reviewing preserved work');

  const before = await bridge('/snapshot');
  expect(before.receipt_count).toBe(0);
  expect(before.contract_revision_count).toBe(0);
  expect(before.repair_debits).toBe(0);
  expect(before.result_digest).toBeTruthy();
  const lost = loseFirstResponse(first, path);
  await lost.install();
  await bridge('/worker/heartbeat', 'POST');
  await preview.getByRole('button', { name: 'Apply recovery' }).click();
  await expect(firstView.recovery.getByRole('button', { name: 'Retry same request' })).toBeVisible();
  await firstView.recovery.getByRole('button', { name: 'Retry same request' }).click();
  await expect(firstView.recovery).toContainText('Recovery receipt');
  await expect(attention).toHaveCount(0, { timeout: 20000 });
  await expect(first.getByText('Events: connected', { exact: true })).toBeVisible();
  expect(eventRequests).toHaveLength(initialEventRequests);
  expect(navigations).toHaveLength(initialNavigations);
  expect(lost.statuses).toHaveLength(2);
  expect(lost.statuses[0]).toBeGreaterThanOrEqual(200);
  expect(lost.statuses[0]).toBeLessThan(300);
  expect(lost.statuses[1]).toBe(lost.statuses[0]);
  expect(lost.requests).toHaveLength(2);
  expect(lost.requests[1]).toEqual(lost.requests[0]);
  expect(lost.replies[1]).toEqual(lost.replies[0]);
  await lost.remove();

  const after = await bridge('/snapshot');
  expect(after.result_digest).toBe(before.result_digest);
  expect(after.result_payload).toEqual(before.result_payload);
  expect(after.receipt_count).toBe(1);
  expect(after.contract_revision_count).toBe(before.contract_revision_count + 1);
  expect(after.repair_debits).toBe(before.repair_debits + 1);
  expect(after.attempt_count).toBe(before.attempt_count);
  expect(after.provider_attempts_consumed).toBe(before.provider_attempts_consumed);

  const staleApply = stalePreview.getByRole('button', { name: 'Apply recovery' });
  let staleOutcome;
  if (await staleApply.count() && await staleApply.isEnabled()) {
    const rejected = second.waitForResponse(response => response.url().endsWith('/recovery')
      && response.request().method() === 'POST');
    await staleApply.click();
    expect((await rejected).status()).toBe(409);
    await expect(secondView.recovery).toContainText(/stale|expired|changed/i);
    staleOutcome = 'server-conflict';
  } else {
    await expect(secondView.recovery).toContainText(/state changed|fresh preview/i);
    staleOutcome = 'client-invalidated';
  }
  const afterStale = await bridge('/snapshot');
  expect(afterStale).toEqual(after);

  const restart = await bridge('/api/restart', 'POST');
  expect(restart.pid).toBeTruthy();
  await first.reload();
  await waitForRunHeader(first, 'IMPLEMENTING');
  await first.getByRole('button', { name: 'Tasks', exact: true }).click();
  const inspector = first.getByRole('region', { name: 'Subscription tasks' });
  await inspector.getByRole('button', { name: `Inspect primary task ${taskId}` }).click();
  await expect(inspector).toContainText('Attempt');
  const durable = await bridge('/snapshot');
  expect(durable).toEqual(after);

  const summary = ({ result_payload, ...counts }) => counts;

  await context.close();
  await browser.close();
  expect(browser.isConnected()).toBe(false);
  process.stdout.write(JSON.stringify({
    scenario: 'workflow-recovery-browser', provider_calls: false,
    proof: 'Chromium, rendered UI, authenticated API process, isolated PostgreSQL',
    run_id: runId, task_id: taskId, attempt_id: attemptId,
    replay_count: lost.requests.length, receipt_id: lost.replies[0].receipt_id,
    stale_outcome: staleOutcome,
    event_requests_before_restart: initialEventRequests,
    navigations_before_restart: initialNavigations,
    before: summary(before), after: summary(durable), browser_closed: true,
  }));
} catch (error) {
  let message = String(error?.stack ?? error);
  for (const secret of secrets) message = message.replaceAll(secret, '[REDACTED]');
  process.stderr.write(message.slice(0, 8000));
  process.exitCode = 1;
} finally {
  if (browser?.isConnected()) await browser.close();
}
