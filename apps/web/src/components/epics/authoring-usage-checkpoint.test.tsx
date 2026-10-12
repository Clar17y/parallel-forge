import { afterEach, expect, test, vi } from 'vitest';
import { cleanup, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { AuthoringUsageCheckpoint } from './authoring-usage-checkpoint';
import type { components } from '@/lib/api/schema';
import { parseExactJson } from '@/lib/api/exact-integer';

const binding: components['schemas']['WorkUnitBinding'] = {
  subject: { project_id: 'project-1', kind: 'authoring_job', subject_id: 'job-1', schema_version: 1 },
  work_unit_id: 'unit-1', admitted_phase_id: 'brainstorm', configured_route: { fingerprint: 'a'.repeat(64), members: [] },
  effective_route: { fingerprint: 'b'.repeat(64), members: [] }, source: { kind: 'agent_execution', source_id: 'source-1', parent_id: null }, schema_version: 1,
};
const checkpoint: components['schemas']['CheckpointRecord'] = {
  checkpoint_id: 'checkpoint-1', binding, baseline_binding: binding, state: 'paused', policy_revision: 1,
  reference_digest: 'c'.repeat(64), trigger_event_id: 'trigger-1', version: 2, generation: 1,
  process_settled: true, next_checkpoint_threshold_tokens: null, usage_pauses_disabled: false, monitoring_policy_revision: 1,
};
const baseline: components['schemas']['BaselineReference'] = {
  binding, policy_revision: 1, snapshot_digest: 'd'.repeat(64), cohort_digest: 'e'.repeat(64), history_digest: 'f'.repeat(64),
  estimator_version: 'v1', comparison_scope: 'route', completeness: 'complete', coverage_count: 10, sample_count: 10,
  outcome_count: 10, source_count: 10, window_limit: 50, minimum_comparables: 5, status: 'ready', reference_tokens: 100,
  mean_tokens: 100, median_tokens: 100, p90_tokens: 150, warning_threshold_tokens: 150, checkpoint_threshold_tokens: 200,
  frozen_at: '2026-10-10T12:00:00Z', schema_version: 1,
};
const report: components['schemas']['UsageReport'] = {
  binding, policy_revision: 1, baseline, input_tokens: 25, output_tokens: 30, cached_input_tokens: null,
  reasoning_output_tokens: null, context_occupied_tokens: null, context_capacity_tokens: null, compaction_count: null,
  completeness: 'complete', freshness: 'final', context_peak_tokens: null, sources: [{ binding, phase: 'brainstorm', role: 'primary', delegated: null,
    input_tokens: 25, output_tokens: 30, context_occupied_tokens: null, context_peak_tokens: null, context_capacity_tokens: null, compaction_count: null, completeness: 'complete' }],
};
const response: components['schemas']['TaskUsageCheckpointResponse'] = { checkpoint, report };
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

test('shows authoring report and posts direct commands to the correct scoped route', async () => {
  const calls: { url: string; body: Record<string, unknown> }[] = [];
  const reads: string[] = [];
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
    if ((init?.method ?? 'GET') === 'POST') {
      calls.push({ url: String(input), body: JSON.parse(String(init?.body)) as Record<string, unknown> });
      return new Response(JSON.stringify({ receipt: { checkpoint_id: checkpoint.checkpoint_id, action: 'continue', state: 'resumed', result_version: 3 }, checkpoint: { ...checkpoint, state: 'resumed', version: 3 }, report }), { status: 200 });
    }
    reads.push(String(input));
    return new Response(JSON.stringify(response), { status: 200 });
  });
  const user = userEvent.setup();
  render(<AuthoringUsageCheckpoint epicId="epic-1" projectId="project-1" jobId="job-1" kind="brainstorm" locked={false} />);
  const region = await screen.findByRole('region', { name: 'Authoring usage checkpoint' });
  expect(region).toHaveTextContent('Input 25 · output 30');
  expect(region).toHaveTextContent('peak occupancy Unknown');
  expect(region).toHaveTextContent('brainstorm · primary · ancestry unknown');
  expect(reads).toEqual(expect.arrayContaining([expect.stringContaining('project_id=project-1')]));
  await user.click(within(region).getByRole('button', { name: 'Continue to next checkpoint' }));
  expect(await screen.findByText(/Owner command recorded/)).toBeVisible();
  expect(calls).toHaveLength(1);
  expect(calls[0].url).toContain('/epics/epic-1/brainstorm-jobs/job-1/usage-checkpoint/commands');
  expect(calls[0].body).toMatchObject({ action: 'continue', continue_mode: 'next_checkpoint', checkpoint_id: checkpoint.checkpoint_id });
});

