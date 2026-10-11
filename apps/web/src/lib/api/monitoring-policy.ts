import { api, mutate } from './client';
import type { components } from './schema';

export type MonitoringMode = components['schemas']['MonitoringMode'];
export type MonitoringPolicyResponse = components['schemas']['MonitoringPolicyResponse'];
export type MonitoringPolicyRevisionRequest = components['schemas']['MonitoringPolicyRevisionRequest'];
export type TaskUsageHistoryResponse = components['schemas']['TaskUsageHistoryResponse'];
export type UsageHistorySummaryResponse = components['schemas']['UsageHistorySummaryResponse'];

export const JS_MAX_SAFE_INTEGER = Number.MAX_SAFE_INTEGER; // 9,007,199,254,740,991
export const MAX_SAFE_SIGNED_64 = 9223372036854775807n; // 2^63 - 1

export interface IntegerValidationResult {
  valid: boolean;
  value?: number | null;
  error?: string;
}

export function validateSafeInteger(
  input: string | number | null | undefined,
  options: {
    fieldName?: string;
    allowNull?: boolean;
    allowZero?: boolean;
    min?: number;
    max?: number;
  } = {}
): IntegerValidationResult {
  const { fieldName = 'Value', allowNull = true, allowZero = false, min, max } = options;

  if (input === null || input === undefined) {
    if (allowNull) return { valid: true, value: null };
    return { valid: false, error: `${fieldName} is required.` };
  }

  const rawStr = String(input).trim();
  if (rawStr === '') {
    if (allowNull) return { valid: true, value: null };
    return { valid: false, error: `${fieldName} is required.` };
  }

  if (!/^-?\d+$/.test(rawStr)) {
    return { valid: false, error: `${fieldName} must be a whole integer.` };
  }

  try {
    const big = BigInt(rawStr);
    if (big > BigInt(Number.MAX_SAFE_INTEGER) || big < -BigInt(Number.MAX_SAFE_INTEGER)) {
      return {
        valid: false,
        error: `${fieldName} exceeds JavaScript safe integer limit (${Number.MAX_SAFE_INTEGER.toLocaleString()}). Integer precision loss rejected.`,
      };
    }
    const num = Number(big);
    if (!allowZero && num === 0) {
      return {
        valid: false,
        error: `${fieldName} cannot be zero. Zero is invalid; omit or clear for unbounded.`,
      };
    }
    if (min !== undefined && num < min) {
      return { valid: false, error: `${fieldName} must be at least ${min}.` };
    }
    if (max !== undefined && num > max) {
      return { valid: false, error: `${fieldName} cannot exceed ${max}.` };
    }
    return { valid: true, value: num };
  } catch {
    return { valid: false, error: `${fieldName} is not a valid integer.` };
  }
}

export async function getMonitoringPolicy(projectId: string): Promise<MonitoringPolicyResponse | null> {
  const result = await api<MonitoringPolicyResponse | null>(`/projects/${projectId}/monitoring-policy`);
  return result ?? null;
}

export async function getMonitoringPolicyRevision(
  projectId: string,
  revision: number
): Promise<MonitoringPolicyResponse> {
  if (!Number.isSafeInteger(revision) || revision < 1) {
    throw new Error('Revision must be a valid positive safe integer');
  }
  const result = await api<MonitoringPolicyResponse>(`/projects/${projectId}/monitoring-policy/${revision}`);
  if (!result) throw new Error('Monitoring policy revision unavailable');
  return result;
}

export async function reviseMonitoringPolicy(
  projectId: string,
  request: MonitoringPolicyRevisionRequest,
  options: { idempotencyKey?: string } = {}
): Promise<MonitoringPolicyResponse> {
  if (!Number.isSafeInteger(request.expected_revision) || request.expected_revision < 0) {
    throw new Error('Expected revision must be a valid nonnegative safe integer');
  }
  for (const field of ['hard_token_cap', 'hard_context_cap', 'manual_estimate_tokens', 'history_limit', 'minimum_comparables', 'window_days'] as const) {
    const value = request[field];
    if (value != null && !validateSafeInteger(value, { fieldName: field, min: 1 }).valid) {
      throw new Error(`${field} must be a positive safe integer or null`);
    }
  }
  const idempotencyKey = options.idempotencyKey ?? crypto.randomUUID();
  const result = await mutate<MonitoringPolicyResponse>(
    `/projects/${projectId}/monitoring-policy`,
    request as Record<string, unknown>,
    { idempotencyKey, method: 'POST' }
  );
  if (!result) throw new Error('Monitoring policy revision response unavailable');
  if (!Number.isSafeInteger(result.revision) || result.revision < 1) throw new Error('Monitoring policy revision response has unsafe revision');
  return result;
}

export function makeMonitoringPolicySaver(projectId: string) {
  let attempt: { body: string; key: string } | null = null;
  return {
    async save(request: MonitoringPolicyRevisionRequest): Promise<MonitoringPolicyResponse> {
      const body = JSON.stringify(request);
      if (attempt?.body !== body) attempt = { body, key: crypto.randomUUID() };
      const saved = await reviseMonitoringPolicy(projectId, request, { idempotencyKey: attempt.key });
      attempt = null;
      return saved;
    },
    reset() { attempt = null; },
  };
}

export interface TaskUsageReportFilterParams {
  projectId: string;
  subjectKind?: 'run' | 'authoring_job' | null;
  taskKind?: string | null;
  provider?: string | null;
  model?: string | null;
  effort?: string | null;
  windowDays?: number | null;
}

export async function getTaskUsageReport(
  params: TaskUsageReportFilterParams
): Promise<TaskUsageHistoryResponse> {
  if (params.windowDays != null && !validateSafeInteger(params.windowDays, { fieldName: 'Window days', min: 1 }).valid) {
    throw new Error('Window days must be a positive safe integer');
  }
  const query = new URLSearchParams();
  query.append('project_id', params.projectId);
  if (params.subjectKind) query.append('subject_kind', params.subjectKind);
  if (params.taskKind) query.append('task_kind', params.taskKind);
  if (params.provider) query.append('provider', params.provider);
  if (params.model) query.append('model', params.model);
  if (params.effort) query.append('effort', params.effort);
  if (params.windowDays != null) {
    query.append('window_days', String(params.windowDays));
  }

  const result = await api<TaskUsageHistoryResponse>(`/task-usage-report?${query.toString()}`);
  if (!result) throw new Error('Task usage report unavailable');
  return result;
}
