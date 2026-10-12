import { afterEach, expect, test, vi } from 'vitest';
import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { RunUsageCheckpoint } from './run-usage-checkpoint';
import type { components } from '@/lib/api/schema';
import { parseExactJson, stringifyExactJson } from '@/lib/api/exact-integer';

const binding = {
  subject: { project_id: 'project-1', kind: 'run', subject_id: 'run-1', schema_version: 1 },
  work_unit_id: 'unit-1', admitted_phase_id: 'implementation',
  configured_route: { fingerprint: 'a'.repeat(64), members: [{ provider: 'google', model: 'flash', role: 'primary', ordinal: 0 }] },
  effective_route: { fingerprint: 'b'.repeat(64), members: [{ provider: 'google', model: 'flash', role: 'primary', ordinal: 0 }] },
  source: { kind: 'agent_execution', source_id: 'source-1', parent_id: null }, schema_version: 1,
};
const baseline = {
  binding, policy_revision: 4, snapshot_digest: 'c'.repeat(64), cohort_digest: 'd'.repeat(64), history_digest: 'e'.repeat(64),
  estimator_version: 'v1', comparison_scope: 'route', completeness: 'complete', coverage_count: 20,
  sample_count: 20, outcome_count: 20, source_count: 20, window_limit: 50, minimum_comparables: 20,
  status: 'ready', reference_tokens: 100, mean_tokens: 110, median_tokens: 100, p90_tokens: 140,
  warning_threshold_tokens: 150, checkpoint_threshold_tokens: 200, frozen_at: '2026-10-10T12:00:00Z', schema_version: 1,
};
const checkpoint: components['schemas']['CheckpointRecord'] = {
  checkpoint_id: 'checkpoint-1', binding, baseline_binding: binding, state: 'paused', policy_revision: 4,
  reference_digest: 'e'.repeat(64), trigger_event_id: 'trigger-1', version: 2, generation: 1,
  process_settled: true, next_checkpoint_threshold_tokens: null, usage_pauses_disabled: false, monitoring_policy_revision: 4,
};
const report: components['schemas']['UsageReport'] = {
  binding, policy_revision: 4, baseline, input_tokens: 0, output_tokens: 20, cached_input_tokens: 0,
  reasoning_output_tokens: null, context_occupied_tokens: null, context_capacity_tokens: null, compaction_count: null,
  completeness: 'complete', freshness: 'live', checkpoint_version: 2, checkpoint, warning: 'At the warning threshold', sources: [],
};
const response: components['schemas']['TaskUsageCheckpointResponse'] = { checkpoint, report };
afterEach(() => { cleanup(); vi.restoreAllMocks(); });
const exactOwnerJson = (value: unknown) => stringifyExactJson(value, new Set(['version', 'revision', 'result_version']));
const hasExactToken = (body: string, field: string, digits: string) =>
  expect(body).toMatch(new RegExp(`"${field}":${digits}(?=[,}])`));

test('replays a high policy command after uncertain response and newer checkpoint without changing body or key', async () => {
  const original = '9007199254740993';
  const receiptVersion = '9007199254740994';
  const currentVersion = '9007199254740995';
  const policyRevision = '9007199254740992';
  const requestedRevision = '9007199254740993';
  const highPolicy = { ...retainedPolicy, revision: policyRevision,
    hard_token_cap: '9223372036854775807', manual_estimate_tokens: '9007199254740992' };
  let visible = { ...response, checkpoint: { ...checkpoint, version: original } };
  const posts: { body: string; key: string | null }[] = [];
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
    if (init?.method === 'POST') {
      posts.push({ body: String(init.body), key: new Headers(init.headers).get('Idempotency-Key') });
      if (posts.length === 1) throw new TypeError('response lost');
      return new Response(exactOwnerJson({ receipt: { checkpoint_id: checkpoint.checkpoint_id,
        action: 'set_policy', state: 'paused', result_version: receiptVersion, replayed: true },
        checkpoint: { ...checkpoint, version: currentVersion }, report }), { status: 200 });
    }
    return new Response(exactOwnerJson(String(input).endsWith('/monitoring-policy') ? highPolicy : visible), { status: 200 });
  });
  const user = userEvent.setup();
  render(<RunUsageCheckpoint runId="run-1" runAllowsExecution runIsTerminal={false} quotaBlocked={false} manualPause={false} />);
  await user.click(await screen.findByRole('button', { name: 'Change monitoring settings' }));
  await user.click(screen.getByRole('button', { name: 'Save monitoring revision' }));
  await screen.findByRole('button', { name: 'Retry same request' });
  visible = { ...visible, checkpoint: { ...checkpoint, version: currentVersion } };
  await user.click(screen.getByRole('button', { name: 'Refresh monitoring' }));
  await screen.findByText(/version 9,007,199,254,740,995/);
  await user.click(screen.getByRole('button', { name: 'Retry same request' }));
  expect(await screen.findByText(/Monitoring policy revision recorded/)).toBeVisible();
  expect(screen.queryByRole('button', { name: 'Retry same request' })).toBeNull();
  expect(posts).toHaveLength(2);
  expect(posts[1]).toEqual(posts[0]);
  expect(posts[0].key).toBe(JSON.parse(posts[0].body).idempotency_key);
  hasExactToken(posts[0].body, 'expected_version', original);
  hasExactToken(posts[0].body, 'revision', requestedRevision);
  hasExactToken(posts[0].body, 'hard_token_cap', '9223372036854775807');
  hasExactToken(posts[0].body, 'manual_estimate_tokens', '9007199254740992');
});

