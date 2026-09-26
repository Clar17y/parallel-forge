// Active specialist controls and feedback through real browser/API processes.
import { createRequire } from 'node:module';
import {
  createControlBridge,
  loadLoopbackOrigins,
  loseFirstResponse,
  waitForRunHeader as header,
} from './subscription_browser_helpers.mjs';

const require = createRequire(new URL('../../apps/web/package.json', import.meta.url));
const { chromium, expect } = require('@playwright/test');
const { web, control } = loadLoopbackOrigins();
const bridge = createControlBridge(control);
const secrets = [];

function freezeTaskSnapshot(page, runId) {
  let frozen;
  let recoveryGate;
  let releaseRecovery;
  let recoverySettled;
  let settleRecovery;
  let recoveryRouteClaimed = false;
  let live = false;
  const path = `**/api/runs/${runId}/subscription-tasks?*`;
  const handler = async route => {
    if (route.request().method() !== 'GET') return route.continue();
    if (live) return route.continue();
    if (!frozen) {
      try {
        const response = await route.fetch();
        const status = response.status();
        frozen = {
          status,
          headers: response.headers(),
          body: await response.body(),
        };
      } catch {
        await route.abort('failed');
        return;
      }
    }
    if (recoveryGate) {
      await recoveryGate;
      if (recoveryRouteClaimed) return route.continue();
      recoveryRouteClaimed = true;
      live = true;
      let response;
      try {
        response = await route.fetch();
      } catch {
        await route.abort('failed');
        settleRecovery();
        return;
      }
      try {
        await route.fulfill({ response });
      } finally {
        settleRecovery();
      }
      return;
    }
    await route.fulfill(frozen);
  };
  return {
    get status() {
      return frozen?.status;
    },
    install: () => page.route(path, handler),
    holdRecovery: () => {
      recoveryGate = new Promise(resolve => { releaseRecovery = resolve; });
      recoverySettled = new Promise(resolve => { settleRecovery = resolve; });
    },
    releaseRecovery: async () => {
      releaseRecovery();
      await recoverySettled;
    },
    remove: () => page.unroute(path, handler),
  };
}

async function openWorker(page, runId, taskId) {
  await page.goto(new URL(`/runs/${runId}`, web).href);
  await header(page, 'IMPLEMENTING');
  await page.getByRole('button', { name: 'Tasks', exact: true }).click();
  const inspector = page.getByRole('region', { name: 'Subscription tasks', exact: true });
  const inspect = inspector.getByRole('button', {
    name: `Inspect routine_implementation task ${taskId}`,
    exact: true,
  });
  await expect(inspect).toBeVisible({ timeout: 20000 });
  if (await inspect.getAttribute('aria-expanded') !== 'true') await inspect.click();
  const controls = inspector.getByRole('region', { name: 'Task controls', exact: true });
  const feedback = inspector.getByRole('region', { name: 'Worker feedback', exact: true });
  await expect(feedback).toContainText(`Target worker: routine_implementation task ${taskId}`);
  return { inspector, controls, feedback };
}

