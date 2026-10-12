import { describe, expect, test, vi, beforeEach } from 'vitest';
import {
  getMonitoringPolicy,
  getMonitoringPolicyRevision,
  reviseMonitoringPolicy,
  getTaskUsageReport,
  makeMonitoringPolicySaver,
  validateSafeInteger,
  JS_MAX_SAFE_INTEGER,
  MAX_SAFE_SIGNED_64,
} from './monitoring-policy';
import { api, mutate } from './client';
import type {
  MonitoringPolicyResponse,
  MonitoringPolicyRevisionRequest,
  TaskUsageHistoryResponse,
} from './monitoring-policy';

vi.mock('./client', () => ({
  api: vi.fn(),
  mutate: vi.fn(),
  ApiError: class ApiError extends Error {
    constructor(public status: number, public code: string, public fields: Record<string, string> = {}) {
      super(code);
    }
  },
}));

describe('monitoring-policy API and integer validation', () => {
  const projectId = '11111111-1111-4111-8111-111111111111';

  beforeEach(() => {
    vi.clearAllMocks();
  });

  describe('validateSafeInteger', () => {
    test('accepts blank/null/undefined as unbounded (null) when allowNull is true', () => {
      expect(validateSafeInteger('', { allowNull: true })).toEqual({ valid: true, value: null });
      expect(validateSafeInteger('   ', { allowNull: true })).toEqual({ valid: true, value: null });
      expect(validateSafeInteger(null, { allowNull: true })).toEqual({ valid: true, value: null });
      expect(validateSafeInteger(undefined, { allowNull: true })).toEqual({ valid: true, value: null });
    });

    test('rejects blank when allowNull is false', () => {
      const res = validateSafeInteger('', { allowNull: false, fieldName: 'Revision' });
      expect(res.valid).toBe(false);
      expect(res.error).toContain('Revision is required');
    });

    test('parses safe positive integers accurately', () => {
      expect(validateSafeInteger(42)).toEqual({ valid: true, value: 42 });
      expect(validateSafeInteger('1000000')).toEqual({ valid: true, value: 1_000_000 });
      expect(validateSafeInteger(Number.MAX_SAFE_INTEGER)).toEqual({
        valid: true,
        value: Number.MAX_SAFE_INTEGER,
      });
    });

    test('rejects non-numeric characters and decimals', () => {
      expect(validateSafeInteger('123.45').valid).toBe(false);
      expect(validateSafeInteger('abc').valid).toBe(false);
      expect(validateSafeInteger('12e5').valid).toBe(false);
    });

    test('rejects zero when allowZero is false (caps must be positive or cleared)', () => {
      const res = validateSafeInteger(0, { allowZero: false, fieldName: 'Hard token cap' });
      expect(res.valid).toBe(false);
      expect(res.error).toContain('cannot be zero');
    });

    test('accepts zero when allowZero is true', () => {
      expect(validateSafeInteger(0, { allowZero: true })).toEqual({ valid: true, value: 0 });
      expect(validateSafeInteger('0', { allowZero: true })).toEqual({ valid: true, value: 0 });
    });

    test('rejects integer precision loss exceeding Number.MAX_SAFE_INTEGER when allowSigned64 is false', () => {
      const unsafeStr = '9007199254740992'; // Number.MAX_SAFE_INTEGER + 1
      const res = validateSafeInteger(unsafeStr, { fieldName: 'Token cap' });
      expect(res.valid).toBe(false);
      expect(res.error).toContain('safe integer limit');
      expect(res.error).toContain('precision loss rejected');
    });

    test('validates signed64 integers across safe boundaries up to INT64_MAX without Number precision loss', () => {
      // MAX_SAFE - 1
      expect(validateSafeInteger(9007199254740990, { allowSigned64: true }))
        .toEqual({ valid: true, value: 9007199254740990 });
      // MAX_SAFE
      expect(validateSafeInteger(Number.MAX_SAFE_INTEGER, { allowSigned64: true }))
        .toEqual({ valid: true, value: 9007199254740991 });
      // MAX_SAFE + 1
      expect(validateSafeInteger('9007199254740992', { allowSigned64: true }))
        .toEqual({ valid: true, value: '9007199254740992' });
      // MAX_SAFE + 2
      expect(validateSafeInteger('9007199254740993', { allowSigned64: true }))
        .toEqual({ valid: true, value: '9007199254740993' });
      // INT64_MAX
      expect(validateSafeInteger('9223372036854775807', { allowSigned64: true }))
        .toEqual({ valid: true, value: '9223372036854775807' });

      // Canonicalizes leading zeros
      expect(validateSafeInteger('009007199254740992', { allowSigned64: true }))
        .toEqual({ valid: true, value: '9007199254740992' });

      // Unsafe JavaScript number is rejected even with allowSigned64
      expect(validateSafeInteger(Number.MAX_SAFE_INTEGER + 1, { allowSigned64: true }).valid).toBe(false);

      // Overflow beyond INT64_MAX
      const overflow = validateSafeInteger('9223372036854775808', { allowSigned64: true, fieldName: 'Cap' });
      expect(overflow.valid).toBe(false);
      expect(overflow.error).toContain('signed 64-bit');

      // Fraction, negative, and zero
      expect(validateSafeInteger('1.5', { allowSigned64: true }).valid).toBe(false);
      expect(validateSafeInteger('-1', { allowSigned64: true, min: 1 }).valid).toBe(false);
      expect(validateSafeInteger('0', { allowSigned64: true, allowZero: false }).valid).toBe(false);
    });

    test('exports recorded signed-64 and JS safe integer constants', () => {
      expect(JS_MAX_SAFE_INTEGER).toBe(9007199254740991);
      expect(MAX_SAFE_SIGNED_64).toBe(9223372036854775807n);
    });
  });

  describe('getMonitoringPolicy', () => {
    test('fetches monitoring policy for project', async () => {
      const mockPolicy: MonitoringPolicyResponse = {
        project_id: projectId,
        revision: 2,
        mode: 'checkpoint',
        warning_multiplier: 1.5,
        checkpoint_multiplier: 2.0,
        history_limit: 50,
        minimum_comparables: 20,
        window_days: null,
        hard_token_cap: null,
        hard_context_cap: null,
        manual_estimate_tokens: null,
        allow_cross_project_fallback: true,
      };
      vi.mocked(api).mockResolvedValue(mockPolicy);

      const result = await getMonitoringPolicy(projectId);
      expect(api).toHaveBeenCalledWith(`/projects/${projectId}/monitoring-policy`);
      expect(result).toEqual(mockPolicy);
    });

    test('returns null when server returns null/undefined (no policy configured)', async () => {
      vi.mocked(api).mockResolvedValue(null);
      const result = await getMonitoringPolicy(projectId);
      expect(result).toBeNull();
    });
  });

  describe('getMonitoringPolicyRevision', () => {
    test('fetches historical policy revision with validated integer revision', async () => {
      const mockPolicy: MonitoringPolicyResponse = {
        project_id: projectId,
        revision: 1,
        mode: 'report_only',
        warning_multiplier: 1.5,
        checkpoint_multiplier: 2.0,
        history_limit: 50,
        minimum_comparables: 20,
        window_days: 30,
        hard_token_cap: 500000,
        hard_context_cap: null,
        manual_estimate_tokens: null,
        allow_cross_project_fallback: false,
      };
      vi.mocked(api).mockResolvedValue(mockPolicy);

      const result = await getMonitoringPolicyRevision(projectId, 1);
      expect(api).toHaveBeenCalledWith(`/projects/${projectId}/monitoring-policy/1`);
      expect(result).toEqual(mockPolicy);
    });

    test('rejects unsafe or non-positive revision numbers', async () => {
      await expect(getMonitoringPolicyRevision(projectId, 0)).rejects.toThrow('Revision must be a valid positive safe integer');
      await expect(getMonitoringPolicyRevision(projectId, Number.MAX_SAFE_INTEGER + 1)).rejects.toThrow('Revision must be a valid positive safe integer');
    });
  });

  describe('reviseMonitoringPolicy', () => {
    test('creates first revision with expected revision zero and the supplied replay key', async () => {
      vi.mocked(mutate).mockResolvedValue({ revision: 1 } as MonitoringPolicyResponse);
      await reviseMonitoringPolicy(projectId, { expected_revision: 0 }, { idempotencyKey: 'same-attempt' });
      expect(mutate).toHaveBeenCalledWith(`/projects/${projectId}/monitoring-policy`,
        { expected_revision: 0 }, { idempotencyKey: 'same-attempt', method: 'POST' });
    });
    test('submits authenticated revision request with idempotency key and expected revision', async () => {
      const savedPolicy: MonitoringPolicyResponse = {
        project_id: projectId,
        revision: 3,
        mode: 'warn',
        warning_multiplier: 1.3,
        checkpoint_multiplier: 1.8,
        history_limit: 60,
        minimum_comparables: 15,
        window_days: 14,
        hard_token_cap: 2000000,
        hard_context_cap: null,
        manual_estimate_tokens: 300000,
        allow_cross_project_fallback: true,
        owner_action: {
          action: 'revise_monitoring_policy',
          actor_id: '22222222-2222-4222-8222-222222222222',
          affected_snapshot: 'a'.repeat(64),
          replayed: false,
          warnings: ['Threshold multiplier below recommended 1.5x'],
        },
      };
      vi.mocked(mutate).mockResolvedValue(savedPolicy);

      const request = {
        expected_revision: 2,
        mode: 'warn' as const,
        warning_multiplier: 1.3,
        checkpoint_multiplier: 1.8,
        history_limit: 60,
        minimum_comparables: 15,
        window_days: 14,
        hard_token_cap: 2000000,
        hard_context_cap: null,
        manual_estimate_tokens: 300000,
        allow_cross_project_fallback: true,
        note: 'Tuned multipliers for team sprint',
      };

      const result = await reviseMonitoringPolicy(projectId, request, { idempotencyKey: 'retry' });

      expect(mutate).toHaveBeenCalledWith(
        `/projects/${projectId}/monitoring-policy`,
        request,
        { idempotencyKey: 'retry', method: 'POST' }
      );
      expect(result).toEqual(savedPolicy);
    });

    test('generates an idempotency key if none provided', async () => {
      vi.mocked(mutate).mockResolvedValue({ revision: 2 } as MonitoringPolicyResponse);
      await reviseMonitoringPolicy(projectId, { expected_revision: 1 });
      expect(mutate).toHaveBeenCalledWith(
        `/projects/${projectId}/monitoring-policy`,
        { expected_revision: 1 },
        expect.objectContaining({
          method: 'POST',
          idempotencyKey: expect.stringMatching(/^[0-9a-f-]{36}$/),
        })
      );
    });

    test('rejects unsafe expected_revision', async () => {
      await expect(
        reviseMonitoringPolicy(projectId, { expected_revision: -1 })
      ).rejects.toThrow('Expected revision must be a valid nonnegative safe integer');
    });
    test('rejects negative, zero, fractional and unsafe cap values before transport', async () => {
      for (const cap of [-1, 0, 1.5, Number.MAX_SAFE_INTEGER + 1]) {
        await expect(reviseMonitoringPolicy(projectId, { expected_revision: 1, hard_token_cap: cap }))
          .rejects.toThrow('hard_token_cap must be a positive safe integer or null');
      }
      expect(mutate).not.toHaveBeenCalled();
    });

    test('accepts exact signed64 decimal string caps and estimates across safe integer boundaries up to INT64_MAX', async () => {
      vi.mocked(mutate).mockResolvedValue({ revision: 2 } as MonitoringPolicyResponse);
      const exactRequest: MonitoringPolicyRevisionRequest = {
        expected_revision: 1,
        hard_token_cap: '9007199254740992',
        hard_context_cap: '9007199254740993',
        manual_estimate_tokens: '9223372036854775807',
      };
      await reviseMonitoringPolicy(projectId, exactRequest);
      expect(mutate).toHaveBeenCalledWith(
        `/projects/${projectId}/monitoring-policy`,
        expect.objectContaining({
          expected_revision: 1,
          hard_token_cap: '9007199254740992',
          hard_context_cap: '9007199254740993',
          manual_estimate_tokens: '9223372036854775807',
        }),
        expect.anything()
      );
    });

    test('rejects overflow beyond INT64_MAX for all contract-supported fields and unsafe numbers', async () => {
      for (const field of ['hard_token_cap', 'hard_context_cap', 'manual_estimate_tokens', 'history_limit', 'minimum_comparables', 'window_days'] as const) {
        await expect(reviseMonitoringPolicy(projectId, { expected_revision: 1, [field]: '9223372036854775808' }))
          .rejects.toThrow(`${field} must be a positive safe integer or null`);
        await expect(reviseMonitoringPolicy(projectId, { expected_revision: 1, [field]: Number.MAX_SAFE_INTEGER + 1 }))
          .rejects.toThrow(`${field} must be a positive safe integer or null`);
        await expect(reviseMonitoringPolicy(projectId, { expected_revision: 1, [field]: 0 }))
          .rejects.toThrow(`${field} must be a positive safe integer or null`);
        await expect(reviseMonitoringPolicy(projectId, { expected_revision: 1, [field]: -1 }))
          .rejects.toThrow(`${field} must be a positive safe integer or null`);
      }
    });

    test('accepts exact signed64 history_limit, minimum_comparables, and window_days across safe boundaries and canonicalizes leading zeros', async () => {
      vi.mocked(mutate).mockResolvedValue({ revision: 2 } as MonitoringPolicyResponse);
      const request: MonitoringPolicyRevisionRequest = {
        expected_revision: 1,
        history_limit: '009007199254740993', // +2 with leading zeros
        minimum_comparables: '9007199254740992', // +1
        window_days: '9223372036854775807', // INT64_MAX
      };
      await reviseMonitoringPolicy(projectId, request);
      expect(mutate).toHaveBeenCalledWith(
        `/projects/${projectId}/monitoring-policy`,
        expect.objectContaining({
          expected_revision: 1,
          history_limit: '9007199254740993',
          minimum_comparables: '9007199254740992',
          window_days: '9223372036854775807',
        }),
        expect.anything()
      );
    });

    test('enforces minimum_comparables <= history_limit using BigInt across signed64 boundaries', async () => {
      await expect(reviseMonitoringPolicy(projectId, {
        expected_revision: 1,
        history_limit: '9007199254740992',
        minimum_comparables: '9007199254740993',
      })).rejects.toThrow('minimum_comparables cannot exceed history_limit');
    });
  });

  describe('getTaskUsageReport', () => {
    test('accepts exact signed64 windowDays up to INT64_MAX and rejects overflow', async () => {
      vi.mocked(api).mockResolvedValue({} as TaskUsageHistoryResponse);
      await getTaskUsageReport({ projectId, windowDays: '9007199254740992' as unknown as number });
      expect(api).toHaveBeenCalledWith(expect.stringContaining('window_days=9007199254740992'));

      await getTaskUsageReport({ projectId, windowDays: '9223372036854775807' as unknown as number });
      expect(api).toHaveBeenCalledWith(expect.stringContaining('window_days=9223372036854775807'));

      await expect(getTaskUsageReport({ projectId, windowDays: '9223372036854775808' as unknown as number }))
        .rejects.toThrow('Window days');
    });

    test('rejects unsafe or invalid report windows rather than widening the query', async () => {
      await expect(getTaskUsageReport({ projectId, windowDays: Number.MAX_SAFE_INTEGER + 1 })).rejects.toThrow('Window days');
      await expect(getTaskUsageReport({ projectId, windowDays: 0 })).rejects.toThrow('Window days');
      expect(api).not.toHaveBeenCalled();
    });
    test('queries task usage report with project_id and optional filters', async () => {
      const mockReport: TaskUsageHistoryResponse = {
        as_of: '2026-10-11T12:00:00Z',
        overview: {
          status: 'ready',
          sample_count: 35,
          eligible_count: 40,
          coverage_count: 35,
          measured_coverage: 0.875,
          mean_tokens: 150000,
          median_tokens: 140000,
          p90_tokens: 220000,
          reference_tokens: 140000,
          warning_threshold_tokens: 210000,
          checkpoint_threshold_tokens: 280000,
          cohort_source: 'project_exact',
          labels: ['exact_cohort'],
          project_ids: [projectId],
          phase_role_breakdown: [],
          excluded_count: 5,
        },
        filters: {
          project_ids: [projectId],
          history_limit: 50,
          minimum_comparables: 20,
        },
        comparable_references: {},
        by_task_type: {},
        by_model: {},
        by_provider: {},
        by_effort: {},
        by_project: {},
        excluded_outcomes: {},
        failed_attempt_count: 1,
      };
      vi.mocked(api).mockResolvedValue(mockReport);

      const result = await getTaskUsageReport({
        projectId,
        taskKind: 'implementation',
        model: 'gemini-3.8-flash',
        windowDays: 30,
      });

      expect(api).toHaveBeenCalledWith(
        expect.stringMatching(/^\/task-usage-report\?.*project_id=11111111-1111-4111-8111-111111111111/)
      );
      expect(api).toHaveBeenCalledWith(expect.stringContaining('task_kind=implementation'));
      expect(api).toHaveBeenCalledWith(expect.stringContaining('model=gemini-3.8-flash'));
      expect(api).toHaveBeenCalledWith(expect.stringContaining('window_days=30'));
      expect(result).toEqual(mockReport);
    });
  });

  test('lost response retries the identical monitoring request with its original key, then uses new keys', async () => {
    const saver = makeMonitoringPolicySaver(projectId);
    const request = { expected_revision: 2, mode: 'warn' as const };
    vi.mocked(mutate).mockRejectedValueOnce(new Error('response lost'))
      .mockResolvedValue({ revision: 3 } as MonitoringPolicyResponse);
    await expect(saver.save(request)).rejects.toThrow('response lost');
    const firstKey = vi.mocked(mutate).mock.calls[0][2]?.idempotencyKey;
    await saver.save(request);
    expect(vi.mocked(mutate).mock.calls[1][2]?.idempotencyKey).toBe(firstKey);
    await saver.save({ expected_revision: 3, mode: 'warn' });
    expect(vi.mocked(mutate).mock.calls[2][2]?.idempotencyKey).not.toBe(firstKey);
  });

  test('changed monitoring request after failed response gets a new key', async () => {
    const saver = makeMonitoringPolicySaver(projectId);
    vi.mocked(mutate).mockRejectedValueOnce(new Error('response lost'))
      .mockResolvedValue({ revision: 3 } as MonitoringPolicyResponse);
    await expect(saver.save({ expected_revision: 2, mode: 'warn' })).rejects.toThrow();
    await saver.save({ expected_revision: 2, mode: 'report_only' });
    expect(vi.mocked(mutate).mock.calls[1][2]?.idempotencyKey)
      .not.toBe(vi.mocked(mutate).mock.calls[0][2]?.idempotencyKey);
  });
});
