// Real-browser A8 operator flow. Bootstrap material stays in memory; no tracing.
import { createRequire } from 'node:module';
const require = createRequire(new URL('../../apps/web/package.json', import.meta.url));
const { chromium, expect } = require('@playwright/test');
const web = new URL(process.env.FORGE_E2E_WEB_ORIGIN);
const control = new URL(process.env.FORGE_E2E_CONTROL_ORIGIN);
for (const origin of [web, control]) {
  if (origin.protocol !== 'http:' || origin.hostname !== '127.0.0.1' || origin.username || origin.password || origin.pathname !== '/') throw new Error('loopback origins required');
}
const secrets = [];
async function bridge(path, method = 'GET') {
  const response = await fetch(new URL(path, control), { method });
  if (!response.ok) throw new Error(`test control failed: ${response.status}`);
  return response.json();
}
async function api(page, path, body, method = 'POST') {
  return page.evaluate(async ({ path, body, method }) => {
    const headers = { 'Content-Type': 'application/json', 'Idempotency-Key': crypto.randomUUID() };
    if (body !== undefined) {
      const csrf = await fetch('/api/auth/csrf').then(response => response.json());
      headers['X-CSRF-Token'] = csrf.csrf_token;
    }
    const response = await fetch(`/api${path}`, { method: body === undefined ? 'GET' : method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
    if (!response.ok) throw new Error(`operator API failed: ${response.status} ${path}`);
    return response.json();
  }, { path, body, method });
}
async function state(page, run, expected) {
  await expect.poll(async () => (await api(page, `/runs/${run}`)).state, { timeout: 20000 }).toBe(expected);
}
async function header(page, expected) {
  const status = page.locator('[aria-label="Current run status"]');
  await expect(status).toBeVisible({ timeout: 20000 });
  await expect(status).toHaveAttribute('data-run-state', expected);
  await expect(page.getByText('Events: connected', { exact: true })).toBeVisible();
}
async function confirm(page, name) {
  await page.getByRole('region', { name: 'Run controls', exact: true }).getByRole('button', { name, exact: true }).click();
  const dialog = page.getByRole('dialog', { name, exact: true });
  await expect(dialog).toBeVisible();
  await dialog.getByRole('button', { name: `Confirm ${name.toLowerCase()}`, exact: true }).click();
}
function loseFirstResponse(page, path) {
  const requests = [];
  const replies = [];
  const handler = async route => {
    if (route.request().method() !== 'POST') return route.continue();
    requests.push({ body: route.request().postData(), key: route.request().headers()['idempotency-key'] });
    const response = await route.fetch();
    expect(response.ok()).toBe(true);
    replies.push(await response.json());
    if (requests.length === 1) await route.abort('failed');
    else await route.fulfill({ response });
  };
  return { requests, replies, install: () => page.route(path, handler), remove: () => page.unroute(path, handler) };
}

let browser;
try {
  const configuration = await bridge('/configuration');
  secrets.push(configuration.bootstrap);
  browser = await chromium.launch({ headless: true });
  const context = await browser.newContext();
  await context.route('**/*', route => new URL(route.request().url()).origin === web.origin ? route.continue() : route.abort('blockedbyclient'));
  const first = await context.newPage();
  await first.goto(new URL(`/subscription-profiles#bootstrap=${encodeURIComponent(configuration.bootstrap)}`, web).href);
  await expect(first.getByRole('heading', { name: 'Subscription profiles', exact: true })).toBeVisible();
  expect(new URL(first.url()).hash).toBe('');
  const registration = first.getByRole('region', { name: 'Worker registration', exact: true });
  await expect(registration).toContainText('No subscription routes registered by this worker.');
  await expect(registration).toContainText('It does not confirm sign-in');
  const workerBefore = await api(first, '/subscription-runtime');

  const create = first.getByRole('form', { name: 'Create subscription profile' });
  await create.getByLabel('Preferred route provider', { exact: true }).fill('openai');
  await create.getByLabel('Preferred route client', { exact: true }).fill('codex_app_server');
  await create.getByLabel('Preferred route model', { exact: true }).fill('gpt-6-astra');
  const createdResponse = first.waitForResponse(response => response.request().method() === 'POST' && response.url().endsWith('/api/subscription-profiles'));
  await create.getByRole('button', { name: 'Create profile version 1' }).click();
  const profileResponse = await createdResponse;
  expect(profileResponse.ok()).toBe(true);
  const profile = await profileResponse.json();
  await expect(first.getByRole('heading', { name: `Profile ${profile.profile_id} · version 1`, exact: true })).toBeVisible();

  // Project/task setup uses real authenticated API calls; profile edits and run
  // controls below use rendered product forms and confirmation dialogs.
  const project = await api(first, '/projects', {
    name: 'A8 browser profiles', repository_path: configuration.repository,
    github_repository: 'example/a8-profiles', default_branch: 'main',
    commands: [{ kind: 'test', name: 'unit', argv: ['python', '--version'], timeout_seconds: 30 }],
  });
  const selectionPath = `/projects/${project.id}/subscription-profile`;
  await api(first, selectionPath, { profile_id: profile.profile_id, profile_version: 1 }, 'PUT');
  async function createRun(title) {
    const task = await api(first, '/tasks', { project_id: project.id, title, body: 'Browser profile freeze fixture' });
    const run = await api(first, '/runs', { task_id: task.id });
    await state(first, run.id, 'PLANNING');
    return run.id;
  }
  const original = await createRun('Frozen browser Astra');
  const frozen = await bridge('/snapshot');
  const second = await context.newPage();
  await second.goto(new URL('/subscription-profiles', web).href);
  for (const page of [first, second]) await page.getByRole('button', { name: 'Append from latest version 1' }).click();
  const edit = first.getByRole('form', { name: 'Append profile version 1' });
  await edit.getByLabel('Preferred route model', { exact: true }).fill('gpt-5.6-sol');
  await edit.getByLabel('Preferred route effort', { exact: true }).selectOption('high');
  const staleEdit = second.getByRole('form', { name: 'Append profile version 1' });
  await staleEdit.getByLabel('Preferred route model', { exact: true }).fill('gpt-5.6-terra');

  const lostProfile = loseFirstResponse(first, `**/api/subscription-profiles/${profile.profile_id}/versions`);
  await lostProfile.install();
  await edit.getByRole('button', { name: 'Append version 2' }).click();
  await expect(edit.getByRole('alert')).toBeVisible();
  await edit.getByRole('button', { name: 'Append version 2' }).click();
  await expect(first.getByRole('heading', { name: `Profile ${profile.profile_id} · version 2`, exact: true })).toBeVisible();
  expect(lostProfile.requests).toHaveLength(2);
  expect(lostProfile.requests[1]).toEqual(lostProfile.requests[0]);
  expect(lostProfile.replies[1]).toEqual(lostProfile.replies[0]);
  await lostProfile.remove();
  await staleEdit.getByRole('button', { name: 'Append version 2' }).click();
  await expect(second.getByRole('alert').filter({ hasText: 'This profile changed in another tab.' })).toBeVisible();
  await expect(staleEdit).toBeHidden();
  await second.getByRole('button', { name: 'Reload profile history' }).click();
  await expect(second.getByRole('heading', { name: `Profile ${profile.profile_id} · version 2`, exact: true })).toBeVisible();
  expect((await api(first, '/subscription-profiles')).length).toBe(2);
  expect((await bridge('/snapshot')).envelopes[original]).toEqual(frozen.envelopes[original]);

  await api(first, selectionPath, { profile_id: profile.profile_id, profile_version: 2, expected_profile_id: profile.profile_id, expected_profile_version: 1 }, 'PUT');
  const future = await createRun('Explicit future browser Sol');
  const beforeRestart = await bridge('/snapshot');
  expect(beforeRestart.envelopes[original].model).toBe('gpt-6-astra');
  expect(beforeRestart.envelopes[original].effort).toBe('low');
  expect(beforeRestart.envelopes[future].model).toBe('gpt-5.6-sol');
  expect(beforeRestart.envelopes[future].effort).toBe('high');
  for (const page of [first, second]) {
    await page.goto(new URL(`/runs/${original}`, web).href);
    await header(page, 'PLANNING');
    const inspector = page.getByRole('region', { name: 'Subscription tasks', exact: true });
    await page.getByRole('button', { name: 'Tasks', exact: true }).click();
    await expect(page.getByRole('button', { name: 'Tasks', exact: true })).toHaveAttribute('aria-pressed', 'true');
    await expect(inspector).toContainText('Status: unknown');
    await expect(inspector).toContainText('gpt-6-astra · low · subscription · allowance_only');
    await inspector.getByRole('button', { name: /^Inspect primary task / }).click();
    await expect(inspector).toContainText('No attempts on this page.');
  }
  await second.getByRole('region', { name: 'Run controls', exact: true }).getByRole('button', { name: 'Pause', exact: true }).click();
  await expect(second.getByRole('dialog', { name: 'Pause', exact: true })).toBeVisible();
  await confirm(first, 'Pause');
  for (const page of [first, second]) await header(page, 'PAUSED');
  await expect(second.getByRole('dialog', { name: 'Pause', exact: true })).toBeHidden();
  await expect(second.getByRole('alert').filter({ hasText: 'The run changed.' })).toBeVisible();

  await bridge('/worker/stop', 'POST');
  const lostResume = loseFirstResponse(first, `**/api/runs/${original}/commands`);
  await lostResume.install();
  await confirm(first, 'Resume');
  await expect(first.getByRole('alert').filter({ hasText: 'The request could not be confirmed.' })).toBeVisible();
  await first.getByRole('dialog', { name: 'Resume', exact: true }).getByRole('button', { name: 'Confirm resume', exact: true }).click();
  await expect(first.getByRole('dialog', { name: 'Resume', exact: true })).toBeHidden();
  expect(lostResume.requests).toHaveLength(2);
  expect(lostResume.requests[1]).toEqual(lostResume.requests[0]);
  expect(lostResume.replies[1].id).toBe(lostResume.replies[0].id);
  await lostResume.remove();
  await state(first, original, 'PAUSED');
  await bridge('/api/restart', 'POST');
  await bridge('/worker/restart', 'POST');
  for (const page of [first, second]) await header(page, 'PLANNING');
  const workerAfter = await api(first, '/subscription-runtime');
  expect(workerAfter.workers.some(worker => !workerBefore.workers.some(old => old.worker_instance_id === worker.worker_instance_id))).toBe(true);
  const afterResume = await bridge('/snapshot');
  expect(afterResume.envelopes).toEqual(beforeRestart.envelopes);
  expect(afterResume.run_controls[original]).toEqual({ 'run.paused': 1, 'run.resumed': 1 });
  expect(afterResume.profile_versions).toBe(2);
  expect(Object.values(afterResume.execution_counts).every(count => count === 0)).toBe(true);
  await context.close();
  await browser.close();
  expect(browser.isConnected()).toBe(false);
  process.stdout.write(JSON.stringify({
    scenario: 'A8-browser-profiles-controls', provider_calls: false,
    proof: 'Chromium, public Next.js/API/worker, PostgreSQL and empty default runtime registry',
    original_run: original, future_run: future, before_restart: beforeRestart,
    after_resume: afterResume, browser_closed: true,
    profile_replay_count: lostProfile.requests.length, resume_replay_count: lostResume.requests.length,
    worker_instances_before: workerBefore.workers.map(worker => worker.worker_instance_id),
    worker_instances_after: workerAfter.workers.map(worker => worker.worker_instance_id),
    limits: 'Queued primary only; no active provider, individual specialist control or installed-client billing/isolation conformance',
  }));
} catch (error) {
  let message = String(error?.stack ?? error);
  for (const secret of secrets) message = message.replaceAll(secret, '[REDACTED]');
  process.stderr.write(message.slice(0, 6000));
  process.exitCode = 1;
} finally {
  if (browser?.isConnected()) await browser.close();
}
