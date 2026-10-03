import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, test } from 'vitest';
import { PlanApprovalEvidence } from './plan-approval-evidence';
import type { Plan } from './plan-content';
import type { PlanApprovalEvidenceData, SubscriptionPlanApprovalEvidenceData } from './plan-evidence';

afterEach(() => {
  cleanup();
});

describe('PlanApprovalEvidence', () => {
  const plan: Plan = {
    summary: 'Refactor database client',
    assumptions: ['Postgres is running'],
    affected_components: ['persistence'],
    steps: ['Add pooling', 'Add connection retries'],
    required_checks: ['db-migration-check'],
    risks: ['Pool exhaustion under load'],
    security_considerations: ['Sanitize connection string credentials'],
    dependency_changes: ['pg-pool'],
    owned_paths: ['src/db/pool.ts', 'src/db/client.ts'],
  };

  const legacyEvidence: PlanApprovalEvidenceData = {
    task_version: 1,
    plan_attempt: 1,
    task_digest: '1'.repeat(64),
    plan_digest: '2'.repeat(64),
    repository: 'owner/parallel-forge',
    base_ref: 'main',
    base_sha: 'a'.repeat(40),
    policy_version: 3,
    dependency_changes: ['pg-pool'],
    required_checks: { 'db-migration-check': 'planned' },
    runner_mode: 'docker',
    local_remediation_limit: 2,
    token_budget: 80000,
    cost_budget_minor: 400,
    duration_budget_seconds: 900,
  };

  const subscriptionEvidence: SubscriptionPlanApprovalEvidenceData = {
    ...legacyEvidence,
    runner_mode: 'trusted_host',
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

  test('renders readable plan proposal sections and honest execution limits', () => {
    render(<PlanApprovalEvidence evidence={legacyEvidence} plan={plan} />);

    expect(screen.getByText('Refactor database client')).toBeInTheDocument();
    expect(screen.getByText('Add pooling')).toBeInTheDocument();
    expect(screen.getByText('Postgres is running')).toBeInTheDocument();
    expect(screen.getByText('persistence')).toBeInTheDocument();
    expect(screen.getByText('db-migration-check')).toBeInTheDocument();
    expect(screen.getByText('Pool exhaustion under load')).toBeInTheDocument();
    expect(screen.getByText('Sanitize connection string credentials')).toBeInTheDocument();
    expect(screen.getByText('pg-pool')).toBeInTheDocument();
    expect(screen.getByText('src/db/pool.ts')).toBeInTheDocument();

    expect(screen.getByText('owner/parallel-forge')).toBeInTheDocument();
    expect(screen.getByText('Docker')).toBeInTheDocument();
    expect(screen.getByText('2 attempts')).toBeInTheDocument();
    expect(screen.getByText('80000 tokens')).toBeInTheDocument();
    expect(screen.getByText('400 minor currency units')).toBeInTheDocument();
    expect(screen.getByText('900 seconds')).toBeInTheDocument();

    // Technical details
    expect(screen.getByText('Technical details')).toBeInTheDocument();
    expect(screen.getByText('2'.repeat(64))).toBeInTheDocument();
  });

  test('renders trusted host runner and subscription producer details', () => {
    render(<PlanApprovalEvidence evidence={subscriptionEvidence} plan={plan} />);

    expect(screen.getByText('Trusted host · unsandboxed')).toBeInTheDocument();
    expect(screen.getByText('3'.repeat(64))).toBeInTheDocument(); // result digest
    expect(screen.getByText('11111111-1111-1111-1111-111111111111')).toBeInTheDocument(); // attempt ID
  });

  test('does not infer unlimited budgets from zeros', () => {
    const zeroBudgets: PlanApprovalEvidenceData = {
      ...legacyEvidence,
      local_remediation_limit: 0,
      token_budget: 0,
      cost_budget_minor: 0,
    };
    render(<PlanApprovalEvidence evidence={zeroBudgets} plan={plan} />);

    expect(screen.getByText('0 attempts')).toBeInTheDocument();
    expect(screen.getByText('0 tokens')).toBeInTheDocument();
    expect(screen.getByText('0 minor currency units')).toBeInTheDocument();
    expect(screen.queryByText(/unlimited/i)).toBeNull();
  });

  test('legacy plan without owned_paths does not imply a scope guarantee', () => {
    const legacyPlan: Plan = {
      ...plan,
      owned_paths: undefined,
    };
    render(<PlanApprovalEvidence evidence={legacyEvidence} plan={legacyPlan} />);

    expect(screen.queryByRole('heading', { name: 'Writable paths' })).toBeNull();
    expect(screen.queryByText('No writable paths.')).toBeNull();
  });

  test('renders raw artifact text exactly unchanged while readable fields use defaults', () => {
    const rawLegacyJson = JSON.stringify({
      task_version: 1,
      task_digest: '1'.repeat(64),
      plan_digest: '2'.repeat(64),
      repository: 'owner/parallel-forge',
      base_ref: 'main',
      base_sha: 'a'.repeat(40),
      policy_version: 3,
      dependency_changes: [],
      required_checks: { 'db-migration-check': 'planned' },
      runner_mode: 'docker',
      local_remediation_limit: 1,
      token_budget: 1000,
      cost_budget_minor: 10,
      duration_budget_seconds: 60,
    });
    const parsedLegacy: PlanApprovalEvidenceData = {
      ...legacyEvidence,
      plan_attempt: 1,
      local_remediation_limit: 1,
    };

    render(<PlanApprovalEvidence evidence={parsedLegacy} plan={plan} rawEvidence={rawLegacyJson} />);

    const rawBlock = document.querySelector('pre.policy-document');
    expect(rawBlock).not.toBeNull();
    expect(rawBlock?.textContent).toBe(rawLegacyJson);
    expect(rawBlock?.textContent).not.toContain('"plan_attempt"');
    expect(screen.getByText('1 attempt')).toBeInTheDocument();
  });
});
