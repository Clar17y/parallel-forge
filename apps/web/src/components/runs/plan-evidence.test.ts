import { describe, expect, test } from 'vitest';
import { parsePlanEvidence, matchesPlanEvidence, type PlanApprovalEvidenceData, type SubscriptionPlanApprovalEvidenceData } from './plan-evidence';
import { projection } from '@/test/projection';

const legacyEvidence: PlanApprovalEvidenceData = {
  task_version: 1,
  plan_attempt: 1,
  task_digest: '1'.repeat(64),
  plan_digest: '2'.repeat(64),
  repository: 'owner/repo',
  base_ref: 'main',
  base_sha: 'a'.repeat(40),
  policy_version: 2,
  dependency_changes: ['express@4.18.2'],
  required_checks: { test: 'planned', lint: 'planned' },
  runner_mode: 'docker',
  local_remediation_limit: 3,
  token_budget: 100000,
  cost_budget_minor: 500,
  duration_budget_seconds: 600,
};

const subscriptionEvidence: SubscriptionPlanApprovalEvidenceData = {
  ...legacyEvidence,
  schema_version: 2,
  result_digest: '3'.repeat(64),
  producer: {
    schema_version: 1,
    producer_kind: 'subscription_plan',
    attempt_id: '11111111-1111-1111-1111-111111111111',
    run_id: '22222222-2222-2222-2222-222222222222',
    task_id: '33333333-3333-3333-3333-333333333333',
    plan_attempt: 1,
    plan_digest: '2'.repeat(64),
    task_digest: '7'.repeat(64),
    envelope_digest: '4'.repeat(64),
    budget_digest: '5'.repeat(64),
    route_digest: '6'.repeat(64),
    telemetry: {
      input_tokens: 1200,
      output_tokens: 450,
      duration_ms: 3200,
    },
  },
};

describe('parsePlanEvidence', () => {

  test('parses valid legacy evidence', () => {
    const parsed = parsePlanEvidence(legacyEvidence as unknown as Record<string, unknown>);
    expect(parsed).toEqual(legacyEvidence);
  });

  test('parses valid subscription evidence', () => {
    const parsed = parsePlanEvidence(subscriptionEvidence as unknown as Record<string, unknown>);
    expect(parsed).toEqual(subscriptionEvidence);
  });

  test('rejects unexpected extra keys', () => {
    expect(() =>
      parsePlanEvidence({ ...legacyEvidence, extra_key: 'forbidden' } as unknown as Record<string, unknown>),
    ).toThrow('Plan evidence unavailable');
  });

  test('rejects unsupported schema_version', () => {
    expect(() =>
      parsePlanEvidence({ ...subscriptionEvidence, schema_version: 3 } as unknown as Record<string, unknown>),
    ).toThrow('Plan evidence unavailable');
  });

  test('rejects producer plan digest mismatch', () => {
    const badProducer = {
      ...subscriptionEvidence,
      producer: { ...subscriptionEvidence.producer, plan_digest: '7'.repeat(64) },
    };
    expect(() => parsePlanEvidence(badProducer as unknown as Record<string, unknown>)).toThrow(
      'Plan evidence unavailable',
    );
  });

  test('rejects nil UUIDs in producer', () => {
    const nilAttempt = {
      ...subscriptionEvidence,
      producer: { ...subscriptionEvidence.producer, attempt_id: '00000000-0000-0000-0000-000000000000' },
    };
    expect(() => parsePlanEvidence(nilAttempt as unknown as Record<string, unknown>)).toThrow(
      'Plan evidence unavailable',
    );
  });

  test('rejects invalid telemetry numbers', () => {
    const badTelemetry = {
      ...subscriptionEvidence,
      producer: { ...subscriptionEvidence.producer, telemetry: { input_tokens: -1, output_tokens: 0, duration_ms: 0 } },
    };
    expect(() => parsePlanEvidence(badTelemetry as unknown as Record<string, unknown>)).toThrow(
      'Plan evidence unavailable',
    );
  });

  test('rejects malformed digests and SHAs', () => {
    expect(() =>
      parsePlanEvidence({ ...legacyEvidence, task_digest: 'short' } as unknown as Record<string, unknown>),
    ).toThrow('Plan evidence unavailable');
    expect(() =>
      parsePlanEvidence({ ...legacyEvidence, base_sha: 'invalid-sha' } as unknown as Record<string, unknown>),
    ).toThrow('Plan evidence unavailable');
  });
});

