import { expect, test, type Page } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';

async function mockSession(page: Page) {
  await page.route('**/api/**', route => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith('/auth/session')) return route.fulfill({ json: { actor_id: 'appearance-check', actor_class: 'operator' } });
    if (path.endsWith('/auth/csrf')) return route.fulfill({ json: { csrf_token: 'appearance-check' } });
    if (path.endsWith('/run-projections')) return route.fulfill({ json: { items: [], truncated: false } });
    return route.fulfill({ json: [] });
  });
}

async function addAppearancePreview(page: Page) {
  await page.locator('main').evaluate(main => {
    const preview = document.createElement('section');
    preview.className = 'panel';
    preview.innerHTML = '<h2>Mock appearance samples</h2><label>Run name<input value="Forge preview"></label><div class="run-status" data-tone="warning"><span class="status-badge" data-tone="warning">Waiting for approval</span></div><span class="provider-badge" data-provider="anthropic">Anthropic</span><table><tbody><tr class="diff-added-row"><td>Added line</td></tr><tr class="diff-removed-row"><td>Removed line</td></tr></tbody></table>';
    main.append(preview);
  });
}

test('System initializes dark, follows OS, and has a readable mobile shell', async ({ page }, testInfo) => {
  const consoleErrors: string[] = [];
  page.on('console', message => { if (message.type() === 'error') consoleErrors.push(message.text()); });
  await page.addInitScript(() => {
    const capture = () => {
      const target = window as Window & { __firstTheme?: string };
      const theme = document.documentElement?.dataset.theme;
      if (theme && !target.__firstTheme) target.__firstTheme = theme;
    };
    const observer = new MutationObserver(capture);
    observer.observe(document, { attributes: true, childList: true, subtree: true, attributeFilter: ['data-theme'] });
    window.addEventListener('DOMContentLoaded', () => { capture(); observer.disconnect(); }, { once: true });
  });
  await page.emulateMedia({ colorScheme: 'dark' });
  await mockSession(page);
  await page.goto('/runs');
  const appearance = page.getByRole('combobox', { name: 'Appearance' });
  await expect(appearance).toHaveValue('system');
  await expect.poll(() => page.locator('html').getAttribute('data-theme')).toBe('dark');
  expect(await page.evaluate(() => (window as Window & { __firstTheme?: string }).__firstTheme)).toBe('dark');
  await expect.poll(() => appearance.evaluate(element => getComputedStyle(element).backgroundColor)).not.toBe('rgb(255, 255, 255)');
  await expect(page.getByRole('navigation', { name: 'Primary' })).toBeVisible();
  await addAppearancePreview(page);
  const sampleColors = await page.locator('.panel input').evaluate(input => {
    const row = document.querySelector('.diff-added-row')!;
    const status = document.querySelector('.run-status')!;
    const provider = document.querySelector('.provider-badge')!;
    return [getComputedStyle(input).backgroundColor, getComputedStyle(status).backgroundColor,
      getComputedStyle(provider).color, getComputedStyle(row).backgroundColor];
  });
  expect(sampleColors[0]).toBe('rgb(27, 36, 44)');
  expect(sampleColors[1]).toBe('rgb(58, 46, 25)');
  expect(sampleColors[2]).toBe('rgb(255, 183, 126)');
  expect(sampleColors[3]).not.toBe(sampleColors[0]);

  await page.emulateMedia({ colorScheme: 'light' });
  await expect.poll(() => page.locator('html').getAttribute('data-theme')).toBe('light');
  await page.emulateMedia({ colorScheme: 'dark' });
  await expect.poll(() => page.locator('html').getAttribute('data-theme')).toBe('dark');

  await page.setViewportSize({ width: 390, height: 844 });
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await testInfo.attach('system-dark-mobile', { body: await page.screenshot({ fullPage: true }), contentType: 'image/png' });
  expect((await new AxeBuilder({ page }).include('.app-topbar').analyze()).violations).toEqual([]);
  expect((await new AxeBuilder({ page }).include('.panel').analyze()).violations).toEqual([]);
  expect(consoleErrors.filter(error => /hydration|did not match|server html/i.test(error))).toEqual([]);
});

test('explicit Light survives dark OS, and Dark survives navigation and reload', async ({ page }, testInfo) => {
  await page.emulateMedia({ colorScheme: 'dark' });
  await page.addInitScript(() => {
    if (localStorage.getItem('forge-appearance') === null) localStorage.setItem('forge-appearance', 'light');
  });
  await mockSession(page);
  await page.goto('/runs');
  const appearance = page.getByRole('combobox', { name: 'Appearance' });
  await expect(appearance).toHaveValue('light');
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'light');

  await appearance.focus();
  await page.keyboard.press('ArrowDown');
  await expect(appearance).toHaveValue('dark');
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
  await expect.poll(() => page.evaluate(() => localStorage.getItem('forge-appearance'))).toBe('dark');
  await page.goto('/projects');
  await expect(page.getByRole('combobox', { name: 'Appearance' })).toHaveValue('dark');
  await page.reload();
  await expect(page.getByRole('combobox', { name: 'Appearance' })).toHaveValue('dark');
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
  await testInfo.attach('explicit-dark', { body: await page.screenshot({ fullPage: true }), contentType: 'image/png' });
});

test('storage denial keeps the control and live theme switching functional', async ({ page }) => {
  await page.emulateMedia({ colorScheme: 'light' });
  await page.addInitScript(() => {
    Storage.prototype.getItem = () => { throw new DOMException('denied', 'SecurityError'); };
    Storage.prototype.setItem = () => { throw new DOMException('denied', 'SecurityError'); };
  });
  await mockSession(page);
  await page.goto('/runs');
  const appearance = page.getByRole('combobox', { name: 'Appearance' });
  await expect(appearance).toHaveValue('system');
  await appearance.selectOption('dark');
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
});
