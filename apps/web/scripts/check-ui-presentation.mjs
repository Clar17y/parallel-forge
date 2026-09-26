// Dependency-free regression checks. Requires the repository's Node.js 24 runtime.
// Run: node --test scripts/check-ui-presentation.mjs (from apps/web).
import test from 'node:test';
import assert from 'node:assert/strict';
import { describeRunState, localCheckDisplay, remoteCheckDisplay, workflowSteps, nextGateMessage, remediationOrigin } from '../src/components/runs/run-presentation.ts';

const head = 'a'.repeat(40);
const otherHead = 'b'.repeat(40);
const hex64 = 'c'.repeat(64);
const fixture = () => ({
  run: { state: 'IMPLEMENTING', suspended_state: null }, recovery_hold: false,
  plan: { approval_id: null }, candidate: { commit: head, review_evidence_digest: null },
  review: { evidence_digest: null, head_sha: null }, remote_observation: null,
  pull_request: null, next_gate: null, available_commands: [],
});
const local = (overrides = {}) => ({ id: 'check-1', name: 'Typecheck', head_sha: head, status: 'SUCCEEDED', exit_code: 0, ...overrides });
const remote = (overrides = {}) => ({ name: 'Integration', head_sha: head, status: 'completed', conclusion: 'success', ...overrides });

test('a local success needs the exact candidate head and an explicit zero exit code', () => {
  assert.equal(localCheckDisplay(local(), head).tone, 'success');
  assert.notEqual(localCheckDisplay(local({ exit_code: null }), head).tone, 'success');
  assert.notEqual(localCheckDisplay(local({ head_sha: null }), head).tone, 'success');
  assert.notEqual(localCheckDisplay(local(), null).tone, 'success');
  assert.equal(localCheckDisplay(local(), otherHead).label, 'Different head');
});
test('unknown and skipped outcomes never become a pass', () => {
  for (const status of ['SKIPPED', 'NEW_PROVIDER_STATE', 'CANCELLED']) {
    assert.notEqual(localCheckDisplay(local({ status }), head).tone, 'success');
  }
  for (const conclusion of ['skipped', 'neutral', 'action_required', null]) {
    assert.notEqual(remoteCheckDisplay(remote({ conclusion }), head, head).tone, 'success');
  }
});
test('remote pass requires check, observation and candidate to share the exact head', () => {
  assert.equal(remoteCheckDisplay(remote(), head, head).tone, 'success');
  for (const [candidate, observed, checkHead] of [[head, otherHead, head], [head, head, otherHead], [null, head, head], [head, null, head], [head, head, null]]) {
    assert.notEqual(remoteCheckDisplay(remote({ head_sha: checkHead }), candidate, observed).tone, 'success');
  }
});
test('contradictory in-progress remote evidence is not reported as successful', () => {
  assert.notEqual(remoteCheckDisplay(remote({ status: 'in_progress' }), head, head).tone, 'success');
});
test('failures and pending checks have explicit, non-colour-only labels', () => {
  assert.equal(localCheckDisplay(local({ status: 'FAILED', exit_code: 1 }), head).label, 'Failed');
  assert.equal(remoteCheckDisplay(remote({ conclusion: 'failure' }), head, head).label, 'Failed');
  assert.equal(remoteCheckDisplay(remote({ status: 'queued', conclusion: null }), head, head).label, 'Queued');
});
test('a late run state does not paint preceding milestones complete', () => {
  const p = fixture(); p.run.state = 'AWAITING_MERGE_APPROVAL';
  const steps = workflowSteps(p);
  assert.equal(steps.filter(s => s.current).length, 1);
  assert.equal(steps.find(s => s.key === 'merge').current, true);
  assert.equal(steps.filter(s => s.recorded).length, 0);
});
test('a review for a previous head does not verify the current candidate', () => {
  const p = fixture(); p.review = { evidence_digest: 'review', head_sha: otherHead };
  p.candidate.review_evidence_digest = 'review';
  assert.equal(workflowSteps(p).find(s => s.key === 'review').recorded, false);
  p.review.head_sha = head;
  assert.equal(workflowSteps(p).find(s => s.key === 'review').detail, 'Review recorded');
});
test('queue admission and a completed run are not evidence of a completed merge', () => {
  const p = fixture(); p.run.state = 'COMPLETED';
  p.pull_request = { head_sha: head, queue_admission: 'accepted', merge_sha: null };
  assert.equal(workflowSteps(p).find(s => s.key === 'merge').recorded, false);
  p.pull_request.merge_sha = otherHead;
  assert.equal(workflowSteps(p).find(s => s.key === 'merge').detail, 'Merge recorded');
});
test('pause uses the suspended phase without claiming that work is active', () => {
  const p = fixture(); p.run.state = 'PAUSED'; p.run.suspended_state = 'REVIEWING';
  assert.equal(workflowSteps(p).find(s => s.key === 'review').detail, 'Paused here');
  assert.equal(describeRunState(p).title, 'Run paused');
});
test('recovery hold takes precedence over ordinary progress and approval messages', () => {
  const p = fixture(); p.recovery_hold = true;
  assert.equal(describeRunState(p).tone, 'danger');
  assert.equal(describeRunState(p).title, 'Recovery needs attention');
  assert.match(nextGateMessage(p), /reconcil/i);
});
test('remediation is explicit, without inferring that a past review still passes', () => {
  const p = fixture(); p.run.state = 'REMEDIATING';
  assert.equal(workflowSteps(p).find(s => s.current).key, 'build');
  assert.equal(describeRunState(p).tone, 'warning');
});
test('the next gate is not presented as authorised without its server-provided command', () => {
  const p = fixture(); p.next_gate = 'merge';
  assert.match(nextGateMessage(p), /not available/i);
  p.available_commands = [{ name: 'approve_merge' }];
  assert.match(nextGateMessage(p), /Review the exact/i);
});
test('no checks is not all checks passed, and CI is not inferred from the phase index', () => {
  const p = fixture(); p.run.state = 'MONITORING_PR';
  p.remote_observation = { head_sha: head, checks: [] };
  assert.equal(workflowSteps(p).find(s => s.key === 'ci').recorded, false);
  p.remote_observation.checks = [remote()];
  assert.equal(workflowSteps(p).find(s => s.key === 'ci').recorded, true);
  p.run.state = 'AWAITING_MERGE_APPROVAL';
  assert.equal(workflowSteps(p).find(s => s.key === 'ci').detail, 'Observation recorded');
});
test('failed and cancelled runs have no active work indicator', () => {
  for (const state of ['FAILED', 'CANCELLED']) {
    const p = fixture(); p.run.state = state;
    assert.equal(workflowSteps(p).some(s => s.current), false);
  }
});