test('clears high-version 409 binding and submits a fresh exact request/key', async () => {
  const first = '9007199254740992';
  const current = '9007199254740993';
  const resultVersion = '9007199254740994';
  let visible = { ...response, checkpoint: { ...checkpoint, version: first } };
  const posts: { body: string; key: string | null }[] = [];
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (_input, init) => {
    if (init?.method === 'POST') {
      posts.push({ body: String(init.body), key: new Headers(init.headers).get('Idempotency-Key') });
      if (posts.length === 1) return new Response('{"detail":"stale-project-policy"}', { status: 409 });
      return new Response(exactOwnerJson({ receipt: { checkpoint_id: checkpoint.checkpoint_id,
        action: 'continue', state: 'resumed', result_version: resultVersion },
        checkpoint: { ...checkpoint, state: 'resumed', version: resultVersion }, report }), { status: 200 });
    }
    return new Response(exactOwnerJson(visible), { status: 200 });
  });
  const user = userEvent.setup();
  render(<RunUsageCheckpoint runId="run-1" runAllowsExecution runIsTerminal={false} quotaBlocked={false} manualPause={false} />);
  await user.click(await screen.findByRole('button', { name: 'Continue without usage pauses' }));
  expect(await screen.findByText(/checkpoint changed/i)).toBeVisible();
  expect(screen.queryByRole('button', { name: 'Retry same request' })).toBeNull();
  visible = { ...visible, checkpoint: { ...checkpoint, version: current } };
  await user.click(screen.getByRole('button', { name: 'Refresh monitoring' }));
  await screen.findByText(/version 9,007,199,254,740,993/);
  await user.click(screen.getByRole('button', { name: 'Continue without usage pauses' }));
  expect(await screen.findByText(/Continue recorded/)).toBeVisible();
  expect(posts).toHaveLength(2);
  expect(posts[0].key).not.toBe(posts[1].key);
  hasExactToken(posts[0].body, 'expected_version', first);
  hasExactToken(posts[1].body, 'expected_version', current);
  expect(posts[1].key).toBe(JSON.parse(posts[1].body).idempotency_key);
});

test('sends high next-checkpoint mode and accepts exact result version', async () => {
  const expected = '9007199254740992';
  const resultVersion = '9007199254740993';
  const posts: string[] = [];
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (_input, init) => {
    if (init?.method === 'POST') {
      posts.push(String(init.body));
      return new Response(exactOwnerJson({ receipt: { checkpoint_id: checkpoint.checkpoint_id,
        action: 'continue', state: 'resumed', result_version: resultVersion },
        checkpoint: { ...checkpoint, state: 'resumed', version: resultVersion }, report }), { status: 200 });
    }
    return new Response(exactOwnerJson({ ...response, checkpoint: { ...checkpoint, version: expected } }), { status: 200 });
  });
  render(<RunUsageCheckpoint runId="run-1" runAllowsExecution runIsTerminal={false} quotaBlocked={false} manualPause={false} />);
  await userEvent.setup().click(await screen.findByRole('button', { name: 'Continue to next checkpoint' }));
  expect(await screen.findByText(/Continue recorded/)).toBeVisible();
  expect(posts).toHaveLength(1);
  expect(JSON.parse(posts[0]).continue_mode).toBe('next_checkpoint');
  hasExactToken(posts[0], 'expected_version', expected);
});

test('keeps exact MAX measured usage visible but refuses a next threshold above signed64', async () => {
  const fetcher = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(exactOwnerJson({ ...response,
    report: { ...report, input_tokens: '9223372036854775807', output_tokens: 0,
      baseline: { ...report.baseline, reference_tokens: null } } }), { status: 200 }));
  render(<RunUsageCheckpoint runId="run-1" runAllowsExecution runIsTerminal={false} quotaBlocked={false} manualPause={false} />);
  const region = await screen.findByRole('region', { name: 'Run usage checkpoint' });
  expect(region).toHaveTextContent('9,223,372,036,854,775,807');
  expect(within(region).getByRole('button', { name: 'Continue to next checkpoint' })).toBeDisabled();
  expect(within(region).getByRole('button', { name: 'Continue without usage pauses' })).toBeEnabled();
  expect(region).toHaveTextContent(/next checkpoint.*signed 64-bit/i);
  expect(fetcher.mock.calls.filter(([, init]) => init?.method === 'POST')).toHaveLength(0);
});

test.each([
  ['9223372036854775806', 0, null, 'complete', true],
  ['9223372036854775787', 0, 20, 'complete', true],
  ['9223372036854775788', 0, 20, 'complete', false],
  ['9223372036854775805', 0, 1.1, 'complete', true],
  ['9223372036854775806', 0, 1.1, 'complete', false],
  ['9223372036854775806', null, null, 'lower_bound', true],
] as const)('run next headroom combines %s input, %s output, frozen %s, %s: eligible %s',
  async (input, output, reference, completeness, eligible) => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(exactOwnerJson({ ...response,
      report: { ...report, input_tokens: input, output_tokens: output, completeness,
        baseline: { ...report.baseline, reference_tokens: reference } } }), { status: 200 }));
    render(<RunUsageCheckpoint runId="run-1" runAllowsExecution runIsTerminal={false} quotaBlocked={false} manualPause={false} />);
    const region = await screen.findByRole('region', { name: 'Run usage checkpoint' });
    const next = within(region).getByRole('button', { name: 'Continue to next checkpoint' });
    if (eligible) expect(next).toBeEnabled(); else expect(next).toBeDisabled();
    expect(within(region).getByRole('button', { name: 'Continue without usage pauses' })).toBeEnabled();
  });

