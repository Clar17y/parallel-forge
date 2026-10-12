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
  const profileOutbound = [];
  page.on('request', request => {
    if (request.method() === 'POST' && /\/api\/subscription-profiles(?:\/[^/]+\/versions)?$/.test(request.url())) {
      profileOutbound.push({ url: request.url(), body: request.postData() });
    }
  });
  let rawProjectPolicy;
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
  await page.goto(`${webOrigin}/epics/${scenario.budget_epic_id}?project_id=${scenario.project_id}&tab=delivery`,
    { waitUntil: 'domcontentloaded' });
  const budgetHeading = page.getByText('Shared Epic Budget & Ceilings');
  await budgetHeading.waitFor({ timeout: 20000 });
  const budgetRoute = `${webOrigin}/api/epics/${scenario.budget_epic_id}/budget`;
  const outboundBudgetBodies = [];
  page.on('request', request => {
    if (request.url() === budgetRoute && request.method() === 'PUT') {
      outboundBudgetBodies.push(request.postData() ?? '');
    }
  });
  const initialBudget = await page.request.get(budgetRoute);
  if (!initialBudget.ok() || (await initialBudget.json()).ceiling.max_input_tokens !== null) {
    throw new Error('omitted epic token cap did not default to null');
  }
  const savedCaps = [];
  const editButton = page.getByRole('button', { name: 'Edit Budget Ceilings' });
  for (const cap of ['9007199254740991', '9007199254740992', '9007199254740993',
    '9223372036854775807']) {
    await editButton.click();
    const form = page.locator('form').filter({ hasText: 'Edit Shared Ceilings' });
    await form.getByLabel('Input tokens ceiling').fill(cap);
    await form.getByLabel('Output tokens ceiling').fill(cap);
    await form.getByRole('button', { name: 'Save Ceilings' }).click();
    await form.waitFor({ state: 'hidden', timeout: 20000 });
    if (!outboundBudgetBodies.some(body =>
      new RegExp(`"max_input_tokens"\\s*:\\s*${cap}\\b`).test(body)
      && new RegExp(`"max_output_tokens"\\s*:\\s*${cap}\\b`).test(body))) {
      throw new Error(`epic budget browser request quoted or rounded numeric cap ${cap}`);
    }
    const raw = await (await page.request.get(budgetRoute)).text();
    if (!new RegExp(`"max_input_tokens"\\s*:\\s*${cap}\\b`).test(raw)
        || !new RegExp(`"max_output_tokens"\\s*:\\s*${cap}\\b`).test(raw)) {
      throw new Error(`epic budget API rounded numeric cap ${cap}`);
    }
    const storedBudget = (await (await page.request.get(`${controlOrigin}/snapshot`)).json()).budget;
    if (storedBudget.max_input_tokens !== cap || storedBudget.max_output_tokens !== cap) {
      throw new Error(`PostgreSQL rounded epic cap ${cap}`);
    }
    savedCaps.push(cap);
  }
  await editButton.click();
  const invalidForm = page.locator('form').filter({ hasText: 'Edit Shared Ceilings' });
  for (const invalid of ['9223372036854775808', '-1', '1.5']) {
    await invalidForm.getByLabel('Input tokens ceiling').fill(invalid);
    await invalidForm.getByRole('button', { name: 'Save Ceilings' }).click();
    await invalidForm.getByRole('alert').waitFor({ timeout: 5000 });
    const unchanged = (await (await page.request.get(`${controlOrigin}/snapshot`)).json()).budget;
    if (unchanged.max_input_tokens !== savedCaps.at(-1)) {
      throw new Error(`invalid browser cap ${invalid} mutated PostgreSQL`);
    }
  }
  const rebasedCap = '9007199254740992';
  await invalidForm.getByLabel('Input tokens ceiling').fill(rebasedCap);
  await invalidForm.getByLabel('Output tokens ceiling').fill(rebasedCap);
  await invalidForm.getByLabel('Optional owner note').fill('Owner rebase verified in browser');
  const csrfReply = await page.request.get(`${webOrigin}/api/auth/csrf`);
  if (!csrfReply.ok()) throw new Error('browser CSRF refresh failed');
  const currentBudget = await (await page.request.get(budgetRoute)).json();
  const competing = await page.request.put(budgetRoute, {
    headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': (await csrfReply.json()).csrf_token,
      'Idempotency-Key': crypto.randomUUID(), Origin: webOrigin },
    data: JSON.stringify({ expected_version: currentBudget.version,
      ceiling: { max_input_tokens: 42, max_output_tokens: 42 } }),
  });
  if (!competing.ok()) throw new Error(`competing owner edit failed: ${competing.status()}`);
  await invalidForm.getByRole('button', { name: 'Save Ceilings' }).click();
  await page.getByRole('button', { name: 'Rebase changes onto latest version' }).waitFor({ timeout: 10000 });
  await page.getByRole('button', { name: 'Rebase changes onto latest version' }).click();
  await invalidForm.getByRole('button', { name: 'Save Ceilings' }).click();
  await invalidForm.waitFor({ state: 'hidden', timeout: 20000 });
  const rebasedBudget = (await (await page.request.get(`${controlOrigin}/snapshot`)).json()).budget;
  if (rebasedBudget.max_input_tokens !== rebasedCap || rebasedBudget.max_output_tokens !== rebasedCap) {
    throw new Error('stale save lost the owner draft after rebase');
  }
  await editButton.click();
  const clearForm = page.locator('form').filter({ hasText: 'Edit Shared Ceilings' });
  await clearForm.getByLabel('Input tokens ceiling').fill('');
  await clearForm.getByLabel('Output tokens ceiling').fill('');
  await clearForm.getByRole('button', { name: 'Save Ceilings' }).click();
  await clearForm.waitFor({ state: 'hidden', timeout: 20000 });
  const clearedCaps = (await (await page.request.get(`${controlOrigin}/snapshot`)).json()).budget;
  if (clearedCaps.max_input_tokens !== null || clearedCaps.max_output_tokens !== null) {
    throw new Error('browser clear did not persist null epic caps');
  }
  receipt.epicCaps = { epicId: scenario.budget_epic_id, savedCaps, invalidWritesRefused: 3,
    staleRebasedCap: rebasedCap, cleared: true, sqlVersion: clearedCaps.version };
  await page.screenshot({ path: join(dirname(outputPath), 'browser-epic-caps.png'), fullPage: true });
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
  const historyQuery = new URLSearchParams({
    project_id: scenario.project_id, subject_kind: 'authoring_job', task_kind: 'brainstorm',
    provider: 'google', model: 'gemini-3.8-flash', effort: 'medium',
  });
  const historyResponse = await page.request.get(`${webOrigin}/api/task-usage-report?${historyQuery}`);
  if (!historyResponse.ok()) throw new Error(`filtered history API: ${historyResponse.status()}`);
  const historyPayload = await historyResponse.json();
  if (historyPayload.overview.eligible_count !== 0
      || historyPayload.overview.excluded_count < 1) {
    throw new Error(`filtered live-only authoring history lost its excluded subjects: ${JSON.stringify(historyPayload.overview)}`);
  }
  await overview.getByText(new RegExp(`${historyPayload.overview.coverage_count} measured of ${historyPayload.overview.eligible_count} eligible · ${historyPayload.overview.excluded_count} excluded`))
    .waitFor({ timeout: 20000 });
  receipt.history = {
    filtered: true, overview: await overview.innerText(),
    excludedOutcomes: historyPayload.excluded_outcomes,
    comparableCohorts: Object.keys(historyPayload.comparable_references).length,
  };
  await page.screenshot({ path: join(dirname(outputPath), 'browser-history.png'), fullPage: true });
  await page.getByLabel('Task category filter').selectOption('');
  await page.getByLabel('Provider filter').fill('');
  await page.getByLabel('Model filter').fill('');
  await page.getByLabel('Effort filter').fill('');
  await page.getByLabel('Project filter').fill(scenario.measured_project_id);
  await page.getByRole('button', { name: 'Apply filters' }).click();
  await overview.getByText(/2 measured of 2 eligible · 0 excluded/).waitFor({ timeout: 20000 });
  const measured = await page.request.get(`${webOrigin}/api/task-usage-report?project_id=${scenario.measured_project_id}`);
  if (!measured.ok() || (await measured.json()).overview.sample_count !== 2) {
    throw new Error('second registered project lost measured and zero-usage samples');
  }
  await page.getByLabel('Project filter').fill(scenario.unknown_project_id);
  await page.getByRole('button', { name: 'Apply filters' }).click();
  await overview.getByText(/0 measured of 0 eligible · 1 excluded/).waitFor({ timeout: 20000 });
  await page.getByLabel('Project filter').fill('');
  await page.getByRole('button', { name: 'Apply filters' }).click();
  const all = await page.request.get(`${webOrigin}/api/task-usage-report`);
  const allReport = await all.json();
  if (!all.ok() || allReport.overview.sample_count < 2
      || !allReport.filters.project_ids.includes(scenario.project_id)
      || !allReport.filters.project_ids.includes(scenario.measured_project_id)
      || !allReport.filters.project_ids.includes(scenario.unknown_project_id)) {
    throw new Error('pooled report failed accessible-project aggregation');
  }
  await overview.getByText(new RegExp(`${allReport.overview.coverage_count} measured of ${allReport.overview.eligible_count} eligible · ${allReport.overview.excluded_count} excluded`))
    .waitFor({ timeout: 20000 });
  const missing = await page.request.get(`${webOrigin}/api/task-usage-report?project_id=00000000-0000-4000-8000-000000000000`);
  if (!missing.ok() || (await missing.json()).overview.sample_count !== 0) {
    throw new Error('unregistered project exposed report rows');
  }
  const anonymous = await browser.newContext();
  try {
    const denied = await anonymous.request.get(`${webOrigin}/api/task-usage-report`);
    if (denied.status() !== 401) throw new Error(`anonymous report returned ${denied.status()}`);
  } finally {
    await anonymous.close();
  }
  receipt.multiProjectHistory = {
    measuredProject: scenario.measured_project_id,
    unknownProject: scenario.unknown_project_id,
    projects: allReport.filters.project_ids,
    measuredSamples: allReport.overview.sample_count,
    excluded: allReport.overview.excluded_count,
    anonymousStatus: 401,
  };
  const stored = await page.request.get(`${controlOrigin}/snapshot`);
  if (!stored.ok()) throw new Error(`PostgreSQL fixture snapshot failed: ${stored.status()}`);
  const sourceRows = new Map((await stored.json()).report_sources.map(row => [row[0], row.slice(1)]));
  if (JSON.stringify(sourceRows.get(scenario.measured_project_id)) !== JSON.stringify([2, 2, 10, 5])
      || JSON.stringify(sourceRows.get(scenario.unknown_project_id)) !== JSON.stringify([1, 0, 0, 0])) {
    throw new Error('browser report did not match durable project source rows');
  }
  for (const [kind, path, heading] of [
    ['project', `/projects/${scenario.project_id}`, 'Project'],
    ['profiles', '/subscription-profiles', 'Subscription profiles'],
  ]) {
    await page.goto(webOrigin + path, { waitUntil: 'domcontentloaded' });
    await page.getByRole('heading', { name: heading, exact: false }).first().waitFor({ timeout: 20000 });
    if (kind === 'project') {
      const policyForm = page.locator('form').filter({ has: page.getByRole('button', { name: 'Create policy version' }) });
      await policyForm.getByText(/API models & budgets/).click();
      await policyForm.getByText('Advanced model and budget').first().click();
      await policyForm.getByRole('textbox', { name: 'Planner input tokens hard cap' }).fill('9007199254740993');
      await policyForm.getByRole('textbox', { name: 'Planner output tokens hard cap' }).fill('9223372036854775807');
      await policyForm.getByRole('button', { name: 'Create policy version' }).click();
      await page.getByRole('heading', { name: 'Policy version 2' }).waitFor({ timeout: 20000 });
      const projectPolicyRoute = `${webOrigin}/api/projects/${scenario.project_id}/policy-versions/2`;
      const projectPolicyReply = await page.request.get(projectPolicyRoute);
      if (!projectPolicyReply.ok()) throw new Error(`project policy read failed: ${projectPolicyReply.status()}`);
      rawProjectPolicy = await projectPolicyReply.text();
      for (const [field, cap] of Object.entries({
        max_input_tokens: '9007199254740993', max_output_tokens: '9223372036854775807',
      })) {
        if (!new RegExp(`"${field}"\\s*:\\s*${cap}\\b`).test(rawProjectPolicy)) {
          throw new Error(`project model policy API rounded ${field}`);
        }
      }
      const savedProjectCaps = (await (await page.request.get(`${controlOrigin}/snapshot`)).json()).project_model_caps;
      if (savedProjectCaps.version !== 2 || savedProjectCaps.max_input_tokens !== '9007199254740993'
          || savedProjectCaps.max_output_tokens !== '9223372036854775807') {
        throw new Error('project model policy differs from PostgreSQL');
      }
      receipt.projectModelCaps = { apiNumeric: true, sqlExact: true, version: savedProjectCaps.version };
      const form = page.locator('form[aria-label="Revise task usage monitoring policy"]');
      await form.waitFor({ timeout: 20000 });
      await form.getByText('Advanced hard caps and manual estimates').click();
      const policyRoute = `${webOrigin}/api/projects/${scenario.project_id}/monitoring-policy`;
      const exactFields = ['history_limit', 'minimum_comparables', 'window_days',
        'hard_token_cap', 'hard_context_cap', 'manual_estimate_tokens'];
      const savedPolicyCaps = [];
      let storedPolicy;
      for (const cap of ['9007199254740991', '9007199254740992',
        '9007199254740993', '9223372036854775807']) {
        await form.getByLabel('History limit').fill(cap);
        await form.getByLabel('Minimum comparables').fill(cap);
        await form.getByLabel('Rolling window days').fill(cap);
        await form.getByLabel('Optional hard token cap').fill(cap);
        await form.getByLabel('Optional hard context cap').fill(cap);
        await form.getByLabel('Manual estimate tokens').fill(cap);
        await form.getByRole('button', { name: 'Save monitoring policy' }).click();
        await page.waitForFunction(async ({ route, expected }) => {
          const response = await fetch(route);
          return response.ok && new RegExp(`"history_limit"\\s*:\\s*${expected}\\b`).test(await response.text());
        }, { route: policyRoute, expected: cap }, { timeout: 20000 });
        const policyReply = await page.request.get(policyRoute);
        if (!policyReply.ok()) throw new Error(`monitoring policy read failed: ${policyReply.status()}`);
        const rawPolicy = await policyReply.text();
        for (const field of exactFields) {
          if (!new RegExp(`"${field}"\\s*:\\s*${cap}\\b`).test(rawPolicy)) {
            throw new Error(`monitoring policy API rounded ${field}: ${rawPolicy.slice(0, 450)}`);
          }
        }
        storedPolicy = (await (await page.request.get(`${controlOrigin}/snapshot`)).json()).policy_exact;
        for (const field of exactFields) {
          if (storedPolicy[field] !== cap) {
            throw new Error(`browser monitoring policy ${field} differs from PostgreSQL`);
          }
        }
        savedPolicyCaps.push({ cap, revision: storedPolicy.revision });
      }
      await form.getByLabel('Rolling window days').fill('');
      await form.getByLabel('Manual estimate tokens').fill('9007199254740992');
      const csrf = await (await page.request.get(`${webOrigin}/api/auth/csrf`)).json();
      const competingPolicy = await page.request.post(policyRoute, {
        headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf.csrf_token,
          'Idempotency-Key': crypto.randomUUID(), Origin: webOrigin },
        data: JSON.stringify({ expected_revision: storedPolicy.revision, hard_context_cap: 44 }),
      });
      if (!competingPolicy.ok()) throw new Error(`competing monitoring policy edit: ${competingPolicy.status()}`);
      await form.getByRole('button', { name: 'Save monitoring policy' }).click();
      const reloadPolicy = form.getByRole('button', { name: 'Load current revision and keep edits' });
      await reloadPolicy.waitFor({ timeout: 10000 });
      await reloadPolicy.click();
      await form.getByRole('button', { name: 'Save monitoring policy' }).click();
      await page.waitForFunction(async route => {
        const response = await fetch(route);
        return response.ok && (await response.json()).window_days === null;
      }, policyRoute, { timeout: 20000 });
      const rebasedPolicy = (await (await page.request.get(`${controlOrigin}/snapshot`)).json()).policy_exact;
      if (rebasedPolicy.window_days !== null
          || rebasedPolicy.manual_estimate_tokens !== '9007199254740992'
          || rebasedPolicy.history_limit !== '9223372036854775807'
          || rebasedPolicy.minimum_comparables !== '9223372036854775807'
          || rebasedPolicy.hard_token_cap !== '9223372036854775807'
          || rebasedPolicy.hard_context_cap !== '44') {
        throw new Error('stale monitoring policy rebase lost exact owner draft');
      }
      receipt.monitoringPolicy = { exactFields: true, sqlRevision: rebasedPolicy.revision,
        savedPolicyCaps, windowCleared: true, staleRebased: true };
    } else {
      const caps = ['9007199254740991', '9007199254740992',
        '9007199254740993', '9223372036854775807', null];
      let createdProfileId;
      const profileReads = [];
      for (const [index, cap] of caps.entries()) {
        if (index > 0) {
          await page.locator('details.saved-profile-history').evaluate(element => { element.open = true; });
          await page.locator('.profile-version-card').filter({ hasText: createdProfileId })
            .getByRole('button', { name: `Append from latest version ${index}` }).click();
        }
        const editor = page.locator('form').filter({ has: page.getByRole('button', {
          name: index === 0 ? 'Create profile version 1' : `Append version ${index + 1}`, exact: true,
        }) });
        await editor.getByText('Advanced', { exact: true }).first().click();
        for (const field of ['Primary input token budget', 'Primary output token budget']) {
          await editor.getByRole('textbox', { name: field }).fill(cap ?? '');
        }
        const responsePromise = page.waitForResponse(response => response.request().method() === 'POST'
          && /\/api\/subscription-profiles(?:\/[^/]+\/versions)?$/.test(response.url()));
        await editor.getByRole('button', { name: index === 0 ? 'Create profile version 1' : `Append version ${index + 1}` }).click();
        const response = await responsePromise;
        if (!response.ok()) throw new Error(`profile version ${index + 1} save returned ${response.status()}: ${(await response.text()).slice(0, 500)}`);
        const raw = await response.text();
        const profile = JSON.parse(raw);
        createdProfileId ??= profile.profile_id;
        if (profile.profile_id !== createdProfileId || profile.version !== index + 1) {
          throw new Error(`profile version identity mismatch: ${raw.slice(0, 300)}`);
        }
        const readback = await page.request.get(`${webOrigin}/api/subscription-profiles/${createdProfileId}/versions/${index + 1}`);
        if (!readback.ok()) throw new Error(`profile version ${index + 1} readback returned ${readback.status()}`);
        const readbackRaw = await readback.text();
        for (const field of ['max_input_tokens', 'max_output_tokens']) {
          const pattern = cap === null ? new RegExp(`"${field}"\\s*:\\s*null`)
            : new RegExp(`"${field}"\\s*:\\s*${cap}\\b`);
          if (!pattern.test(raw)) throw new Error(`profile API rounded ${field} at version ${index + 1}`);
          if (!pattern.test(readbackRaw)) throw new Error(`profile GET rounded ${field} at version ${index + 1}`);
          const sent = profileOutbound.at(-1)?.body ?? '';
          if (!pattern.test(sent)) throw new Error(`profile POST body quoted or changed ${field} at version ${index + 1}: ${sent.slice(0, 300)}`);
        }
        profileReads.push({ version: index + 1, cap, postResponse: raw, getReadback: readbackRaw });
        if (index < caps.length - 1) {
          await page.locator('details.saved-profile-history').evaluate(element => { element.open = true; });
          await page.locator('.profile-version-card').filter({ hasText: createdProfileId })
            .getByRole('button', { name: `Append from latest version ${index + 1}` }).waitFor({ timeout: 20000 });
        }
      }
      const storedProfiles = (await (await page.request.get(`${controlOrigin}/snapshot`)).json()).profile_versions
        .filter(row => row[0] === createdProfileId);
      if (storedProfiles.length !== caps.length || storedProfiles.some((row, index) =>
        row[1] !== index + 1 || row[2] !== caps[index] || row[3] !== caps[index])) {
        throw new Error(`profile PostgreSQL version/cap mismatch: ${JSON.stringify(storedProfiles)}`);
      }
      receipt.profileCaps = { profileId: createdProfileId, versions: storedProfiles, numericPostBodies: profileOutbound.length };
      await writeFile(join(dirname(outputPath), 'browser-profile-api.json'),
        JSON.stringify(profileReads, null, 2), 'utf8');
    }
    receipt.surfaces.push({ kind, pathname: new URL(page.url()).pathname, rendered: true });
    await page.screenshot({ path: join(dirname(outputPath), `browser-${kind}.png`), fullPage: true });
  }
  const evidenceDir = dirname(outputPath);
  await writeFile(join(evidenceDir, 'browser-epic-outbound.json'),
    JSON.stringify(outboundBudgetBodies, null, 2), 'utf8');
  await writeFile(join(evidenceDir, 'browser-profile-outbound.json'),
    JSON.stringify(profileOutbound, null, 2), 'utf8');
  await writeFile(join(evidenceDir, 'browser-project-model-api.json'), rawProjectPolicy, 'utf8');
  await writeFile(join(evidenceDir, 'browser-report-api.json'), JSON.stringify(allReport, null, 2), 'utf8');
  await writeFile(join(evidenceDir, 'browser-policy-api.json'),
    await (await page.request.get(`${webOrigin}/api/projects/${scenario.project_id}/monitoring-policy`)).text(), 'utf8');
  await writeFile(join(evidenceDir, 'browser-sql-snapshot.json'),
    await (await page.request.get(`${controlOrigin}/snapshot`)).text(), 'utf8');
  await writeFile(outputPath, JSON.stringify(receipt, null, 2), 'utf8');
  console.log(JSON.stringify(receipt));
} finally {
  await browser.close();
}
