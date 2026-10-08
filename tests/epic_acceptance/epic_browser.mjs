// Real-browser delivery action and observation of the frozen execution.
import { createRequire } from 'node:module';

const require = createRequire(new URL('../../apps/web/package.json', import.meta.url));
const { chromium, expect } = require('@playwright/test');
const chunks = [];
for await (const chunk of process.stdin) chunks.push(chunk);
const { origin, token, epicId, executionId, phase } = JSON.parse(Buffer.concat(chunks).toString('utf8'));
let browser;
try {
  browser = await chromium.launch({ headless: true });
  const context = await browser.newContext();
  await context.route('**/*', route => new URL(route.request().url()).origin === origin
    ? route.continue() : route.abort('blockedbyclient'));
  const page = await context.newPage();
  const url = new URL(`/epics/${epicId}?tab=delivery&execution_id=${executionId}`, origin);
  url.hash = `bootstrap=${encodeURIComponent(token)}`;
  await page.goto(url.href);
  await expect(page.getByText('Sequential Delivery', { exact: true })).toBeVisible({ timeout: 30000 });
  await expect(page.getByText('Work-Item Progression', { exact: true })).toBeVisible();
  await expect(page.getByText('Linked Child Runs & Human Approval Gates', { exact: true })).toBeVisible();
  await expect(page.getByText('#1 Deliver item 1')).toBeVisible();
  await expect(page.getByText('#2 Deliver item 2')).toBeVisible();
  await expect(page.getByText('#3 Deliver item 3')).toBeVisible();
  expect(new URL(page.url()).hash).toBe('');
  if (phase === 'enable') {
    await expect(page.getByText('Sequential delivery is off.')).toBeVisible();
    const changed = page.waitForResponse(response => response.request().method() === 'PUT'
      && response.url().endsWith(`/api/epics/${epicId}/executions/${executionId}/dispatch`));
    await page.getByRole('button', { name: 'Enable Sequential Delivery' }).click();
    expect((await changed).status()).toBe(200);
    await expect(page.getByText('Sequential delivery is on.')).toBeVisible();
  } else if (phase === 'plan') {
    await expect(page.getByText('Sequential delivery is on.')).toBeVisible();
    await expect(page.getByRole('link', { name: 'Review Gate at plan' })).toHaveAttribute('href', /\/runs\//);
    await expect(page.getByText('Blocked: The prerequisite work item has not started.')).toBeVisible();
  } else if (phase === 'finished') {
    await expect(page.getByText('Execution completed (status SUCCEEDED).')).toBeVisible();
    await expect(page.getByText('Sequential delivery is off.')).toBeVisible();
    for (const number of [1, 2, 3]) {
      await expect(page.getByRole('link', { name: `Run for Deliver item ${number}` })).toBeVisible();
    }
    await expect(page.getByText('Integrated and verified')).toHaveCount(3);
  } else throw new Error(`unknown phase ${phase}`);
  process.stdout.write(JSON.stringify({ phase, epicId, executionId, observed: true }));
} finally {
  await browser?.close();
}
