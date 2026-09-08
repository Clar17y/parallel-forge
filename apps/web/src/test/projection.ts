import type { components } from '@/lib/api/schema';

export function projection(overrides: Partial<components['schemas']['RunProjection']> = {}): components['schemas']['RunProjection'] {
  return {
    recovery_hold: false, remote_observation: null,
    run: { id: 'run-1', project_id: 'project-1', task_id: 'task-1', state: 'AWAITING_PLAN_APPROVAL', version: 7,
      suspended_state: null, suspension_kind: null, local_remediation_count: 0, remote_remediation_count: 0,
      policy_version: 2, base_ref: 'main', base_sha: 'a'.repeat(40), branch_name: null },
    task: { id: 'task-1', title: 'Implement feature', body: 'Task description', external_source: null, source_url: null, untrusted_external_content: false },
    project: { id: 'project-1', name: 'Parallel', github_repository: 'owner/repo', policy_version: 2, policy_digest: 'b'.repeat(64) },
    resource: { worktree_path: null, branch_name: null, database_state: 'DISABLED', database_name: null },
    plan: { output_artifact_digest: 'c'.repeat(64), approval_id: null, approval_evidence_digest: 'd'.repeat(64) },
    candidate: { commit: null, pending_evidence_digest: 'd'.repeat(64), validation_evidence_digest: null, review_evidence_digest: null },
    pull_request: null, checks: [], review: { execution_id: null, evidence_digest: null, head_sha: null, findings: [] }, agents: {},
    budgets: { local_remediation_count: 0, local_remediation_limit: 3, local_remediation_remaining: 3,
      remote_remediation_count: 0, remote_remediation_limit: 3, remote_remediation_remaining: 3,
      token_limit: 116000, cost_limit_minor: 1000, duration_limit_seconds: 1800 },
    usage: { input_tokens: 0, output_tokens: 0, cached_input_tokens: 0, duration_ms: 0, tool_calls: 0, model_calls: 0, currencies: [] },
    security: { commands: [], secret_paths: ['.env', '.env.local'], runner_mode: 'docker', trusted_project: false, database_enabled: false }, latest_events: [],
    available_commands: [{ name: 'approve_plan', expected_run_version: 7, requires_feedback: false,
      gate: 'plan', evidence_digest: 'd'.repeat(64), policy_version: 2 }], next_gate: 'plan', ...overrides,
  };
}
