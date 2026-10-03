import type { components } from '@/lib/api/schema';

export type PlanApprovalEvidenceData = {
  task_version: number;
  plan_attempt: number;
  task_digest: string;
  plan_digest: string;
  repository: string;
  base_ref: string;
  base_sha: string;
  policy_version: number;
  dependency_changes: string[];
  required_checks: Record<string, string>;
  runner_mode: 'docker' | 'trusted_host';
  local_remediation_limit: number;
  token_budget: number;
  cost_budget_minor: number;
  duration_budget_seconds: number;
};

export type SubscriptionPlanProducerData = {
  schema_version: 1;
  producer_kind: 'subscription_plan';
  attempt_id: string;
  run_id: string;
  task_id: string;
  plan_attempt: number;
  plan_digest: string;
  task_digest: string;
  envelope_digest: string;
  budget_digest: string;
  route_digest: string;
  telemetry: {
    input_tokens: number | null;
    output_tokens: number | null;
    duration_ms: number | null;
  };
};

export type SubscriptionPlanApprovalEvidenceData = PlanApprovalEvidenceData & {
  schema_version: 2;
  result_digest: string;
  producer: SubscriptionPlanProducerData;
};

export type PlanEvidence = PlanApprovalEvidenceData | SubscriptionPlanApprovalEvidenceData;

const SHA40_REGEX = /^[0-9a-fA-F]{40}$/;
const DIGEST64_REGEX = /^[0-9a-fA-F]{64}$/;
const UUID_REGEX = /^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$/;
const NIL_UUID = '00000000-0000-0000-0000-000000000000';

function isNonNilUuid(value: unknown): value is string {
  return typeof value === 'string' && UUID_REGEX.test(value) && value !== NIL_UUID;
}

