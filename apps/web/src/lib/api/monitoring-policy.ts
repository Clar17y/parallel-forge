import { api, mutate } from './client';
import { MAX_SIGNED_64 } from './exact-integer';
import type { components } from './schema';

export type MonitoringMode = components['schemas']['MonitoringMode'];
export type MonitoringPolicyResponse = components['schemas']['MonitoringPolicyResponse'];
export type MonitoringPolicyRevisionRequest = components['schemas']['MonitoringPolicyRevisionRequest'];
export type TaskUsageHistoryResponse = components['schemas']['TaskUsageHistoryResponse'];
export type UsageHistorySummaryResponse = components['schemas']['UsageHistorySummaryResponse'];

export const JS_MAX_SAFE_INTEGER = Number.MAX_SAFE_INTEGER; // 9,007,199,254,740,991
export const MAX_SAFE_SIGNED_64 = MAX_SIGNED_64;

export interface IntegerValidationResult {
  valid: boolean;
  value?: number | string | null;
  error?: string;
}

export function validateSafeInteger(
  input: string | number | null | undefined,
  options: {
    fieldName?: string;
    allowNull?: boolean;
    allowZero?: boolean;
    allowSigned64?: boolean;
    min?: number | bigint;
    max?: number | bigint;
  } = {}
): IntegerValidationResult {
  const { fieldName = 'Value', allowNull = true, allowZero = false, allowSigned64 = false, min, max } = options;

  if (input === null || input === undefined) {
    if (allowNull) return { valid: true, value: null };
    return { valid: false, error: `${fieldName} is required.` };
  }

  if (typeof input === 'number' && !Number.isSafeInteger(input)) {
    return {
      valid: false,
      error: `${fieldName} is an unsafe JavaScript number. Integer precision loss rejected.`,
    };
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
    if (allowSigned64) {
      if (big > MAX_SAFE_SIGNED_64 || big < -MAX_SAFE_SIGNED_64) {
        return {
          valid: false,
          error: `${fieldName} exceeds signed 64-bit integer limit (${MAX_SAFE_SIGNED_64.toString()}).`,
        };
      }
    } else {
      if (big > BigInt(Number.MAX_SAFE_INTEGER) || big < -BigInt(Number.MAX_SAFE_INTEGER)) {
        return {
          valid: false,
          error: `${fieldName} exceeds JavaScript safe integer limit (${Number.MAX_SAFE_INTEGER.toLocaleString()}). Integer precision loss rejected.`,
        };
      }
    }

    if (!allowZero && big === 0n) {
      return {
        valid: false,
        error: `${fieldName} cannot be zero. Zero is invalid; omit or clear for unbounded.`,
      };
    }
    if (min !== undefined && big < BigInt(min)) {
      return { valid: false, error: `${fieldName} must be at least ${min}.` };
    }
    if (max !== undefined && big > BigInt(max)) {
      return { valid: false, error: `${fieldName} cannot exceed ${max}.` };
    }

    if (big >= -BigInt(Number.MAX_SAFE_INTEGER) && big <= BigInt(Number.MAX_SAFE_INTEGER)) {
      return { valid: true, value: Number(big) };
    }
    return { valid: true, value: big.toString() };
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
  revision: number | string
): Promise<MonitoringPolicyResponse> {
  const validated = validateSafeInteger(revision, { allowSigned64: true, min: 1, allowNull: false });
  if (!validated.valid) throw new Error('Revision must be a valid positive signed 64-bit integer');
  const result = await api<MonitoringPolicyResponse>(`/projects/${projectId}/monitoring-policy/${validated.value}`);
  if (!result) throw new Error('Monitoring policy revision unavailable');
  return result;
}

export async function reviseMonitoringPolicy(
  projectId: string,
  request: MonitoringPolicyRevisionRequest,
  options: { idempotencyKey?: string } = {}
): Promise<MonitoringPolicyResponse> {
  const expected = validateSafeInteger(request.expected_revision, { allowSigned64: true,
    min: 0, max: MAX_SIGNED_64 - 1n, allowZero: true, allowNull: false });
  if (!expected.valid) throw new Error('Expected revision must be a valid nonnegative signed 64-bit integer');
  const signed64Fields = new Set([
    'hard_token_cap',
    'hard_context_cap',
    'manual_estimate_tokens',
    'history_limit',
    'minimum_comparables',
    'window_days',
  ]);
  const sanitizedRequest: MonitoringPolicyRevisionRequest = { ...request, expected_revision: expected.value as number | string };
  for (const field of ['hard_token_cap', 'hard_context_cap', 'manual_estimate_tokens', 'history_limit', 'minimum_comparables', 'window_days'] as const) {
    const value = request[field];
    if (value != null) {
      const isSigned64 = signed64Fields.has(field);
      const validation = validateSafeInteger(value, {
        fieldName: field,
        min: 1,
        allowSigned64: isSigned64,
      });
      if (!validation.valid) {
        throw new Error(`${field} must be a positive safe integer or null`);
      }
      sanitizedRequest[field] = validation.value as (number | string);
    }
  }
  if (sanitizedRequest.history_limit != null && sanitizedRequest.minimum_comparables != null) {
    if (BigInt(sanitizedRequest.minimum_comparables) > BigInt(sanitizedRequest.history_limit)) {
      throw new Error('minimum_comparables cannot exceed history_limit');
    }
  }
  const idempotencyKey = options.idempotencyKey ?? crypto.randomUUID();
  const result = await mutate<MonitoringPolicyResponse>(
    `/projects/${projectId}/monitoring-policy`,
    sanitizedRequest as Record<string, unknown>,
    { idempotencyKey, method: 'POST' }
  );
  if (!result) throw new Error('Monitoring policy revision response unavailable');
  if (!validateSafeInteger(result.revision, { allowSigned64: true, min: 1, allowNull: false }).valid) {
    throw new Error('Monitoring policy revision response has invalid revision');
  }
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
  windowDays?: number | string | null;
}

export async function getTaskUsageReport(
  params: TaskUsageReportFilterParams
): Promise<TaskUsageHistoryResponse> {
  const query = new URLSearchParams();
  query.append('project_id', params.projectId);
  if (params.subjectKind) query.append('subject_kind', params.subjectKind);
  if (params.taskKind) query.append('task_kind', params.taskKind);
  if (params.provider) query.append('provider', params.provider);
  if (params.model) query.append('model', params.model);
  if (params.effort) query.append('effort', params.effort);
  if (params.windowDays != null) {
    const validation = validateSafeInteger(params.windowDays, {
      fieldName: 'Window days',
      min: 1,
      allowSigned64: true,
    });
    if (!validation.valid) {
      throw new Error(validation.error ?? 'Window days must be a positive safe integer');
    }
    query.append('window_days', String(validation.value));
  }

  const result = await api<TaskUsageHistoryResponse>(`/task-usage-report?${query.toString()}`);
  if (!result) throw new Error('Task usage report unavailable');
  return result;
}