test('every current backend run state has explicit presentation without inventing new states', () => {
  const titles = {
    CREATED: 'Run created', PLANNING: 'Preparing a plan', AWAITING_PLAN_APPROVAL: 'Plan approval needed',
    PREPARING_WORKTREE: 'Preparing the workspace', IMPLEMENTING: 'Implementation phase',
    VALIDATING: 'Validating the candidate', REVIEWING: 'Review phase', REMEDIATING: 'Repairing recorded findings',
    AWAITING_PR_APPROVAL: 'PR publication approval needed', PUBLISHING_PR: 'Publishing the approved candidate',
    MONITORING_PR: 'Monitoring the pull request', AWAITING_HUMAN_INTERVENTION: 'Human attention required',
    AWAITING_MERGE_APPROVAL: 'Merge approval needed', MERGING: 'Merge operation in progress',
    PAUSED: 'Run paused', COMPLETED: 'Run completed', FAILED: 'Run failed', CANCELLED: 'Run cancelled',
  };
  for (const [state, title] of Object.entries(titles)) {
    const p = fixture(); p.run.state = state;
    assert.equal(describeRunState(p).title, title);
    assert.equal(workflowSteps(p).filter(step => step.current).length <= 1, true);
  }
});
test('presentation does not modify the projection or manufacture approval commands', () => {
  const p = fixture(); p.next_gate = 'merge';
  p.remote_observation = { head_sha: head, checks: [remote()] };
  p.run.state = 'REMEDIATING';
  p.latest_events = [
    {
      sequence: 1,
      run_version: 3,
      actor_class: 'worker',
      event_type: 'run.pr_observed',
      occurred_at: '2026-01-01T00:00:00Z',
      payload: {
        source_command_id: 'cmd-1',
        pull_request_id: 'pr-1',
        poll: 1,
        observation_digest: hex64,
        reason: 'Checks failed',
        disposition: 'remediate',
        target: 'REMEDIATING',
      },
    },
  ];
  const snapshot = structuredClone(p);
  describeRunState(p); workflowSteps(p); nextGateMessage(p); remediationOrigin(p);
  remoteCheckDisplay(p.remote_observation.checks[0], head, head);
  assert.deepEqual(p, snapshot);
});