describe('matchesPlanEvidence', () => {
  const baseEvidence: PlanApprovalEvidenceData = {
    task_version: 1,
    plan_attempt: 1,
    task_digest: '1'.repeat(64),
    plan_digest: 'c'.repeat(64),
    repository: 'owner/repo',
    base_ref: 'main',
    base_sha: 'a'.repeat(40),
    policy_version: 2,
    dependency_changes: [],
    required_checks: { test: 'planned' },
    runner_mode: 'docker',
    local_remediation_limit: 3,
    token_budget: 116000,
    cost_budget_minor: 1000,
    duration_budget_seconds: 1800,
  };

  test('matches authoritative projection and command', () => {
    const proj = projection();
    const cmd = proj.available_commands[0];
    expect(matchesPlanEvidence(baseEvidence, proj, cmd)).toBe(true);
  });

  test('handles refs/heads/ prefix differences cleanly', () => {
    const proj = projection();
    proj.run.base_ref = 'refs/heads/main';
    const cmd = proj.available_commands[0];
    expect(matchesPlanEvidence(baseEvidence, proj, cmd)).toBe(true);
  });

  test('detects repository mismatch', () => {
    const proj = projection();
    proj.project.github_repository = 'other/repo';
    const cmd = proj.available_commands[0];
    expect(matchesPlanEvidence(baseEvidence, proj, cmd)).toBe(false);
  });

  test('detects base_sha mismatch', () => {
    const proj = projection();
    proj.run.base_sha = 'b'.repeat(40);
    const cmd = proj.available_commands[0];
    expect(matchesPlanEvidence(baseEvidence, proj, cmd)).toBe(false);
  });

  test('detects policy_version mismatch', () => {
    const proj = projection();
    proj.project.policy_version = 3;
    const cmd = proj.available_commands[0];
    expect(matchesPlanEvidence(baseEvidence, proj, cmd)).toBe(false);
  });

  test('detects runner_mode mismatch', () => {
    const proj = projection();
    proj.security.runner_mode = 'trusted_host';
    const cmd = proj.available_commands[0];
    expect(matchesPlanEvidence(baseEvidence, proj, cmd)).toBe(false);
  });

  test('detects plan_digest mismatch when output_artifact_digest is present', () => {
    const proj = projection();
    proj.plan.output_artifact_digest = '9'.repeat(64);
    const cmd = proj.available_commands[0];
    expect(matchesPlanEvidence(baseEvidence, proj, cmd)).toBe(false);
  });

  test('fails closed when output_artifact_digest is absent on gated projection', () => {
    const proj = projection();
    proj.plan.output_artifact_digest = null;
    const cmd = proj.available_commands[0];
    expect(matchesPlanEvidence(baseEvidence, proj, cmd)).toBe(false);
  });

  test('matches subscription evidence with distinct task digests when producer run_id matches', () => {
    const proj = projection();
    proj.run.id = '22222222-2222-2222-2222-222222222222';
    proj.plan.output_artifact_digest = '2'.repeat(64);
    const cmd = proj.available_commands[0];
    expect(matchesPlanEvidence(subscriptionEvidence, proj, cmd)).toBe(true);
  });

  test('rejects subscription evidence with foreign producer run_id', () => {
    const proj = projection();
    proj.run.id = '99999999-9999-9999-9999-999999999999';
    proj.plan.output_artifact_digest = '2'.repeat(64);
    const cmd = proj.available_commands[0];
    expect(matchesPlanEvidence(subscriptionEvidence, proj, cmd)).toBe(false);
  });
});
