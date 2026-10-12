// Read-only browser receipt for the live checkpoint fixture. The operator uses
// the same loopback redirect interactively for the owner action.
import { createRequire } from 'node:module';
import { readFile, writeFile } from 'node:fs/promises';
import { dirname, join } from 'node:path';

const require = createRequire(new URL('../../apps/web/package.json', import.meta.url));
const { chromium } = require('@playwright/test');

const [controlOrigin, outputPath, scenarioPath] = process.argv.slice(2);
if (!controlOrigin || !outputPath || !scenarioPath || !/^http:\/\/127\.0\.0\.1:\d+$/.test(controlOrigin)) {
  throw new Error('usage: node task_usage_monitoring_browser.mjs http://127.0.0.1:PORT RECEIPT.json SCENARIO.json');
}
const scenario = JSON.parse(await readFile(scenarioPath, 'utf8'));

const browser = await chromium.launch({ headless: true });
try {
  const page = await browser.newPage();
  const receipt = { observedAt: new Date().toISOString(), surfaces: [] };
  let webOrigin;
  for (const [kind, path, label] of [
    ['authoring', '/open-authoring', 'Authoring usage checkpoint'],
    ['run', scenario.run_path, 'Run usage checkpoint'],
  ]) {
    await page.goto(kind === 'authoring' ? controlOrigin + path : webOrigin + path,
      { waitUntil: 'domcontentloaded' });
    await page.waitForFunction(() => !location.hash.includes('bootstrap='), null, { timeout: 20000 });
    webOrigin = new URL(page.url()).origin;
    if (kind === 'run') await page.locator('button[data-section="tasks"]').click();
    const section = page.getByRole('region', { name: label });
    await section.waitFor({ timeout: 20000 });
    await page.waitForFunction((name) => {
      const text = document.querySelector(`[aria-label="${name}"]`)?.textContent ?? '';
      return text.includes('Input') && text.includes('output');
    }, label, { timeout: 20000 });
    const text = await section.innerText();
    await page.screenshot({ path: join(dirname(outputPath), `browser-${kind}.png`), fullPage: true });
    const api = await page.request.get(new URL('/api/health', page.url()).toString());
    if (!api.ok()) throw new Error(`${kind} API proxy was unavailable: ${api.status()}`);
    receipt.surfaces.push({
      kind, pathname: new URL(page.url()).pathname, rendered: true,
      checkpoint: text.includes('Checkpoint paused') || text.includes('Checkpoint status: Paused'),
      measured: text.includes('Input') && text.includes('output'),
      apiRole: (await api.json()).role,
    });
  }
  await page.goto(webOrigin + '/usage', { waitUntil: 'domcontentloaded' });
  const history = page.getByRole('region', { name: 'Historical task usage' });
  await history.waitFor({ timeout: 20000 });
  await page.getByLabel('Project filter').fill(scenario.project_id);
  await page.getByLabel('Task category filter').selectOption('brainstorm');
  await page.getByLabel('Provider filter').fill('google');
  await page.getByLabel('Model filter').fill('gemini-3.8-flash');
  await page.getByLabel('Effort filter').fill('medium');
  await page.getByRole('button', { name: 'Apply filters' }).click();
  const overview = page.getByRole('article', { name: 'Pooled overview' });
  await overview.getByText(/0 measured of 0 eligible · 2 excluded/).waitFor({ timeout: 20000 });
  const historyQuery = new URLSearchParams({
    project_id: scenario.project_id, subject_kind: 'authoring_job', task_kind: 'brainstorm',
    provider: 'google', model: 'gemini-3.8-flash', effort: 'medium',
  });
  const historyResponse = await page.request.get(`${webOrigin}/api/task-usage-report?${historyQuery}`);
  if (!historyResponse.ok()) throw new Error(`filtered history API: ${historyResponse.status()}`);
  const historyPayload = await historyResponse.json();
  if (historyPayload.excluded_outcomes.incomplete_usage !== 2
      || historyPayload.overview.excluded_count !== 2
      || historyPayload.overview.eligible_count !== 0) {
    throw new Error('filtered live-only authoring history lost its excluded subjects');
  }
  receipt.history = {
    filtered: true, overview: await overview.innerText(),
    excludedOutcomes: historyPayload.excluded_outcomes,
    comparableCohorts: Object.keys(historyPayload.comparable_references).length,
  };
  await page.screenshot({ path: join(dirname(outputPath), 'browser-history.png'), fullPage: true });
  for (const [kind, path, heading] of [
    ['project', `/projects/${scenario.project_id}`, 'Project'],
    ['profiles', '/subscription-profiles', 'Subscription profiles'],
  ]) {
    await page.goto(webOrigin + path, { waitUntil: 'domcontentloaded' });
    await page.getByRole('heading', { name: heading, exact: false }).first().waitFor({ timeout: 20000 });
    receipt.surfaces.push({ kind, pathname: new URL(page.url()).pathname, rendered: true });
    await page.screenshot({ path: join(dirname(outputPath), `browser-${kind}.png`), fullPage: true });
  }
  await writeFile(outputPath, JSON.stringify(receipt, null, 2), 'utf8');
  console.log(JSON.stringify(receipt));
} finally {
  await browser.close();
}