let browser;
try {
  const configuration = await bridge('/configuration');
  secrets.push(configuration.bootstrap);
  const { run_id: runId, task_id: taskId } = configuration;
  browser = await chromium.launch({ headless: true });
  const context = await browser.newContext();
  await context.route('**/*', route => new URL(route.request().url()).origin === web.origin
    ? route.continue() : route.abort('blockedbyclient'));
  const first = await context.newPage();
  await first.goto(new URL(`/subscription-profiles#bootstrap=${encodeURIComponent(configuration.bootstrap)}`, web).href);
  await expect(first.getByRole('heading', { name: 'Subscription profiles', exact: true })).toBeVisible();
  expect(new URL(first.url()).hash).toBe('');

  const second = await context.newPage();
  const staleTasks = freezeTaskSnapshot(second, runId);
  await staleTasks.install();
  const firstView = await openWorker(first, runId, taskId);
  const secondView = await openWorker(second, runId, taskId);
  expect(staleTasks.status).toBe(200);
  for (const view of [firstView, secondView]) {
    await expect(view.inspector).toContainText('routine_implementation · leased');
    await expect(view.inspector).toContainText('Owned paths: src/counter.py, tests/test_counter.py');
    await expect(view.feedback).toContainText('No retained worker feedback receipts.');
  }

  const lostPause = loseFirstResponse(first, `**/api/runs/${runId}/subscription-tasks/${taskId}/controls`);
  await lostPause.install();
  await firstView.controls.getByLabel('Task control reason').fill('Inspect retained partial work');
  await firstView.controls.getByRole('button', { name: 'Pause task', exact: true }).click();
  await expect(firstView.controls.getByRole('button', { name: 'Retry same request', exact: true })).toBeVisible();
  await firstView.controls.getByRole('button', { name: 'Retry same request', exact: true }).click();
  await expect(firstView.controls).toContainText('Pause requested. Waiting for stopped work to be confirmed.');
  expect(lostPause.statuses).toEqual([200, 200]);
  expect(lostPause.requests).toHaveLength(2);
  expect(lostPause.requests[1]).toEqual(lostPause.requests[0]);
  expect(lostPause.parseErrors).toEqual([]);
  expect(lostPause.replies[0]).not.toBeNull();
  expect(lostPause.replies[1]).toEqual(lostPause.replies[0]);
  await lostPause.remove();

  const firstStop = await bridge('/active/first/finish', 'POST');
  expect(firstStop.stop_confirmed).toBe(true);
  expect(firstStop.partial_write).toBe(true);
  await bridge('/worker/restart', 'POST');
  await bridge('/api/restart', 'POST');

  // Keep the stale snapshot only long enough to form the 409 POST. The live
  // recovery GET is then explicitly held, proving the controls stay fenced.
  staleTasks.holdRecovery();
  await secondView.controls.getByLabel('Task control reason').fill('Stale duplicate pause');
  await secondView.controls.getByRole('button', { name: 'Pause task', exact: true }).click();
  await expect(secondView.controls).toContainText(
    'The task or run changed. Review the refreshed state before acting again.',
  );
  await expect(secondView.controls.getByLabel('Task control reason')).toHaveAttribute('readonly');
  await expect(secondView.controls.getByRole('button', { name: 'Pause task', exact: true })).toBeDisabled();
  expect((await bridge('/active/snapshot')).control_mutations).toBe(1);
  await staleTasks.releaseRecovery();
  await staleTasks.remove();
  await expect(secondView.controls).toContainText('Paused', { timeout: 20000 });
  await expect(secondView.controls.getByLabel('Task control reason')).not.toHaveAttribute('readonly');
  await expect(firstView.controls).toContainText('Paused', { timeout: 20000 });
  const paused = await bridge('/active/snapshot');
  expect(paused.control_mutations).toBe(1);
  expect(paused.task.pause_requested).toBe(true);

  const lostFeedback = loseFirstResponse(first, `**/api/runs/${runId}/subscription-tasks/${taskId}/feedback`);
  await lostFeedback.install();
  const feedbackText = 'Keep the retained parser bytes and add the missing replay assertion.';
  await firstView.feedback.getByLabel('Feedback for routine_implementation worker').fill(feedbackText);
  await firstView.feedback.getByRole('button', { name: 'Send worker feedback', exact: true }).click();
  await expect(firstView.feedback.getByRole('button', { name: 'Retry same request', exact: true })).toBeVisible();
  await firstView.feedback.getByRole('button', { name: 'Retry same request', exact: true }).click();
  await expect(firstView.feedback).toContainText(
    'Feedback recorded. The primary coordinator will forward its retained receipt.',
  );
  expect(lostFeedback.statuses).toEqual([200, 200]);
  expect(lostFeedback.requests).toHaveLength(2);
  expect(lostFeedback.requests[1]).toEqual(lostFeedback.requests[0]);
  expect(lostFeedback.parseErrors).toEqual([]);
  expect(lostFeedback.replies[0]).not.toBeNull();
  expect(lostFeedback.replies[1]).toEqual(lostFeedback.replies[0]);
  await lostFeedback.remove();
  await first.getByRole('button', { name: 'Refresh tasks', exact: true }).click();
  await expect(firstView.feedback).toContainText('Pending primary forwarding');

  const forwarded = await bridge('/active/feedback/forward', 'POST');
  expect(forwarded.status).toBe('forwarded');
  for (const page of [first, second]) {
    await page.getByRole('button', { name: 'Refresh tasks', exact: true }).click();
    await expect(page.getByRole('region', { name: 'Worker feedback', exact: true }))
      .toContainText('Forwarded to worker');
  }

  // Both public processes restart while the receipt and paused worker are
  // durable. Browser sessions then continue against the recreated processes.
  await bridge('/api/restart', 'POST');
  await bridge('/worker/restart', 'POST');
  for (const page of [first, second]) {
    await header(page, 'IMPLEMENTING');
    await page.getByRole('button', { name: 'Refresh tasks', exact: true }).click();
    await expect(page.getByRole('region', { name: 'Worker feedback', exact: true }))
      .toContainText('Forwarded to worker');
  }

  const blocked = await bridge('/active/quota/block', 'POST');
  expect(blocked.status).toBe('blocked');
  await first.getByRole('button', { name: 'Refresh tasks', exact: true }).click();
  await expect(firstView.inspector).toContainText('Status: blocked');
  await expect(firstView.inspector).toContainText('Reason: operator_report');
  await firstView.controls.getByLabel('Task control reason').fill('Resume under the retained quota fence');
  await firstView.controls.getByRole('button', { name: 'Resume task', exact: true }).click();
  await expect(firstView.controls).toContainText('Resume recorded. The scheduler will recheck eligibility before launching work.');

  const probe = await bridge('/active/quota/probe', 'POST');
  expect(probe.outcome).toBe('deferred');
  expect(probe.after.attempt_count).toBe(probe.before.attempt_count);
  expect(probe.after.repair_debits).toBe(probe.before.repair_debits);

  const resumed = await bridge('/active/second/start', 'POST');
  expect(resumed.feedback_receipt_id).toBe(lostFeedback.replies[0].receipt_id);
  for (const page of [first, second]) {
    await page.getByRole('button', { name: 'Refresh tasks', exact: true }).click();
    await expect(page.getByRole('region', { name: 'Subscription tasks', exact: true }))
      .toContainText('routine_implementation · leased');
  }

  await secondView.controls.getByLabel('Task control reason').fill('Cancel after proving resumed feedback delivery');
  await secondView.controls.getByRole('button', { name: 'Cancel task', exact: true }).click();
  await expect(secondView.controls).toContainText('Cancellation requested. Waiting for stopped work to be confirmed.');
  const secondStop = await bridge('/active/second/finish', 'POST');
  expect(secondStop.stop_confirmed).toBe(true);
  await bridge('/worker/restart', 'POST');
  for (const page of [first, second]) {
    await page.getByRole('button', { name: 'Refresh tasks', exact: true }).click();
    const inspector = page.getByRole('region', { name: 'Subscription tasks', exact: true });
    await expect(inspector).toContainText('Cancelled', { timeout: 20000 });
    await expect(page.getByRole('region', { name: 'Worker feedback', exact: true }))
      .toContainText('Delivered to worker');
  }

  const final = await bridge('/active/snapshot');
  expect(final.feedback_mutations).toBe(1);
  expect(final.control_mutations).toBe(3);
  expect(final.feedback_states).toEqual(['delivered']);
  expect(final.attempt_count).toBe(probe.before.attempt_count + 1);
  expect(final.repair_debits).toBe(probe.before.repair_debits);
  expect(final.task.owned_paths).toEqual(paused.task.owned_paths);
  expect(final.partial_digest).toBe(paused.partial_digest);
  expect(final.partial_write).toBe(true);
  expect(final.unsettled_effects).toBe(0);

  await context.close();
  await browser.close();
  expect(browser.isConnected()).toBe(false);
  process.stdout.write(JSON.stringify({
    scenario: 'active-specialist-feedback-controls',
    provider_calls: false,
    proof: 'Chromium, public Next.js/API/worker, PostgreSQL and supervised fake provider processes',
    run_id: runId,
    task_id: taskId,
    pause_replay_count: lostPause.requests.length,
    feedback_replay_count: lostFeedback.requests.length,
    paused,
    before_probe: probe.before,
    final,
    browser_closed: true,
    limits: 'Fake provider capability only; no official-client or allowance conformance claim',
  }));
} catch (error) {
  let message = String(error?.stack ?? error);
  for (const secret of secrets) message = message.replaceAll(secret, '[REDACTED]');
  process.stderr.write(message.slice(0, 8000));
  process.exitCode = 1;
} finally {
  if (browser?.isConnected()) await browser.close();
}