test('remote repair yields the remote title and repair step, while an unrecorded origin claims neither budget', () => {
  const p = fixture();
  p.run.state = 'REMEDIATING';
  p.latest_events = [
    {
      sequence: 1,
      run_version: 3,
      actor_class: 'worker',
      event_type: 'run.pr_observed',
      occurred_at: '2026-01-01T00:00:00Z',
      payload: {
        source_command_id: 'cmd-1',
        pull_request_id: 'pr-1',
        poll: 1,
        observation_digest: hex64,
        reason: 'Checks failed',
        disposition: 'remediate',
        target: 'REMEDIATING',
      },
    },
  ];
  assert.equal(remediationOrigin(p), 'remote');
  const desc = describeRunState(p);
  assert.equal(desc.title, 'Repairing observed PR findings');
  assert.equal(desc.tone, 'warning');
  assert.match(desc.description, /recorded pull request observation/i);
  assert.match(desc.description, /fresh validation and review/i);

  const steps = workflowSteps(p);
  const currentStep = steps.find(s => s.current);
  assert.equal(currentStep.key, 'build');
  assert.equal(currentStep.label, 'Repair & revalidate');
  assert.equal(currentStep.detail, 'Remote repair');

  // Plain REMEDIATING without events is unrecorded: 'Repairing recorded findings'
  const pPlain = fixture();
  pPlain.run.state = 'REMEDIATING';
  assert.equal(remediationOrigin(pPlain), 'unrecorded');
  assert.equal(describeRunState(pPlain).title, 'Repairing recorded findings');
  const plainStep = workflowSteps(pPlain).find(s => s.current);
  assert.equal(plainStep.key, 'build');
  assert.equal(plainStep.label, 'Repair & revalidate');
  assert.equal(plainStep.detail, 'Repair in progress');
});

test('a later local remediation transition is presented as local even when older remote repair event remains in window', () => {
  const p = fixture();
  p.run.state = 'REMEDIATING';
  p.latest_events = [
    {
      sequence: 1,
      run_version: 3,
      actor_class: 'worker',
      event_type: 'run.pr_observed',
      occurred_at: '2026-01-01T00:00:00Z',
      payload: {
        source_command_id: 'cmd-1',
        pull_request_id: 'pr-1',
        poll: 1,
        observation_digest: hex64,
        reason: 'Checks failed',
        disposition: 'remediate',
        target: 'REMEDIATING',
      },
    },
    {
      sequence: 2,
      run_version: 4,
      actor_class: 'worker',
      event_type: 'run.review_decided',
      occurred_at: '2026-01-01T00:00:02Z',
      payload: {
        approval_id: 'app-1',
        validation_evidence_set_id: 'ves-1',
        target: 'REMEDIATING',
        semantic_attempt: 1,
        local_remediation_count: 1,
      },
    },
  ];
  assert.equal(remediationOrigin(p), 'local');
  assert.equal(describeRunState(p).title, 'Repairing local findings');
  const buildStep = workflowSteps(p).find(s => s.current);
  assert.equal(buildStep.key, 'build');
  assert.equal(buildStep.label, 'Build & validate');
  assert.equal(buildStep.detail, 'Current phase');
});