test('continues from an exact high checkpoint version and accepts its numeric durable receipt', async () => {
  const high = '9007199254740993';
  const next = '9007199254740994';
  const current = JSON.stringify({ ...response, checkpoint: { ...checkpoint, version: high } }).replace(`"version":"${high}"`, `"version":${high}`);
  const completed = JSON.stringify({ receipt: { checkpoint_id: checkpoint.checkpoint_id, action: 'continue', state: 'resumed', result_version: next },
    checkpoint: { ...checkpoint, state: 'resumed', version: next }, report }).replace(`"result_version":"${next}"`, `"result_version":${next}`).replace(`"version":"${next}"`, `"version":${next}`);
  let sent = '';
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (_input, init) => {
    if ((init?.method ?? 'GET') === 'POST') { sent = String(init?.body); return new Response(completed, { status: 200 }); }
    return new Response(current, { status: 200 });
  });
  render(<RunUsageCheckpoint runId="run-1" runAllowsExecution runIsTerminal={false} quotaBlocked={false} manualPause={false} />);
  await userEvent.setup().click(await screen.findByRole('button', { name: 'Continue without usage pauses' }));
  hasExactToken(sent, 'expected_version', high);
  expect(await screen.findByText(/Continue recorded/)).toBeVisible();
  expect(screen.queryByRole('button', { name: 'Retry same request' })).toBeNull();
});

test('disables owner actions when checkpoint version cannot increment', async () => {
  vi.spyOn(globalThis, 'fetch').mockImplementation(async () => new Response(JSON.stringify({ ...response,
    checkpoint: { ...checkpoint, version: '9223372036854775807' } }), { status: 200 }));
  render(<RunUsageCheckpoint runId="run-1" runAllowsExecution runIsTerminal={false} quotaBlocked={false} manualPause={false} />);
  expect(await screen.findByRole('button', { name: 'Continue without usage pauses' })).toBeDisabled();
  expect(screen.getByRole('button', { name: 'Stop run' })).toBeDisabled();
  expect(screen.getByText(/version cannot be incremented/i)).toBeInTheDocument();
});

test('explains a monitoring revision at the terminal signed64 version', async () => {
  vi.spyOn(globalThis, 'fetch').mockImplementation(async input => new Response(JSON.stringify(
    String(input).endsWith('/monitoring-policy') ? { ...retainedPolicy, revision: '9223372036854775807' } : response
  ), { status: 200 }));
  render(<RunUsageCheckpoint runId="run-1" runAllowsExecution runIsTerminal={false} quotaBlocked={false} manualPause={false} />);
  await userEvent.setup().click(await screen.findByRole('button', { name: 'Change monitoring settings' }));
  expect(await screen.findByText(/Monitoring revision cannot be incremented/i)).toBeVisible();
  expect(screen.getByRole('button', { name: 'Save monitoring revision' })).toBeDisabled();
});

test('renders settled checkpoint and honest usage, baseline and unknown context', async () => {
  const unknownSource = { binding, phase: 'implementation', role: 'primary', delegated: null,
    input_tokens: 0, output_tokens: 20, context_occupied_tokens: null, context_peak_tokens: null,
    context_capacity_tokens: null, compaction_count: null, completeness: 'complete' };
  vi.spyOn(globalThis, 'fetch').mockImplementation(async () => new Response(JSON.stringify({
    ...response, report: { ...report, sources: [unknownSource] },
  }), { status: 200 }));
  render(<RunUsageCheckpoint runId="run-1" runAllowsExecution runIsTerminal={false} quotaBlocked={false} manualPause={false} />);
  const region = await screen.findByRole('region', { name: 'Run usage checkpoint' });
  expect(region).toHaveTextContent('Paused');
  expect(region).toHaveTextContent('Input tokens: 0');
  expect(region).toHaveTextContent('Output tokens: 20');
  expect(region).toHaveTextContent('Measurement: Complete');
  expect(region).toHaveTextContent('Context occupancy: Unknown');
  expect(region).toHaveTextContent('Peak occupancy: Unknown');
  expect(region).toHaveTextContent('Learned reference: 100 tokens');
  expect(region).toHaveTextContent('Checkpoint threshold: 200 tokens');
  expect(region).toHaveTextContent('At the warning threshold');
  expect(region).toHaveTextContent('implementation · primary · ancestry unknown');
  expect(within(region).getByRole('button', { name: 'Continue to next checkpoint' })).toBeEnabled();
});