test('does not allow exact continuation with an unsafe or incomplete report', async () => {
  const fetcher = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({ ...response, report: { ...report, input_tokens: 'invalid-tokens', completeness: 'lower_bound' } }), { status: 200 }));
  render(<AuthoringUsageCheckpoint epicId="epic-1" projectId="project-1" jobId="job-1" kind="decomposition" locked={false} />);
  const region = await screen.findByRole('region', { name: 'Authoring usage checkpoint' });
  expect(within(region).getByRole('button', { name: 'Continue to next checkpoint' })).toBeDisabled();
  expect(within(region).getByRole('button', { name: 'Continue without usage pauses' })).toBeEnabled();
  expect(fetcher.mock.calls.some(([url]) => String(url).includes('/epics/epic-1/decomposition-jobs/job-1/usage-checkpoint'))).toBe(true);
});

test('submits next-checkpoint for a mixed-source measured lower bound', async () => {
  const source = report.sources[0];
  const partial = { ...response, report: { ...report, completeness: 'lower_bound', input_tokens: 20, output_tokens: 30,
    sources: [{ ...source, phase: 'brainstorm', role: 'primary', delegated: false },
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
  render(<AuthoringUsageCheckpoint epicId="epic-1" projectId="project-1" jobId="job-1" kind="brainstorm" locked={false} />);
  const region = await screen.findByRole('region', { name: 'Authoring usage checkpoint' });
  expect(region).toHaveTextContent('Lower bound');
  expect(region).toHaveTextContent('review · routine_implementation · delegated');
  await user.click(within(region).getByRole('button', { name: 'Continue to next checkpoint' }));
  await screen.findByText(/Owner command recorded/);
  expect(command).toMatchObject({ action: 'continue', continue_mode: 'next_checkpoint' });
});

test('uses known lower-bound dimensions while keeping the other dimension unknown', async () => {
  vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({ ...response, report: { ...report, completeness: 'lower_bound', input_tokens: null, output_tokens: 30 } }), { status: 200 }));
  render(<AuthoringUsageCheckpoint epicId="epic-1" projectId="project-1" jobId="job-1" kind="brainstorm" locked={false} />);
  const region = await screen.findByRole('region', { name: 'Authoring usage checkpoint' });
  expect(region).toHaveTextContent('Input Unknown · output 30');
  expect(within(region).getByRole('button', { name: 'Continue to next checkpoint' })).toBeEnabled();
});

test('requires known measured usage and a frozen reference, and keeps independent locks effective', async () => {
  const user = userEvent.setup();
  let current: components['schemas']['TaskUsageCheckpointResponse'] = { ...response, report: { ...report, input_tokens: null, output_tokens: null, completeness: 'unknown' } };
  vi.spyOn(globalThis, 'fetch').mockImplementation(async () => new Response(JSON.stringify(current), { status: 200 }));
  const { rerender } = render(<AuthoringUsageCheckpoint epicId="epic-1" projectId="project-1" jobId="job-1" kind="brainstorm" locked={false} />);
  const region = await screen.findByRole('region', { name: 'Authoring usage checkpoint' });
  expect(within(region).getByRole('button', { name: 'Continue to next checkpoint' })).toBeDisabled();
  expect(within(region).getByRole('button', { name: 'Continue without usage pauses' })).toBeEnabled();
  current = { ...response, report: { ...report, completeness: 'lower_bound', baseline: null } };
  await user.click(within(region).getByRole('button', { name: 'Refresh authoring usage' }));
  expect(await screen.findByText(/Frozen reference: Unknown/)).toBeVisible();
  expect(within(region).getByRole('button', { name: 'Continue to next checkpoint' })).toBeDisabled();
  rerender(<AuthoringUsageCheckpoint epicId="epic-1" projectId="project-1" jobId="job-1" kind="brainstorm" locked />);
  expect(within(region).getByRole('button', { name: 'Continue without usage pauses' })).toBeDisabled();
});

test('retains an exact request for its subject while visible checkpoint advances to a new generation', async () => {
  const secondGeneration = { ...checkpoint, checkpoint_id: 'checkpoint-2', generation: 2, version: 1 };
  let current = response;
  const posts: { body: string; key: string | null }[] = [];
  let postCount = 0;
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (_input, init) => {
    if ((init?.method ?? 'GET') === 'POST') {
      posts.push({ body: String(init?.body), key: new Headers(init?.headers).get('Idempotency-Key') });
      postCount += 1;
      if (postCount === 1) throw new TypeError('connection lost');
      return new Response(JSON.stringify({ receipt: { checkpoint_id: checkpoint.checkpoint_id, action: 'continue', state: 'resumed', result_version: 3, replayed: true }, checkpoint: { ...checkpoint, state: 'resumed', version: 4 }, report }), { status: 200 });
    }
    return new Response(JSON.stringify(current), { status: 200 });
  });
  const user = userEvent.setup();
  render(<AuthoringUsageCheckpoint epicId="epic-1" projectId="project-1" jobId="job-1" kind="brainstorm" locked={false} />);
  const region = await screen.findByRole('region', { name: 'Authoring usage checkpoint' });
  await user.click(within(region).getByRole('button', { name: 'Continue without usage pauses' }));
  await screen.findByRole('button', { name: 'Retry same request' });
  current = { checkpoint: secondGeneration, report };
  await user.click(await screen.findByRole('button', { name: 'Refresh authoring usage' }));
  await screen.findByText(/generation 2/);
  await user.click(screen.getByRole('button', { name: 'Retry same request' }));
  expect(await screen.findByText(/Owner command recorded/)).toBeVisible();
  expect(posts).toHaveLength(2);
  expect(posts[1]).toEqual(posts[0]);
});

