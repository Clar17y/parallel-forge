// Shared low-level browser acceptance helpers for Forge subscription flows.
import { createRequire } from 'node:module';

const require = createRequire(new URL('../../apps/web/package.json', import.meta.url));
const { expect } = require('@playwright/test');

export function validateLoopbackOrigins(webOrigin, controlOrigin) {
  const web = new URL(webOrigin);
  const control = new URL(controlOrigin);
  for (const origin of [web, control]) {
    if (
      origin.protocol !== 'http:' ||
      origin.hostname !== '127.0.0.1' ||
      origin.username ||
      origin.password ||
      origin.pathname !== '/'
    ) {
      throw new Error('loopback origins required');
    }
  }
  return { web, control };
}

export function loadLoopbackOrigins(env = process.env) {
  return validateLoopbackOrigins(env.FORGE_E2E_WEB_ORIGIN, env.FORGE_E2E_CONTROL_ORIGIN);
}

export function createControlBridge(control) {
  return async function bridge(path, method = 'GET') {
    const response = await fetch(new URL(path, control), { method });
    if (!response.ok) throw new Error(`test control failed: ${response.status} ${path}`);
    return response.json();
  };
}

export async function waitForRunHeader(page, expected) {
  const status = page.locator('[aria-label="Current run status"]');
  await expect(status).toBeVisible({ timeout: 20000 });
  await expect(status).toHaveAttribute('data-run-state', expected);
  await expect(page.getByText('Events: connected', { exact: true })).toBeVisible();
}

export function loseFirstResponse(page, path) {
  const requests = [];
  const replies = [];
  const statuses = [];
  const parseErrors = [];
  const handler = async route => {
    if (route.request().method() !== 'POST') return route.continue();
    requests.push({
      body: route.request().postData(),
      key: route.request().headers()['idempotency-key'],
    });
    let response;
    try {
      response = await route.fetch();
    } catch {
      await route.abort('failed');
      return;
    }
    statuses.push(response.status());
    let data = null;
    try {
      data = await response.json();
    } catch (error) {
      parseErrors.push({
        status: response.status(),
        error: String(error?.message ?? error),
      });
    }
    replies.push(data);
    if (requests.length === 1) {
      await route.abort('failed');
    } else {
      await route.fulfill({ response });
    }
  };
  return {
    requests,
    replies,
    statuses,
    parseErrors,
    install: () => page.route(path, handler),
    remove: () => page.unroute(path, handler),
  };
}