test('allows known lower bounds during quota waits but keeps continuation behind independent execution blockers', async () => {
  const incomplete = { ...response, report: { ...report, completeness: 'lower_bound' } };
  vi.spyOn(globalThis, 'fetch').mockImplementation(async () => new Response(JSON.stringify(incomplete), { status: 200 }));
  const { rerender } = render(<RunUsageCheckpoint runId="run-1" runAllowsExecution runIsTerminal={false} quotaBlocked={false} manualPause={false} />);
  const next = await screen.findByRole('button', { name: 'Continue to next checkpoint' });
  expect(next).toBeEnabled();
  expect(screen.getByRole('button', { name: 'Continue without usage pauses' })).toBeEnabled();
  rerender(<RunUsageCheckpoint runId="run-1" runAllowsExecution runIsTerminal={false} quotaBlocked manualPause={false} />);
  expect(screen.getByRole('button', { name: 'Continue to next checkpoint' })).toBeEnabled();
  expect(screen.getByRole('button', { name: 'Continue without usage pauses' })).toBeEnabled();
  expect(screen.getByText(/waiting for provider quota eligibility/i)).toBeInTheDocument();
  rerender(<RunUsageCheckpoint runId="run-1" runAllowsExecution runIsTerminal={false} quotaBlocked={false} manualPause />);
  expect(screen.getByRole('button', { name: 'Continue without usage pauses' })).toBeDisabled();
  rerender(<RunUsageCheckpoint runId="run-1" runAllowsExecution={false} runIsTerminal={false} quotaBlocked={false} manualPause={false} />);
  expect(screen.getByRole('button', { name: 'Continue without usage pauses' })).toBeDisabled();
  rerender(<RunUsageCheckpoint runId="run-1" runAllowsExecution runIsTerminal quotaBlocked={false} manualPause={false} />);
  expect(screen.getByRole('button', { name: 'Continue to next checkpoint' })).toBeDisabled();
});

test('does not claim a requested checkpoint is paused or allow continuation before process settlement', async () => {
  const requested = { checkpoint: { ...checkpoint, state: 'pausing' as const, process_settled: false }, report };
  vi.spyOn(globalThis, 'fetch').mockImplementation(async () => new Response(JSON.stringify(requested), { status: 200 }));
  render(<RunUsageCheckpoint runId="run-1" runAllowsExecution={false} runIsTerminal={false} quotaBlocked={false} manualPause={false} />);
  const region = await screen.findByRole('region', { name: 'Run usage checkpoint' });
  expect(region).toHaveTextContent('Pausing');
  expect(region).toHaveTextContent('Process settlement pending');
  expect(within(region).queryByRole('button', { name: /Continue/ })).toBeNull();
});

test('submits a direct continue action with one idempotency key', async () => {
  const calls: { url: string; body: Record<string, unknown>; key: string | null }[] = [];
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
    if ((init?.method ?? 'GET') === 'POST') {
      calls.push({ url: String(input), body: JSON.parse(String(init?.body)) as Record<string, unknown>, key: new Headers(init?.headers).get('Idempotency-Key') });
      return new Response(JSON.stringify({ receipt: { checkpoint_id: checkpoint.checkpoint_id, action: 'continue', state: 'resumed', result_version: 3 }, checkpoint: { ...checkpoint, state: 'resumed', version: 3 }, report }), { status: 200 });
    }
    return new Response(JSON.stringify(response), { status: 200 });
  });
  const user = userEvent.setup();
  render(<RunUsageCheckpoint runId="run-1" runAllowsExecution runIsTerminal={false} quotaBlocked manualPause={false} />);
  await user.click(await screen.findByRole('button', { name: 'Continue to next checkpoint' }));
  expect(await screen.findByText(/Continue recorded\. The scheduler will wait for provider quota eligibility/)).toBeVisible();
  expect(calls).toHaveLength(1);
  expect(calls[0].url).toContain('/runs/run-1/usage-checkpoint/commands');
  expect(calls[0].body).toMatchObject({ action: 'continue', checkpoint_id: checkpoint.checkpoint_id, expected_version: 2, continue_mode: 'next_checkpoint' });
  expect(calls[0].key).toEqual(calls[0].body.idempotency_key);
});

test('retries an uncertain owner command with the identical body and idempotency key', async () => {
  const calls: { body: Record<string, unknown>; key: string | null }[] = [];
  let posts = 0;
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (_input, init) => {
    if ((init?.method ?? 'GET') === 'POST') {
      calls.push({ body: JSON.parse(String(init?.body)) as Record<string, unknown>, key: new Headers(init?.headers).get('Idempotency-Key') });
      posts += 1;
      if (posts === 1) throw new TypeError('connection lost');
      return new Response(JSON.stringify({ receipt: { checkpoint_id: checkpoint.checkpoint_id, action: 'continue', state: 'resumed', result_version: 3, replayed: true }, checkpoint: { ...checkpoint, state: 'resumed', version: 3 }, report }), { status: 200 });
    }
    return new Response(JSON.stringify(response), { status: 200 });
  });
  const user = userEvent.setup();
  render(<RunUsageCheckpoint runId="run-1" runAllowsExecution runIsTerminal={false} quotaBlocked={false} manualPause={false} />);
  await user.click(await screen.findByRole('button', { name: 'Continue to next checkpoint' }));
  await user.click(await screen.findByRole('button', { name: 'Retry same request' }));
  expect(await screen.findByText(/Continue recorded/)).toBeVisible();
  expect(calls).toHaveLength(2);
  expect(calls[1]).toEqual(calls[0]);
  expect(calls[0].body.idempotency_key).toEqual(calls[0].key);
});

test('accepts an exact replay receipt after a later action advanced the same checkpoint version', async () => {
  let posts = 0;
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (_input, init) => {
    if ((init?.method ?? 'GET') === 'POST') {
      posts += 1;
      if (posts === 1) throw new TypeError('connection lost');
      return new Response(JSON.stringify({ receipt: { checkpoint_id: checkpoint.checkpoint_id, action: 'continue', state: 'resumed', result_version: 3, replayed: true }, checkpoint: { ...checkpoint, state: 'resumed', version: 4 }, report }), { status: 200 });
    }
    return new Response(JSON.stringify(response), { status: 200 });
  });
  const user = userEvent.setup();
  render(<RunUsageCheckpoint runId="run-1" runAllowsExecution runIsTerminal={false} quotaBlocked={false} manualPause={false} />);
  await user.click(await screen.findByRole('button', { name: 'Continue to next checkpoint' }));
  await user.click(await screen.findByRole('button', { name: 'Retry same request' }));
  expect(await screen.findByText(/Continue recorded/)).toBeVisible();
});