export function parsePlanEvidence(value: Record<string, unknown>): PlanEvidence {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error('Plan evidence unavailable');
  }

  const baseFields = [
    'task_version',
    'plan_attempt',
    'task_digest',
    'plan_digest',
    'repository',
    'base_ref',
    'base_sha',
    'policy_version',
    'dependency_changes',
    'required_checks',
    'runner_mode',
    'local_remediation_limit',
    'token_budget',
    'cost_budget_minor',
    'duration_budget_seconds',
  ];

  const hasSchemaVersion = Object.hasOwn(value, 'schema_version');
  const allowedKeys = hasSchemaVersion
    ? [...baseFields, 'schema_version', 'result_digest', 'producer']
    : baseFields;

  if (Object.keys(value).some(key => !allowedKeys.includes(key))) {
    throw new Error('Plan evidence unavailable');
  }

  const taskVersion = value.task_version;
  if (!Number.isSafeInteger(taskVersion) || Number(taskVersion) < 1) {
    throw new Error('Plan evidence unavailable');
  }

  const planAttempt = value.plan_attempt !== undefined ? value.plan_attempt : 1;
  if (!Number.isSafeInteger(planAttempt) || Number(planAttempt) < 1) {
    throw new Error('Plan evidence unavailable');
  }

  const taskDigest = value.task_digest;
  const planDigest = value.plan_digest;
  if (
    typeof taskDigest !== 'string' ||
    !DIGEST64_REGEX.test(taskDigest) ||
    typeof planDigest !== 'string' ||
    !DIGEST64_REGEX.test(planDigest)
  ) {
    throw new Error('Plan evidence unavailable');
  }

  const repository = value.repository;
  const baseRef = value.base_ref;
  if (typeof repository !== 'string' || !repository.trim() || typeof baseRef !== 'string' || !baseRef.trim()) {
    throw new Error('Plan evidence unavailable');
  }

  const baseSha = value.base_sha;
  if (typeof baseSha !== 'string' || !SHA40_REGEX.test(baseSha)) {
    throw new Error('Plan evidence unavailable');
  }

  const policyVersion = value.policy_version;
  if (!Number.isSafeInteger(policyVersion) || Number(policyVersion) < 1) {
    throw new Error('Plan evidence unavailable');
  }

  const dependencyChanges = value.dependency_changes;
  if (
    !Array.isArray(dependencyChanges) ||
    !dependencyChanges.every(item => typeof item === 'string' && item.length <= 5000)
  ) {
    throw new Error('Plan evidence unavailable');
  }

  const requiredChecks = value.required_checks;
  if (
    !requiredChecks ||
    typeof requiredChecks !== 'object' ||
    Array.isArray(requiredChecks) ||
    Object.entries(requiredChecks).some(
      ([key, status]) => typeof key !== 'string' || !key.trim() || typeof status !== 'string' || !status.trim(),
    )
  ) {
    throw new Error('Plan evidence unavailable');
  }

  const runnerMode = value.runner_mode;
  if (runnerMode !== 'docker' && runnerMode !== 'trusted_host') {
    throw new Error('Plan evidence unavailable');
  }

  const localRemediationLimit = value.local_remediation_limit;
  if (!Number.isSafeInteger(localRemediationLimit) || Number(localRemediationLimit) < 0) {
    throw new Error('Plan evidence unavailable');
  }

  const tokenBudget = value.token_budget;
  if (!Number.isSafeInteger(tokenBudget) || Number(tokenBudget) < 0) {
    throw new Error('Plan evidence unavailable');
  }

  const costBudgetMinor = value.cost_budget_minor;
  if (!Number.isSafeInteger(costBudgetMinor) || Number(costBudgetMinor) < 0) {
    throw new Error('Plan evidence unavailable');
  }

  const durationBudgetSeconds = value.duration_budget_seconds;
  if (!Number.isSafeInteger(durationBudgetSeconds) || Number(durationBudgetSeconds) < 1) {
    throw new Error('Plan evidence unavailable');
  }

  const parsedBase: PlanApprovalEvidenceData = {
    task_version: Number(taskVersion),
    plan_attempt: Number(planAttempt),
    task_digest: taskDigest,
    plan_digest: planDigest,
    repository,
    base_ref: baseRef,
    base_sha: baseSha,
    policy_version: Number(policyVersion),
    dependency_changes: dependencyChanges as string[],
    required_checks: requiredChecks as Record<string, string>,
    runner_mode: runnerMode,
    local_remediation_limit: Number(localRemediationLimit),
    token_budget: Number(tokenBudget),
    cost_budget_minor: Number(costBudgetMinor),
    duration_budget_seconds: Number(durationBudgetSeconds),
  };

  if (!hasSchemaVersion) {
    return parsedBase;
  }

  if (value.schema_version !== 2) {
    throw new Error('Plan evidence unavailable');
  }

  const resultDigest = value.result_digest;
  if (typeof resultDigest !== 'string' || !DIGEST64_REGEX.test(resultDigest)) {
    throw new Error('Plan evidence unavailable');
  }

  const producerRaw = value.producer;
  if (!producerRaw || typeof producerRaw !== 'object' || Array.isArray(producerRaw)) {
    throw new Error('Plan evidence unavailable');
  }
  const producer = producerRaw as Record<string, unknown>;

  const producerKeys = [
    'schema_version',
    'producer_kind',
    'attempt_id',
    'run_id',
    'task_id',
    'plan_attempt',
    'plan_digest',
    'task_digest',
    'envelope_digest',
    'budget_digest',
    'route_digest',
    'telemetry',
  ];
  if (Object.keys(producer).some(key => !producerKeys.includes(key))) {
    throw new Error('Plan evidence unavailable');
  }

  if (producer.schema_version !== 1 || producer.producer_kind !== 'subscription_plan') {
    throw new Error('Plan evidence unavailable');
  }

  if (!isNonNilUuid(producer.attempt_id) || !isNonNilUuid(producer.run_id) || !isNonNilUuid(producer.task_id)) {
    throw new Error('Plan evidence unavailable');
  }

  if (!Number.isSafeInteger(producer.plan_attempt) || Number(producer.plan_attempt) < 1) {
    throw new Error('Plan evidence unavailable');
  }
  if (producer.plan_attempt !== parsedBase.plan_attempt) {
    throw new Error('Plan evidence unavailable');
  }

  const producerDigests = ['plan_digest', 'task_digest', 'envelope_digest', 'budget_digest', 'route_digest'];
  if (producerDigests.some(key => typeof producer[key] !== 'string' || !DIGEST64_REGEX.test(String(producer[key])))) {
    throw new Error('Plan evidence unavailable');
  }
  if (producer.plan_digest !== parsedBase.plan_digest) {
    throw new Error('Plan evidence unavailable');
  }

  const telemetryRaw = producer.telemetry;
  if (!telemetryRaw || typeof telemetryRaw !== 'object' || Array.isArray(telemetryRaw)) {
    throw new Error('Plan evidence unavailable');
  }
  const telemetry = telemetryRaw as Record<string, unknown>;
  const telemetryKeys = ['input_tokens', 'output_tokens', 'duration_ms'];
  if (
    Object.keys(telemetry).length !== 3 ||
    telemetryKeys.some(key => !Object.hasOwn(telemetry, key)) ||
    telemetryKeys.some(key => {
      const val = telemetry[key];
      return val !== null && (!Number.isSafeInteger(val) || Number(val) < 0);
    })
  ) {
    throw new Error('Plan evidence unavailable');
  }

  return {
    ...parsedBase,
    schema_version: 2,
    result_digest: resultDigest,
    producer: {
      schema_version: 1,
      producer_kind: 'subscription_plan',
      attempt_id: String(producer.attempt_id),
      run_id: String(producer.run_id),
      task_id: String(producer.task_id),
      plan_attempt: Number(producer.plan_attempt),
      plan_digest: String(producer.plan_digest),
      task_digest: String(producer.task_digest),
      envelope_digest: String(producer.envelope_digest),
      budget_digest: String(producer.budget_digest),
      route_digest: String(producer.route_digest),
      telemetry: {
        input_tokens: telemetry.input_tokens as number | null,
        output_tokens: telemetry.output_tokens as number | null,
        duration_ms: telemetry.duration_ms as number | null,
      },
    },
  };
}

export function branchName(ref: string | null | undefined): string {
  if (!ref) return '';
  return ref.startsWith('refs/heads/') ? ref.slice('refs/heads/'.length) : ref;
}

export function matchesPlanEvidence(
  evidence: PlanEvidence,
  projection: components['schemas']['RunProjection'],
  command: components['schemas']['AvailableCommand'],
): boolean {
  if (
    evidence.repository !== projection.project.github_repository ||
    evidence.base_sha !== projection.run.base_sha ||
    branchName(evidence.base_ref) !== branchName(projection.run.base_ref) ||
    evidence.policy_version !== projection.project.policy_version ||
    evidence.policy_version !== command.policy_version ||
    evidence.runner_mode !== projection.security.runner_mode
  ) {
    return false;
  }
  if (!projection.plan.output_artifact_digest || evidence.plan_digest !== projection.plan.output_artifact_digest) {
    return false;
  }
  if ('producer' in evidence && evidence.producer.run_id !== projection.run.id) {
    return false;
  }
  return true;
}