test('a job switch clears and aborts the previous subject retry state', async () => {
  let posts = 0;
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (_input, init) => {
    if ((init?.method ?? 'GET') === 'POST') { posts += 1; throw new TypeError('connection lost'); }
    return new Response(JSON.stringify(response), { status: 200 });
  });
  const user = userEvent.setup();
  const panel = (jobId: string) => <AuthoringUsageCheckpoint key={`brainstorm:${jobId}`} epicId="epic-1" projectId="project-1" jobId={jobId} kind="brainstorm" locked={false} />;
  const { rerender } = render(panel('job-1'));
  await user.click(await screen.findByRole('button', { name: 'Continue without usage pauses' }));
  expect(await screen.findByRole('button', { name: 'Retry same request' })).toBeVisible();
  rerender(panel('job-2'));
  await screen.findByRole('region', { name: 'Authoring usage checkpoint' });
  expect(screen.queryByRole('button', { name: 'Retry same request' })).toBeNull();
  expect(posts).toBe(1);
});

const retainedPolicy: components['schemas']['MonitoringPolicyResponse'] = {
  project_id: 'project-1', revision: 1, mode: 'checkpoint', warning_multiplier: 1.5, checkpoint_multiplier: 2,
  history_limit: 50, minimum_comparables: 20, window_days: 90, allow_cross_project_fallback: false,
  manual_estimate_tokens: null, hard_token_cap: null, hard_context_cap: null,
};