test('allows a mixed-source numeric lower-bound crossing and submits next-checkpoint mode', async () => {
  const source = { binding, input_tokens: 20, output_tokens: 30, context_occupied_tokens: null, context_peak_tokens: null,
    context_capacity_tokens: null, compaction_count: null, completeness: 'lower_bound' };
  const partial = { ...response, report: { ...report, completeness: 'lower_bound', input_tokens: 20, output_tokens: 30,
    sources: [{ ...source, phase: 'implementation', role: 'primary', delegated: false },
      { ...source, binding: { ...binding, source: { ...binding.source, source_id: 'source-2' } }, phase: 'review', role: 'routine_implementation', delegated: true }] } };
  let command: Record<string, unknown> | null = null;
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (_input, init) => {
    if ((init?.method ?? 'GET') === 'POST') {
      command = JSON.parse(String(init?.body)) as Record<string, unknown>;
      return new Response(JSON.stringify({ receipt: { checkpoint_id: checkpoint.checkpoint_id, action: 'continue', state: 'resumed', result_version: 3 }, checkpoint: { ...checkpoint, state: 'resumed', version: 3 }, report: partial.report }), { status: 200 });
    }
    return new Response(JSON.stringify(partial), { status: 200 });
  });
  const user = userEvent.setup();
  render(<RunUsageCheckpoint runId="run-1" runAllowsExecution runIsTerminal={false} quotaBlocked={false} manualPause={false} />);
  const region = await screen.findByRole('region', { name: 'Run usage checkpoint' });
  expect(region).toHaveTextContent('Usage is a lower bound');
  expect(region).toHaveTextContent('implementation · primary');
  expect(region).toHaveTextContent('review · routine_implementation · delegated');
  await user.click(within(region).getByRole('button', { name: 'Continue to next checkpoint' }));
  await screen.findByText(/Continue recorded/);
  expect(command).toMatchObject({ action: 'continue', continue_mode: 'next_checkpoint' });
});

test('permits a lower-bound total with one unknown dimension without displaying it as zero', async () => {
  const partial = { ...response, report: { ...report, completeness: 'lower_bound', input_tokens: null, output_tokens: 30 } };
  vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify(partial), { status: 200 }));
  render(<RunUsageCheckpoint runId="run-1" runAllowsExecution runIsTerminal={false} quotaBlocked={false} manualPause={false} />);
  const region = await screen.findByRole('region', { name: 'Run usage checkpoint' });
  expect(region).toHaveTextContent('Input tokens: Unknown');
  expect(region).toHaveTextContent('Output tokens: 30');
  expect(within(region).getByRole('button', { name: 'Continue to next checkpoint' })).toBeEnabled();
});

test('keeps exact continuation disabled for wholly unknown usage or a missing frozen reference', async () => {
  const user = userEvent.setup();
  const fetcher = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({ ...response, report: { ...report, input_tokens: null, output_tokens: null, completeness: 'unknown' } }), { status: 200 }));
  const { rerender } = render(<RunUsageCheckpoint runId="run-1" runAllowsExecution runIsTerminal={false} quotaBlocked={false} manualPause={false} />);
  const region = await screen.findByRole('region', { name: 'Run usage checkpoint' });
  expect(region).toHaveTextContent('Input tokens: Unknown');
  expect(within(region).getByRole('button', { name: 'Continue to next checkpoint' })).toBeDisabled();
  expect(within(region).getByRole('button', { name: 'Continue without usage pauses' })).toBeEnabled();
  fetcher.mockImplementation(async () => new Response(JSON.stringify({ ...response, report: { ...report, baseline: null, completeness: 'lower_bound' } }), { status: 200 }));
  rerender(<RunUsageCheckpoint runId="run-1" runAllowsExecution runIsTerminal={false} quotaBlocked={false} manualPause={false} />);
  await user.click(screen.getByRole('button', { name: 'Refresh monitoring' }));
  await waitFor(() => expect(within(region).getByRole('button', { name: 'Continue to next checkpoint' })).toBeDisabled());
});

const retainedPolicy = {
  project_id: 'project-1', revision: 4, mode: 'checkpoint', warning_multiplier: 1.5, checkpoint_multiplier: 2,
  history_limit: 50, minimum_comparables: 20, window_days: 90, allow_cross_project_fallback: false,
  manual_estimate_tokens: null, hard_token_cap: null, hard_context_cap: null,
};