test('a paused run in remote repair reports Run paused with Repair & revalidate step, and resume does not clear classification', () => {
  const p = fixture();
  p.run.state = 'PAUSED';
  p.run.suspended_state = 'REMEDIATING';
  p.latest_events = [
    {
      sequence: 1,
      run_version: 3,
      actor_class: 'worker',
      event_type: 'run.pr_observed',
      occurred_at: '2026-01-01T00:00:00Z',
      payload: {
        source_command_id: 'cmd-1',
        pull_request_id: 'pr-1',
        poll: 1,
        observation_digest: hex64,
        reason: 'Checks failed',
        disposition: 'remediate',
        target: 'REMEDIATING',
      },
    },
  ];
  assert.equal(remediationOrigin(p), 'remote');
  assert.equal(describeRunState(p).title, 'Run paused');
  const buildStep = workflowSteps(p).find(s => s.current);
  assert.equal(buildStep.key, 'build');
  assert.equal(buildStep.label, 'Repair & revalidate');
  assert.equal(buildStep.detail, 'Paused here');

  // Resume event without a target does not clear the remote classification
  p.run.state = 'REMEDIATING';
  p.run.suspended_state = null;
  p.latest_events.push({
    sequence: 2,
    run_version: 4,
    actor_class: 'operator',
    event_type: 'run.resumed',
    occurred_at: '2026-01-01T00:00:02Z',
    payload: { restored_state: 'REMEDIATING' },
  });
  assert.equal(remediationOrigin(p), 'remote');
  assert.equal(describeRunState(p).title, 'Repairing observed PR findings');
  const resumedStep = workflowSteps(p).find(s => s.current);
  assert.equal(resumedStep.key, 'build');
  assert.equal(resumedStep.label, 'Repair & revalidate');
  assert.equal(resumedStep.detail, 'Remote repair');
});

test('a same-version event that carries only a 64-hex observation_digest and target REMEDIATING is classified remote', () => {
  const p = fixture();
  p.run.state = 'REMEDIATING';
  p.latest_events = [
    {
      sequence: 1,
      run_version: 5,
      actor_class: 'worker',
      event_type: 'run.subscription_remote_repair_requested',
      occurred_at: '2026-01-01T00:00:00Z',
      payload: {
        observation_digest: hex64,
        target: 'REMEDIATING',
      },
    },
  ];
  assert.equal(remediationOrigin(p), 'remote');
  assert.equal(describeRunState(p).title, 'Repairing observed PR findings');
  const buildStep = workflowSteps(p).find(s => s.current);
  assert.equal(buildStep.key, 'build');
  assert.equal(buildStep.label, 'Repair & revalidate');
  assert.equal(buildStep.detail, 'Remote repair');
});

test('a window containing only tool_call.completed events at current run version results in unrecorded classification', () => {
  const p = fixture();
  p.run.state = 'REMEDIATING';
  p.latest_events = [
    {
      sequence: 51,
      run_version: 5,
      actor_class: 'worker',
      event_type: 'tool_call.completed',
      occurred_at: '2026-01-01T00:00:10Z',
      payload: { tool_name: 'read_file', output_digest: hex64 },
    },
    {
      sequence: 52,
      run_version: 5,
      actor_class: 'worker',
      event_type: 'tool_call.completed',
      occurred_at: '2026-01-01T00:00:11Z',
      payload: { tool_name: 'edit_file', output_digest: hex64 },
    },
  ];
  assert.equal(remediationOrigin(p), 'unrecorded');
  const desc = describeRunState(p);
  assert.equal(desc.title, 'Repairing recorded findings');
  assert.equal(desc.tone, 'warning');
  assert.match(desc.description, /retained event window no longer shows/i);
  assert.match(desc.description, /either remediation budget/i);

  const buildStep = workflowSteps(p).find(s => s.current);
  assert.equal(buildStep.key, 'build');
  assert.equal(buildStep.label, 'Repair & revalidate');
  assert.equal(buildStep.detail, 'Repair in progress');
});
