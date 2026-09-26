import AxeBuilder from '@axe-core/playwright';
import { test, expect, openRun } from './fixtures';

// Uses the existing deterministic backend fixture. These are layout/interaction
// assertions, not new approval shortcuts. Screenshot attachments are review
// evidence, not silently accepted golden snapshots.
test('cockpit layout, task navigation and mobile drawer remain accessible', async ({ page, restartScenario }, testInfo) => {
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await openRun(page, restartScenario.runId!, restartScenario.bootstrapToken);
  await expect(page.getByRole('region', { name: 'Checks at a glance' })).toBeVisible();
  await expect(page.getByRole('complementary', { name: 'Run context' })).toBeVisible();

  for (const [width, height] of [[1440, 900], [1280, 800], [768, 1024], [390, 844]]) {
    await page.setViewportSize({ width, height });
    await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true);
    await expect(page.getByRole('complementary', { name: 'Run context' })).toBeVisible();
    expect((await new AxeBuilder({ page }).analyze()).violations, `Accessibility at ${width}px`).toEqual([]);
    await testInfo.attach(`cockpit-${width}`, { body: await page.screenshot({ fullPage: true }), contentType: 'image/png' });
  }

  const menu = page.getByRole('button', { name: 'Open navigation' });
  await menu.click();
  const drawer = page.getByRole('dialog', { name: 'Navigation', exact: true });
  await expect(drawer).toBeVisible();
  await expect(drawer.getByRole('link', { name: 'Runs', exact: true })).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(drawer).not.toBeVisible();
  await expect(menu).toBeFocused();

  const tasks = page.getByRole('button', { name: 'Tasks', exact: true });
  await tasks.click();
  await expect(tasks).toHaveAttribute('aria-pressed', 'true');
  await expect(page.getByRole('region', { name: 'Subscription tasks' })).toBeVisible();
  await page.getByRole('button', { name: 'Overview', exact: true }).click();
  await expect(page.getByRole('region', { name: 'Subscription tasks' })).not.toBeVisible();
  await page.getByRole('button', { name: 'View checks', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Checks', exact: true })).toBeFocused();
});