test.each(['manual_estimate_tokens', 'hard_token_cap', 'hard_context_cap'] as const)(
  'does not rewrite unsafe retained %s while keeping owner run actions available', async field => {
    const calls: Record<string, unknown>[] = [];
    const fetcher = vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
      if (init?.method === 'POST') {
        calls.push(JSON.parse(String(init.body)) as Record<string, unknown>);
        return new Response(JSON.stringify({ receipt: { checkpoint_id: checkpoint.checkpoint_id, action: 'set_policy', state: 'paused', result_version: 3 }, checkpoint: { ...checkpoint, version: 3 }, report }));
      }
      return new Response(JSON.stringify(String(input).includes('/monitoring-policy')
        ? { ...retainedPolicy, [field]: '9223372036854775808' } : response));
    });
    const user = userEvent.setup();
    render(<RunUsageCheckpoint runId="run-1" runAllowsExecution runIsTerminal={false} quotaBlocked={false} manualPause={false} />);
    const region = await screen.findByRole('region', { name: 'Run usage checkpoint' });
    await user.click(within(region).getByRole('button', { name: 'Change monitoring settings' }));
    expect(await screen.findByText(/Use POST \/api\/projects\/project-1\/monitoring-policy with a client that preserves signed 64-bit integers/)).toBeInTheDocument();
    expect(within(region).getByRole('button', { name: 'Save monitoring revision' })).toBeDisabled();
    expect(within(region).getByRole('button', { name: 'Continue to next checkpoint' })).toBeEnabled();
    expect(within(region).getByRole('button', { name: 'Continue without usage pauses' })).toBeEnabled();
    expect(within(region).getByRole('button', { name: 'Stop run' })).toBeEnabled();
    expect(calls).toHaveLength(0);
    expect(fetcher.mock.calls.some(([url]) => String(url).includes('/monitoring-policy'))).toBe(true);
  },
);

test('preserves safe and null retained policy integers exactly when creating a run revision', async () => {
  let submitted: Record<string, unknown> | null = null;
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
    if (init?.method === 'POST') {
      submitted = JSON.parse(String(init.body)) as Record<string, unknown>;
      return new Response(JSON.stringify({ receipt: { checkpoint_id: checkpoint.checkpoint_id, action: 'set_policy', state: 'paused', result_version: 3 }, checkpoint: { ...checkpoint, version: 3 }, report }));
    }
    return new Response(JSON.stringify(String(input).includes('/monitoring-policy')
      ? { ...retainedPolicy, manual_estimate_tokens: 0, hard_token_cap: 123456, hard_context_cap: null } : response));
  });
  const user = userEvent.setup();
  render(<RunUsageCheckpoint runId="run-1" runAllowsExecution runIsTerminal={false} quotaBlocked={false} manualPause={false} />);
  const region = await screen.findByRole('region', { name: 'Run usage checkpoint' });
  await user.click(within(region).getByRole('button', { name: 'Change monitoring settings' }));
  await user.click(within(region).getByRole('button', { name: 'Save monitoring revision' }));
  await screen.findByText('Monitoring policy revision recorded.');
  expect(submitted).toMatchObject({ action: 'set_policy', requested_policy: {
    manual_estimate_tokens: 0, hard_token_cap: 123456, hard_context_cap: null,
  } });
});

test('refreshes project policy after set_policy so two consecutive edits use fresh policy revision and checkpoint version', async () => {
  const posts: { body: Record<string, unknown>; key: string | null }[] = [];
  let currentCheckpoint = checkpoint;
  let currentPolicy = retainedPolicy;
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
    if (init?.method === 'POST') {
      const body = JSON.parse(String(init.body)) as Record<string, unknown>;
      const key = new Headers(init.headers).get('Idempotency-Key');
      posts.push({ body, key });
      const nextVersion = (body.expected_version as number) + 1;
      const requestedPolicy = body.requested_policy as typeof retainedPolicy;
      currentCheckpoint = { ...currentCheckpoint, version: nextVersion, monitoring_policy_revision: requestedPolicy.revision };
      currentPolicy = { ...requestedPolicy };
      return new Response(JSON.stringify({
        receipt: { checkpoint_id: currentCheckpoint.checkpoint_id, action: 'set_policy', state: 'paused', result_version: nextVersion },
        checkpoint: currentCheckpoint,
        report: { ...report, checkpoint_version: nextVersion, checkpoint: currentCheckpoint },
      }), { status: 200 });
    }
    return new Response(JSON.stringify(String(input).includes('/monitoring-policy')
      ? currentPolicy
      : { checkpoint: currentCheckpoint, report: { ...report, checkpoint_version: currentCheckpoint.version, checkpoint: currentCheckpoint } }), { status: 200 });
  });

  const user = userEvent.setup();
  render(<RunUsageCheckpoint runId="run-1" runAllowsExecution runIsTerminal={false} quotaBlocked={false} manualPause={false} />);
  const region = await screen.findByRole('region', { name: 'Run usage checkpoint' });

  // First edit: revision 4 -> 5, expected checkpoint version 2
  await user.click(within(region).getByRole('button', { name: 'Change monitoring settings' }));
  const warningInput = screen.getByLabelText('Warning multiplier');
  await user.clear(warningInput);
  await user.type(warningInput, '1.8');
  await user.click(within(region).getByRole('button', { name: 'Save monitoring revision' }));
  await screen.findByText('Monitoring policy revision recorded.');

  expect(posts).toHaveLength(1);
  expect(posts[0].body).toMatchObject({
    action: 'set_policy',
    expected_version: 2,
    requested_policy: expect.objectContaining({ revision: 5, warning_multiplier: 1.8 }),
  });

  // Second consecutive edit: revision 5 -> 6, expected checkpoint version 3
  await user.click(within(region).getByRole('button', { name: 'Change monitoring settings' }));
  const thresholdInput = screen.getByLabelText('Checkpoint multiplier');
  await user.clear(thresholdInput);
  await user.type(thresholdInput, '2.5');
  await user.click(within(region).getByRole('button', { name: 'Save monitoring revision' }));
  await screen.findByText('Monitoring policy revision recorded.');

  expect(posts).toHaveLength(2);
  expect(posts[1].body).toMatchObject({
    action: 'set_policy',
    expected_version: 3,
    requested_policy: expect.objectContaining({ revision: 6, checkpoint_multiplier: 2.5 }),
  });
});