test.each(['manual_estimate_tokens', 'hard_token_cap', 'hard_context_cap'] as const)(
  'does not rewrite unsafe retained %s while keeping authoring job actions available', async field => {
    const calls: Record<string, unknown>[] = [];
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
      if (init?.method === 'POST') {
        calls.push(JSON.parse(String(init.body)) as Record<string, unknown>);
        return new Response(JSON.stringify({ receipt: { checkpoint_id: checkpoint.checkpoint_id, action: 'set_policy', state: 'paused', result_version: 3 }, checkpoint: { ...checkpoint, version: 3 }, report }));
      }
      return new Response(JSON.stringify(String(input).includes('/monitoring-policy')
        ? { ...retainedPolicy, [field]: '9223372036854775808' } : response));
    });
    const user = userEvent.setup();
    render(<AuthoringUsageCheckpoint epicId="epic-1" projectId="project-1" jobId="job-1" kind="brainstorm" locked={false} />);
    const region = await screen.findByRole('region', { name: 'Authoring usage checkpoint' });
    await user.click(within(region).getByRole('button', { name: 'Change monitoring settings' }));
    expect(await screen.findByText(/Use POST \/api\/projects\/project-1\/monitoring-policy with a client that preserves signed 64-bit integers/)).toBeInTheDocument();
    expect(within(region).getByRole('button', { name: 'Save monitoring revision' })).toBeDisabled();
    expect(within(region).getByRole('button', { name: 'Continue to next checkpoint' })).toBeEnabled();
    expect(within(region).getByRole('button', { name: 'Continue without usage pauses' })).toBeEnabled();
    expect(within(region).getByRole('button', { name: 'Stop authoring job' })).toBeEnabled();
    expect(calls).toHaveLength(0);
  },
);

test('preserves safe and null retained policy integers exactly in an authoring revision', async () => {
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
  render(<AuthoringUsageCheckpoint epicId="epic-1" projectId="project-1" jobId="job-1" kind="brainstorm" locked={false} />);
  const region = await screen.findByRole('region', { name: 'Authoring usage checkpoint' });
  await user.click(within(region).getByRole('button', { name: 'Change monitoring settings' }));
  await user.click(within(region).getByRole('button', { name: 'Save monitoring revision' }));
  await screen.findByText(/Owner command recorded/);
  expect(submitted).toMatchObject({ action: 'set_policy', requested_policy: {
    manual_estimate_tokens: 0, hard_token_cap: 123456, hard_context_cap: null,
  } });
});