test('handles external-tab policy 409: clears stale body/key, refreshes checkpoint and policy, leaves Next/bypass/Stop eligible, and allows fresh next edit', async () => {
  const posts: { body: Record<string, unknown>; key: string | null }[] = [];
  let currentCheckpoint = checkpoint;
  let currentPolicy = retainedPolicy; // initially revision 4
  let postCount = 0;

  vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
    if (init?.method === 'POST') {
      postCount += 1;
      const body = JSON.parse(String(init.body)) as Record<string, unknown>;
      const key = new Headers(init.headers).get('Idempotency-Key');
      posts.push({ body, key });
      if (postCount === 1) {
        // External write happened in background: revision is now 5
        currentPolicy = { ...retainedPolicy, revision: 5, warning_multiplier: 1.7 };
        return new Response(JSON.stringify({ detail: 'stale-project-policy' }), { status: 409 });
      }
      const nextVersion = (body.expected_version as number) + 1;
      const requestedPolicy = body.requested_policy as typeof retainedPolicy;
      currentCheckpoint = { ...currentCheckpoint, version: nextVersion, monitoring_policy_revision: requestedPolicy.revision };
      currentPolicy = { ...requestedPolicy };
      return new Response(JSON.stringify({
        receipt: { checkpoint_id: currentCheckpoint.checkpoint_id, action: 'set_policy', state: 'paused', result_version: nextVersion },
        checkpoint: currentCheckpoint,
        report: { ...report, checkpoint_version: nextVersion, checkpoint: currentCheckpoint },
      }), { status: 200 });
    }
    return new Response(JSON.stringify(String(input).includes('/monitoring-policy')
      ? currentPolicy
      : { checkpoint: currentCheckpoint, report: { ...report, checkpoint_version: currentCheckpoint.version, checkpoint: currentCheckpoint } }), { status: 200 });
  });

  const user = userEvent.setup();
  render(<RunUsageCheckpoint runId="run-1" runAllowsExecution runIsTerminal={false} quotaBlocked={false} manualPause={false} />);
  const region = await screen.findByRole('region', { name: 'Run usage checkpoint' });

  // First edit attempts revision 5 (4 + 1), but server returns 409
  await user.click(within(region).getByRole('button', { name: 'Change monitoring settings' }));
  await user.click(within(region).getByRole('button', { name: 'Save monitoring revision' }));
  await screen.findByText('The checkpoint changed. Current state has been refreshed; review it before acting again.');

  // Definite 409 must discard stale key/body: retry button must NOT be present
  expect(screen.queryByRole('button', { name: 'Retry same request' })).toBeNull();
  // Next, bypass, and Stop remain eligible after conflict
  expect(within(region).getByRole('button', { name: 'Continue to next checkpoint' })).toBeEnabled();
  expect(within(region).getByRole('button', { name: 'Continue without usage pauses' })).toBeEnabled();
  expect(within(region).getByRole('button', { name: 'Stop run' })).toBeEnabled();
  expect(within(region).getByRole('button', { name: 'Change monitoring settings' })).toBeEnabled();

  // Fresh edit uses refreshed policy revision (5 -> 6)
  await user.click(within(region).getByRole('button', { name: 'Change monitoring settings' }));
  await user.click(within(region).getByRole('button', { name: 'Save monitoring revision' }));
  await screen.findByText('Monitoring policy revision recorded.');

  expect(posts).toHaveLength(2);
  expect(posts[0].body).toMatchObject({
    action: 'set_policy',
    requested_policy: expect.objectContaining({ revision: 5 }),
  });
  expect(posts[1].body).toMatchObject({
    action: 'set_policy',
    requested_policy: expect.objectContaining({ revision: 6 }),
  });
});

test('uncertain policy edit preserves exact body and idempotency key across polling and refresh', async () => {
  const posts: { body: Record<string, unknown>; key: string | null }[] = [];
  let postCount = 0;
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
    if (init?.method === 'POST') {
      postCount += 1;
      const body = JSON.parse(String(init.body)) as Record<string, unknown>;
      const key = new Headers(init.headers).get('Idempotency-Key');
      posts.push({ body, key });
      if (postCount === 1) throw new TypeError('network error');
      return new Response(JSON.stringify({
        receipt: { checkpoint_id: checkpoint.checkpoint_id, action: 'set_policy', state: 'paused', result_version: 3, replayed: true },
        checkpoint: { ...checkpoint, version: 3 },
        report,
      }), { status: 200 });
    }
    return new Response(JSON.stringify(String(input).includes('/monitoring-policy')
      ? retainedPolicy
      : response), { status: 200 });
  });

  const user = userEvent.setup();
  render(<RunUsageCheckpoint runId="run-1" runAllowsExecution runIsTerminal={false} quotaBlocked={false} manualPause={false} />);
  const region = await screen.findByRole('region', { name: 'Run usage checkpoint' });

  await user.click(within(region).getByRole('button', { name: 'Change monitoring settings' }));
  const warningInput = screen.getByLabelText('Warning multiplier');
  await user.clear(warningInput);
  await user.type(warningInput, '1.9');
  await user.click(within(region).getByRole('button', { name: 'Save monitoring revision' }));

  await screen.findByText('The command could not be confirmed. Retry the same request to retrieve its durable receipt.');
  expect(screen.getByRole('button', { name: 'Retry same request' })).toBeVisible();

  // Polling / manual refresh must not overwrite the pending retry body or key
  await user.click(screen.getByRole('button', { name: 'Refresh monitoring' }));
  expect(screen.getByRole('button', { name: 'Retry same request' })).toBeVisible();

  // Retry submits the identical body and key
  await user.click(screen.getByRole('button', { name: 'Retry same request' }));
  await screen.findByText('Monitoring policy revision recorded.');

  expect(posts).toHaveLength(2);
  expect(posts[1]).toEqual(posts[0]);
  expect(posts[0].key).toBeTruthy();
  expect(posts[0].body).toMatchObject({
    action: 'set_policy',
    requested_policy: expect.objectContaining({ revision: 5, warning_multiplier: 1.9 }),
  });
});

test('run remount cleans up pending uncertain retry state', async () => {
  let postCount = 0;
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
    if (init?.method === 'POST') {
      postCount += 1;
      throw new TypeError('network error');
    }
    return new Response(JSON.stringify(String(input).includes('/monitoring-policy')
      ? retainedPolicy
      : response), { status: 200 });
  });

  const user = userEvent.setup();
  const panel = (runId: string) => <RunUsageCheckpoint key={`run:${runId}`} runId={runId} runAllowsExecution runIsTerminal={false} quotaBlocked={false} manualPause={false} />;
  const { rerender } = render(panel('run-1'));
  const region = await screen.findByRole('region', { name: 'Run usage checkpoint' });

  await user.click(within(region).getByRole('button', { name: 'Change monitoring settings' }));
  await user.click(within(region).getByRole('button', { name: 'Save monitoring revision' }));
  expect(await screen.findByRole('button', { name: 'Retry same request' })).toBeVisible();

  rerender(panel('run-2'));
  await screen.findByRole('region', { name: 'Run usage checkpoint' });
  expect(screen.queryByRole('button', { name: 'Retry same request' })).toBeNull();
  expect(postCount).toBe(1);
});

test('enables continue to next checkpoint with exact-string token dimensions > MAX_SAFE', async () => {
  const largeReport = {
    ...response,
    report: {
      ...report,
      input_tokens: '9007199254740992',
      output_tokens: '9007199254740993',
      completeness: 'complete',
    },
  };
  vi.spyOn(globalThis, 'fetch').mockImplementation(async () => new Response(JSON.stringify(largeReport), { status: 200 }));
  render(<RunUsageCheckpoint runId="run-1" runAllowsExecution runIsTerminal={false} quotaBlocked={false} manualPause={false} />);
  const region = await screen.findByRole('region', { name: 'Run usage checkpoint' });
  expect(within(region).getByRole('button', { name: 'Continue to next checkpoint' })).toBeEnabled();
});

test('disables continue to next checkpoint when two dimensions exceed int64 boundary sum', async () => {
  const overflowReport = {
    ...response,
    report: {
      ...report,
      input_tokens: '9223372036854775800',
      output_tokens: '100',
      completeness: 'complete',
    },
  };
  vi.spyOn(globalThis, 'fetch').mockImplementation(async () => new Response(JSON.stringify(overflowReport), { status: 200 }));
  render(<RunUsageCheckpoint runId="run-1" runAllowsExecution runIsTerminal={false} quotaBlocked={false} manualPause={false} />);
  const region = await screen.findByRole('region', { name: 'Run usage checkpoint' });
  expect(within(region).getByRole('button', { name: 'Continue to next checkpoint' })).toBeDisabled();
});

test('preserves exact-string retained policy caps and allows saving revision without precision-loss alert', async () => {
  let submitted: Record<string, unknown> | null = null;
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
    if (init?.method === 'POST') {
      submitted = parseExactJson(String(init.body)) as Record<string, unknown>;
      return new Response(JSON.stringify({
        receipt: { checkpoint_id: checkpoint.checkpoint_id, action: 'set_policy', state: 'paused', result_version: 3 },
        checkpoint: { ...checkpoint, version: 3 },
        report,
      }));
    }
    return new Response(JSON.stringify(String(input).includes('/monitoring-policy')
      ? {
        ...retainedPolicy,
        manual_estimate_tokens: '9007199254740992',
        hard_token_cap: '9223372036854775807',
        hard_context_cap: null,
      }
      : response));
  });
  const user = userEvent.setup();
  render(<RunUsageCheckpoint runId="run-1" runAllowsExecution runIsTerminal={false} quotaBlocked={false} manualPause={false} />);
  const region = await screen.findByRole('region', { name: 'Run usage checkpoint' });
  await user.click(within(region).getByRole('button', { name: 'Change monitoring settings' }));
  expect(screen.queryByText(/exceeds safe browser integer precision/)).toBeNull();
  const saveBtn = within(region).getByRole('button', { name: 'Save monitoring revision' });
  expect(saveBtn).toBeEnabled();
  await user.click(saveBtn);
  await screen.findByText('Monitoring policy revision recorded.');
  expect(submitted).toMatchObject({
    action: 'set_policy',
    requested_policy: {
      manual_estimate_tokens: '9007199254740992',
      hard_token_cap: '9223372036854775807',
      hard_context_cap: null,
    },
  });
});