test('refreshes project policy after set_policy so two consecutive edits use fresh policy revision on brainstorm route', async () => {
  const posts: { url: string; body: Record<string, unknown> }[] = [];
  let currentCheckpoint = checkpoint;
  let currentPolicy = retainedPolicy;
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
    if (init?.method === 'POST') {
      const body = JSON.parse(String(init.body)) as Record<string, unknown>;
      posts.push({ url: String(input), body });
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
  render(<AuthoringUsageCheckpoint epicId="epic-1" projectId="project-1" jobId="job-1" kind="brainstorm" locked={false} />);
  const region = await screen.findByRole('region', { name: 'Authoring usage checkpoint' });

  // First edit: revision 1 -> 2
  await user.click(within(region).getByRole('button', { name: 'Change monitoring settings' }));
  const warningInput = screen.getByLabelText('Warning multiplier');
  await user.clear(warningInput);
  await user.type(warningInput, '1.6');
  await user.click(within(region).getByRole('button', { name: 'Save monitoring revision' }));
  await screen.findByText(/Owner command recorded/);

  expect(posts).toHaveLength(1);
  expect(posts[0].url).toContain('/epics/epic-1/brainstorm-jobs/job-1/usage-checkpoint/commands');
  expect(posts[0].body).toMatchObject({
    action: 'set_policy',
    expected_version: 2,
    requested_policy: expect.objectContaining({ revision: 2, warning_multiplier: 1.6 }),
  });

  // Second consecutive edit: revision 2 -> 3
  await user.click(within(region).getByRole('button', { name: 'Change monitoring settings' }));
  const thresholdInput = screen.getByLabelText('Checkpoint multiplier');
  await user.clear(thresholdInput);
  await user.type(thresholdInput, '2.4');
  await user.click(within(region).getByRole('button', { name: 'Save monitoring revision' }));
  await screen.findByText(/Owner command recorded/);

  expect(posts).toHaveLength(2);
  expect(posts[1].url).toContain('/epics/epic-1/brainstorm-jobs/job-1/usage-checkpoint/commands');
  expect(posts[1].body).toMatchObject({
    action: 'set_policy',
    expected_version: 3,
    requested_policy: expect.objectContaining({ revision: 3, checkpoint_multiplier: 2.4 }),
  });
});

test('supports consecutive edits on decomposition route where checkpoint version and policy revision differ', async () => {
  const diffCheckpoint = { ...checkpoint, version: 7, policy_revision: 1 };
  let currentCheckpoint = diffCheckpoint;
  let currentPolicy = { ...retainedPolicy, revision: 2 }; // checkpoint version 7 != policy revision 2
  const posts: { url: string; body: Record<string, unknown> }[] = [];

  vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
    if (init?.method === 'POST') {
      const body = JSON.parse(String(init.body)) as Record<string, unknown>;
      posts.push({ url: String(input), body });
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
  render(<AuthoringUsageCheckpoint epicId="epic-1" projectId="project-1" jobId="job-2" kind="decomposition" locked={false} />);
  const region = await screen.findByRole('region', { name: 'Authoring usage checkpoint' });

  // First edit: expected checkpoint version 7, requested policy revision 3 (2 + 1)
  await user.click(within(region).getByRole('button', { name: 'Change monitoring settings' }));
  await user.click(within(region).getByRole('button', { name: 'Save monitoring revision' }));
  await screen.findByText(/Owner command recorded/);

  expect(posts).toHaveLength(1);
  expect(posts[0].url).toContain('/epics/epic-1/decomposition-jobs/job-2/usage-checkpoint/commands');
  expect(posts[0].body).toMatchObject({
    action: 'set_policy',
    expected_version: 7,
    requested_policy: expect.objectContaining({ revision: 3 }),
  });

  // Second edit: expected checkpoint version 8, requested policy revision 4 (3 + 1)
  await user.click(within(region).getByRole('button', { name: 'Change monitoring settings' }));
  await user.click(within(region).getByRole('button', { name: 'Save monitoring revision' }));
  await screen.findByText(/Owner command recorded/);

  expect(posts).toHaveLength(2);
  expect(posts[1].url).toContain('/epics/epic-1/decomposition-jobs/job-2/usage-checkpoint/commands');
  expect(posts[1].body).toMatchObject({
    action: 'set_policy',
    expected_version: 8,
    requested_policy: expect.objectContaining({ revision: 4 }),
  });
});

test('handles external authoring policy 409: clears stale retry, refreshes policy, leaves action buttons eligible, and allows fresh next edit', async () => {
  const posts: { body: Record<string, unknown> }[] = [];
  let currentCheckpoint = checkpoint;
  let currentPolicy = retainedPolicy; // revision 1
  let postCount = 0;

  vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
    if (init?.method === 'POST') {
      postCount += 1;
      const body = JSON.parse(String(init.body)) as Record<string, unknown>;
      posts.push({ body });
      if (postCount === 1) {
        // External write changed policy to revision 2
        currentPolicy = { ...retainedPolicy, revision: 2, warning_multiplier: 1.8 };
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
  render(<AuthoringUsageCheckpoint epicId="epic-1" projectId="project-1" jobId="job-1" kind="brainstorm" locked={false} />);
  const region = await screen.findByRole('region', { name: 'Authoring usage checkpoint' });

  // First edit attempts revision 2 (1 + 1), rejected with 409
  await user.click(within(region).getByRole('button', { name: 'Change monitoring settings' }));
  await user.click(within(region).getByRole('button', { name: 'Save monitoring revision' }));
  await screen.findByText('Checkpoint changed; current state refreshed. Review before acting again.');

  // Stale retry must be discarded
  expect(screen.queryByRole('button', { name: 'Retry same request' })).toBeNull();
  // Next/bypass/Stop remain eligible
  expect(within(region).getByRole('button', { name: 'Continue to next checkpoint' })).toBeEnabled();
  expect(within(region).getByRole('button', { name: 'Continue without usage pauses' })).toBeEnabled();
  expect(within(region).getByRole('button', { name: 'Stop authoring job' })).toBeEnabled();
  expect(within(region).getByRole('button', { name: 'Change monitoring settings' })).toBeEnabled();

  // Fresh edit uses refreshed revision (2 -> 3)
  await user.click(within(region).getByRole('button', { name: 'Change monitoring settings' }));
  await user.click(within(region).getByRole('button', { name: 'Save monitoring revision' }));
  await screen.findByText(/Owner command recorded/);

  expect(posts).toHaveLength(2);
  expect(posts[0].body).toMatchObject({
    action: 'set_policy',
    requested_policy: expect.objectContaining({ revision: 2 }),
  });
  expect(posts[1].body).toMatchObject({
    action: 'set_policy',
    requested_policy: expect.objectContaining({ revision: 3 }),
  });
});

test('uncertain authoring policy edit retains exact body and key across polling and refresh', async () => {
  const posts: { body: Record<string, unknown>; key: string | null }[] = [];
  let postCount = 0;
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
    if (init?.method === 'POST') {
      postCount += 1;
      const body = JSON.parse(String(init.body)) as Record<string, unknown>;
      const key = new Headers(init.headers).get('Idempotency-Key');
      posts.push({ body, key });
      if (postCount === 1) throw new TypeError('connection lost');
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
  render(<AuthoringUsageCheckpoint epicId="epic-1" projectId="project-1" jobId="job-1" kind="brainstorm" locked={false} />);
  const region = await screen.findByRole('region', { name: 'Authoring usage checkpoint' });

  await user.click(within(region).getByRole('button', { name: 'Change monitoring settings' }));
  const warningInput = screen.getByLabelText('Warning multiplier');
  await user.clear(warningInput);
  await user.type(warningInput, '1.7');
  await user.click(within(region).getByRole('button', { name: 'Save monitoring revision' }));

  await screen.findByText('The result is uncertain. Retry the same request to retrieve its durable receipt.');
  expect(screen.getByRole('button', { name: 'Retry same request' })).toBeVisible();

  // Refresh must not rewrite the retry request
  await user.click(screen.getByRole('button', { name: 'Refresh authoring usage' }));
  expect(screen.getByRole('button', { name: 'Retry same request' })).toBeVisible();

  // Retry submits identical request
  await user.click(screen.getByRole('button', { name: 'Retry same request' }));
  await screen.findByText(/Owner command recorded/);

  expect(posts).toHaveLength(2);
  expect(posts[1]).toEqual(posts[0]);
  expect(posts[0].key).toBeTruthy();
  expect(posts[0].body).toMatchObject({
    action: 'set_policy',
    requested_policy: expect.objectContaining({ revision: 2, warning_multiplier: 1.7 }),
  });
});

test('enables continue to next checkpoint with exact-string token dimensions > MAX_SAFE in authoring checkpoint', async () => {
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
  render(<AuthoringUsageCheckpoint epicId="epic-1" projectId="project-1" jobId="job-1" kind="brainstorm" locked={false} />);
  const region = await screen.findByRole('region', { name: 'Authoring usage checkpoint' });
  expect(within(region).getByRole('button', { name: 'Continue to next checkpoint' })).toBeEnabled();
});

test('disables continue to next checkpoint when two dimensions exceed int64 boundary sum in authoring checkpoint', async () => {
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
  render(<AuthoringUsageCheckpoint epicId="epic-1" projectId="project-1" jobId="job-1" kind="brainstorm" locked={false} />);
  const region = await screen.findByRole('region', { name: 'Authoring usage checkpoint' });
  expect(within(region).getByRole('button', { name: 'Continue to next checkpoint' })).toBeDisabled();
});

test('preserves exact-string retained policy caps and allows saving authoring revision without precision-loss alert', async () => {
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
  render(<AuthoringUsageCheckpoint epicId="epic-1" projectId="project-1" jobId="job-1" kind="brainstorm" locked={false} />);
  const region = await screen.findByRole('region', { name: 'Authoring usage checkpoint' });
  await user.click(within(region).getByRole('button', { name: 'Change monitoring settings' }));
  expect(screen.queryByText(/exceeds safe browser integer precision/)).toBeNull();
  const saveBtn = within(region).getByRole('button', { name: 'Save monitoring revision' });
  expect(saveBtn).toBeEnabled();
  await user.click(saveBtn);
  await screen.findByText(/Owner command recorded/);
  expect(submitted).toMatchObject({
    action: 'set_policy',
    requested_policy: {
      manual_estimate_tokens: '9007199254740992',
      hard_token_cap: '9223372036854775807',
      hard_context_cap: null,
    },
  });
});
