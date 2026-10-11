import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, test, vi } from 'vitest';
import { MonitoringPolicySummary } from './monitoring-policy-summary';
import { MonitoringPolicyEditor } from './monitoring-policy-editor';
import type { MonitoringPolicyResponse, TaskUsageHistoryResponse } from '@/lib/api/monitoring-policy';
import { ApiError } from '@/lib/api/client';

afterEach(cleanup);

describe('MonitoringPolicySummary and MonitoringPolicyEditor', () => {
  const projectId = '11111111-1111-4111-8111-111111111111';

  const samplePolicy: MonitoringPolicyResponse = {
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

  const sampleReportReady: TaskUsageHistoryResponse = {
    as_of: '2026-10-11T12:00:00Z',
    overview: {
      status: 'ready',
      sample_count: 42,
      eligible_count: 45,
      coverage_count: 42,
      measured_coverage: 0.933,
      mean_tokens: 185000,
      median_tokens: 170000,
      p90_tokens: 290000,
      reference_tokens: 170000,
      warning_threshold_tokens: 255000,
      checkpoint_threshold_tokens: 340000,
      cohort_source: 'project_exact',
      labels: ['exact_cohort'],
      project_ids: [projectId],
      phase_role_breakdown: [],
      excluded_count: 3,
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
    failed_attempt_count: 0,
  };

  describe('MonitoringPolicySummary', () => {
    test('renders default state when no policy is configured', () => {
      render(<MonitoringPolicySummary policy={null} onEdit={vi.fn()} />);
      expect(screen.getByText(/Task usage monitoring policy/i)).toBeInTheDocument();
      expect(screen.getByText(/No monitoring policy revision configured/i)).toBeInTheDocument();
      expect(screen.getByRole('button', { name: /Configure monitoring policy/i })).toBeInTheDocument();
    });

    test('renders active policy parameters and honest unbounded caps', () => {
      render(<MonitoringPolicySummary policy={samplePolicy} onEdit={vi.fn()} />);
      expect(screen.getByText(/Revision 2/i)).toBeInTheDocument();
      expect(screen.getByText(/Mode: checkpoint/i)).toBeInTheDocument();
      expect(screen.getByText(/Warning threshold: 1.5x/i)).toBeInTheDocument();
      expect(screen.getByText(/Checkpoint threshold: 2x/i)).toBeInTheDocument();
      expect(screen.getByText(/50 completions/i)).toBeInTheDocument();
      expect(screen.getByText(/20 samples/i)).toBeInTheDocument();
      expect(screen.getByText(/Cross-project fallback: Enabled/i)).toBeInTheDocument();
      expect(screen.getByText(/Hard token cap: Unbounded \(no cap\)/i)).toBeInTheDocument();
      expect(screen.getByText(/Hard context cap: Unbounded \(no cap\)/i)).toBeInTheDocument();
    });

    test('renders learned advisory usage distributions honestly when ready', () => {
      render(<MonitoringPolicySummary policy={samplePolicy} report={sampleReportReady} onEdit={vi.fn()} />);
      expect(screen.getByText(/185,000 tokens/i)).toBeInTheDocument();
      expect(screen.getByText(/170,000 tokens/i)).toBeInTheDocument();
      expect(screen.getByText(/290,000 tokens/i)).toBeInTheDocument();
      expect(screen.getByText(/Sample count:/i)).toBeInTheDocument();
      expect(screen.getByText(/42 eligible completions/i)).toBeInTheDocument();
      expect(screen.getByText(/Coverage:/i)).toBeInTheDocument();
      expect(screen.getByText(/93%/i)).toBeInTheDocument();
      expect(screen.getByText(/can mix task types, models, routes, and subjects/i)).toBeInTheDocument();
      expect(screen.queryByText(/255,000 tokens/i)).not.toBeInTheDocument();
      expect(screen.queryByText(/340,000 tokens/i)).not.toBeInTheDocument();
    });

    test('distinguishes measured zero from unknown in insufficient history warm-up', () => {
      const zeroReport: TaskUsageHistoryResponse = {
        ...sampleReportReady,
        overview: {
          ...sampleReportReady.overview,
          status: 'insufficient_history',
          sample_count: 5,
          mean_tokens: 0,
          median_tokens: 0,
          p90_tokens: 0,
        },
      };
      const { rerender } = render(<MonitoringPolicySummary policy={samplePolicy} report={zeroReport} onEdit={vi.fn()} />);
      expect(screen.getByText(/Insufficient history: 5 \/ 20 reported samples/i)).toBeInTheDocument();
      expect(screen.getByText(/Advisory reference unavailable/i)).toBeInTheDocument();
      expect(screen.getByText(/Measured average: 0 tokens \(measured zero\)/i)).toBeInTheDocument();

      const unknownReport: TaskUsageHistoryResponse = {
        ...sampleReportReady,
        overview: {
          ...sampleReportReady.overview,
          status: 'insufficient_history',
          sample_count: 0,
          mean_tokens: null,
          median_tokens: null,
          p90_tokens: null,
        },
      };
      rerender(<MonitoringPolicySummary policy={samplePolicy} report={unknownReport} onEdit={vi.fn()} />);
      expect(screen.getByText(/Measured average: Unknown \(no samples yet\)/i)).toBeInTheDocument();
      expect(screen.queryByText(/measured zero/i)).not.toBeInTheDocument();
    });

    test('displays owner action receipt with actor and warnings if present', () => {
      const policyWithReceipt: MonitoringPolicyResponse = {
        ...samplePolicy,
        owner_action: {
          action: 'revise_monitoring_policy',
          actor_id: 'owner-uuid-4444',
          affected_snapshot: 'sha256:test1234',
          replayed: true,
          warnings: ['Custom multiplier below default'],
        },
      };
      render(<MonitoringPolicySummary policy={policyWithReceipt} onEdit={vi.fn()} />);
      expect(screen.getByText(/actor: owner-uuid-4444/i)).toBeInTheDocument();
      expect(screen.getByText(/Custom multiplier below default/i)).toBeInTheDocument();
      expect(screen.getByText(/Replayed: Yes/i)).toBeInTheDocument();
    });
  });

  describe('MonitoringPolicyEditor', () => {
    test('creates absent policy at revision zero and advances from returned receipt', async () => {
      const save = vi.fn().mockResolvedValueOnce({ ...samplePolicy, revision: 1 }).mockResolvedValueOnce({ ...samplePolicy, revision: 2 });
      render(<MonitoringPolicyEditor projectId={projectId} initial={null} onSave={save} />);
      await userEvent.click(screen.getByRole('button', { name: /Save monitoring policy/i }));
      await waitFor(() => expect(save).toHaveBeenCalledWith(expect.objectContaining({ expected_revision: 0 })));
      await userEvent.click(screen.getByRole('button', { name: /Save monitoring policy/i }));
      await waitFor(() => expect(save).toHaveBeenLastCalledWith(expect.objectContaining({ expected_revision: 1 })));
    });
    test('initializes form with policy values and allows direct owner save', async () => {
      const save = vi.fn().mockResolvedValue({ ...samplePolicy, revision: 3 });
      render(<MonitoringPolicyEditor projectId={projectId} initial={samplePolicy} onSave={save} />);

      expect(screen.getByLabelText(/Monitoring mode/i)).toHaveValue('checkpoint');
      expect(screen.getByLabelText(/Warning multiplier/i)).toHaveValue(1.5);
      expect(screen.getByLabelText(/Checkpoint multiplier/i)).toHaveValue(2.0);
      expect(screen.getByLabelText(/History limit/i)).toHaveValue(50);
      expect(screen.getByLabelText(/Minimum comparables/i)).toHaveValue(20);
      expect(screen.getByLabelText(/Cross-project cohort fallback/i)).toBeChecked();

      await userEvent.click(screen.getByRole('button', { name: /Save monitoring policy/i }));

      expect(save).toHaveBeenCalledWith({
        expected_revision: 2,
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
        note: null,
      });
    });

    test('supports editing mode, multipliers, optional note, and custom limits', async () => {
      const save = vi.fn().mockResolvedValue({ ...samplePolicy, revision: 3 });
      render(<MonitoringPolicyEditor projectId={projectId} initial={samplePolicy} onSave={save} />);

      await userEvent.selectOptions(screen.getByLabelText(/Monitoring mode/i), 'warn');
      const warningInput = screen.getByLabelText(/Warning multiplier/i);
      await userEvent.clear(warningInput);
      await userEvent.type(warningInput, '1.4');

      const checkpointInput = screen.getByLabelText(/Checkpoint multiplier/i);
      await userEvent.clear(checkpointInput);
      await userEvent.type(checkpointInput, '2.2');

      const noteInput = screen.getByLabelText(/Optional owner note/i);
      await userEvent.type(noteInput, 'Tightened warning threshold');

      await userEvent.click(screen.getByRole('button', { name: /Save monitoring policy/i }));

      expect(save).toHaveBeenCalledWith(expect.objectContaining({
        mode: 'warn',
        warning_multiplier: 1.4,
        checkpoint_multiplier: 2.2,
        note: 'Tightened warning threshold',
      }));
    });

    test('validates that checkpoint multiplier cannot precede warning multiplier', async () => {
      const save = vi.fn();
      render(<MonitoringPolicyEditor projectId={projectId} initial={samplePolicy} onSave={save} />);

      const checkpointInput = screen.getByLabelText(/Checkpoint multiplier/i);
      await userEvent.clear(checkpointInput);
      await userEvent.type(checkpointInput, '1.2'); // warning is 1.5

      await userEvent.click(screen.getByRole('button', { name: /Save monitoring policy/i }));

      expect(save).not.toHaveBeenCalled();
      expect(await screen.findByText(/Checkpoint multiplier must be greater than or equal to warning multiplier/i)).toBeInTheDocument();
    });

    test('validates that minimum comparables cannot exceed history limit', async () => {
      const save = vi.fn();
      render(<MonitoringPolicyEditor projectId={projectId} initial={samplePolicy} onSave={save} />);

      const minInput = screen.getByLabelText(/Minimum comparables/i);
      await userEvent.clear(minInput);
      await userEvent.type(minInput, '100'); // history limit is 50

      await userEvent.click(screen.getByRole('button', { name: /Save monitoring policy/i }));

      expect(save).not.toHaveBeenCalled();
      expect(await screen.findByText(/Minimum comparables cannot exceed history limit/i)).toBeInTheDocument();
    });

    test('rejects zero and precision loss in advanced hard caps, while valid caps save and clear to null', async () => {
      const save = vi.fn().mockResolvedValue({ ...samplePolicy, revision: 3 });
      render(<MonitoringPolicyEditor projectId={projectId} initial={samplePolicy} onSave={save} />);

      // Reveal advanced caps disclosure
      await userEvent.click(screen.getByText(/Advanced hard caps and manual estimates/i));

      const tokenCapInput = screen.getByLabelText(/Optional hard token cap/i);

      // Try zero (zero is invalid!)
      await userEvent.clear(tokenCapInput);
      await userEvent.type(tokenCapInput, '0');
      await userEvent.click(screen.getByRole('button', { name: /Save monitoring policy/i }));
      expect(save).not.toHaveBeenCalled();
      expect(screen.getByText(/cannot be zero/i)).toBeInTheDocument();

      // Try precision loss (> Number.MAX_SAFE_INTEGER)
      await userEvent.clear(tokenCapInput);
      await userEvent.type(tokenCapInput, '9007199254740993');
      await userEvent.click(screen.getByRole('button', { name: /Save monitoring policy/i }));
      expect(save).not.toHaveBeenCalled();
      expect(screen.getByText(/safe integer limit/i)).toBeInTheDocument();

      // Valid safe cap
      await userEvent.clear(tokenCapInput);
      await userEvent.type(tokenCapInput, '5000000');

      const manualEstimateInput = screen.getByLabelText(/Manual estimate tokens/i);
      await userEvent.type(manualEstimateInput, '250000');

      await userEvent.click(screen.getByRole('button', { name: /Save monitoring policy/i }));
      expect(save).toHaveBeenCalledWith(expect.objectContaining({
        hard_token_cap: 5_000_000,
        manual_estimate_tokens: 250_000,
      }));

      // Now clear token cap to null (unbounded)
      cleanup();
      const saveCleared = vi.fn().mockResolvedValue({ ...samplePolicy, revision: 4 });
      render(<MonitoringPolicyEditor projectId={projectId} initial={{ ...samplePolicy, hard_token_cap: 5000000 }} onSave={saveCleared} />);
      await userEvent.click(screen.getByText(/Advanced hard caps and manual estimates/i));
      const capInput2 = screen.getByLabelText(/Optional hard token cap/i);
      expect(capInput2).toHaveValue('5000000');
      await userEvent.clear(capInput2);
      await userEvent.click(screen.getByRole('button', { name: /Save monitoring policy/i }));
      expect(saveCleared).toHaveBeenCalledWith(expect.objectContaining({
        hard_token_cap: null,
      }));
    });
    test('rejects negative cap and manual estimate without saving', async () => {
      const save = vi.fn();
      render(<MonitoringPolicyEditor projectId={projectId} initial={samplePolicy} onSave={save} />);
      await userEvent.click(screen.getByText(/Advanced hard caps and manual estimates/i));
      await userEvent.type(screen.getByLabelText(/Optional hard token cap/i), '-5');
      await userEvent.click(screen.getByRole('button', { name: /Save monitoring policy/i }));
      expect(save).not.toHaveBeenCalled();
      await userEvent.clear(screen.getByLabelText(/Optional hard token cap/i));
      await userEvent.type(screen.getByLabelText(/Manual estimate tokens/i), '-1');
      await userEvent.click(screen.getByRole('button', { name: /Save monitoring policy/i }));
      expect(save).not.toHaveBeenCalled();
    });

    test('handles 409 stale revision conflict and allows owner recovery without losing edits', async () => {
      let attempts = 0;
      const save = vi.fn().mockImplementation(async () => {
        attempts++;
        if (attempts === 1) throw new ApiError(409, 'stale-project-policy');
        return { ...samplePolicy, revision: 4 };
      });

      const refresh = vi.fn().mockResolvedValue({ ...samplePolicy, revision: 8 });
      render(<MonitoringPolicyEditor projectId={projectId} initial={samplePolicy} onSave={save} onRefresh={refresh} />);

      const noteInput = screen.getByLabelText(/Optional owner note/i);
      await userEvent.type(noteInput, 'Keep this note across conflict');

      await userEvent.click(screen.getByRole('button', { name: /Save monitoring policy/i }));

      expect(await screen.findByText(/Monitoring policy revision conflict/i)).toBeInTheDocument();
      // Owner edit is preserved
      expect(screen.getByLabelText(/Optional owner note/i)).toHaveValue('Keep this note across conflict');

      // Re-submitting with refreshed revision succeeds
      await userEvent.click(screen.getByRole('button', { name: /Load current revision and keep edits/i }));
      expect(refresh).toHaveBeenCalledOnce();
      await userEvent.click(screen.getByRole('button', { name: /Save monitoring policy/i }));
      expect(save).toHaveBeenCalledTimes(2);
      expect(save).toHaveBeenLastCalledWith(expect.objectContaining({ expected_revision: 8, note: 'Keep this note across conflict' }));
    });

    test('rebases untouched concurrent policy fields while retaining a note-only owner edit', async () => {
      const save = vi.fn().mockRejectedValueOnce(new ApiError(409, 'stale-project-policy'))
        .mockResolvedValue({ ...samplePolicy, revision: 9 });
      const latest = { ...samplePolicy, revision: 8, mode: 'warn', history_limit: 60,
        minimum_comparables: 25, hard_token_cap: 900000, hard_context_cap: 200000,
        warning_multiplier: 1.7, checkpoint_multiplier: 2.2 } as const;
      const refresh = vi.fn().mockResolvedValue(latest);
      render(<MonitoringPolicyEditor projectId={projectId} initial={samplePolicy} onSave={save} onRefresh={refresh} />);
      await userEvent.type(screen.getByLabelText(/Optional owner note/i), 'Only my note');
      await userEvent.click(screen.getByRole('button', { name: /Save monitoring policy/i }));
      await screen.findByText(/Monitoring policy revision conflict/i);
      await userEvent.click(screen.getByRole('button', { name: /Load current revision and keep edits/i }));
      await userEvent.click(screen.getByText(/Advanced hard caps and manual estimates/i));
      expect(screen.getByLabelText(/Optional hard token cap/i)).toHaveValue('900000');
      expect(screen.getByLabelText(/Optional hard context cap/i)).toHaveValue('200000');
      expect(screen.getByLabelText(/Monitoring mode/i)).toHaveValue('warn');
      expect(screen.getByLabelText(/History limit/i)).toHaveValue(60);
      expect(screen.getByLabelText(/Optional owner note/i)).toHaveValue('Only my note');
      await userEvent.click(screen.getByRole('button', { name: /Save monitoring policy/i }));
      expect(save).toHaveBeenLastCalledWith(expect.objectContaining({
        expected_revision: 8, hard_token_cap: 900000, hard_context_cap: 200000,
        mode: 'warn', history_limit: 60, minimum_comparables: 25,
        warning_multiplier: 1.7, checkpoint_multiplier: 2.2, note: 'Only my note',
      }));
    });

    test('keeps owner-cleared cap and raw invalid edit while rebasing untouched fields', async () => {
      const save = vi.fn().mockRejectedValueOnce(new ApiError(409, 'stale-project-policy'))
        .mockResolvedValue({ ...samplePolicy, revision: 9 });
      const refresh = vi.fn().mockResolvedValue({ ...samplePolicy, revision: 8,
        hard_token_cap: 700000, hard_context_cap: 250000, history_limit: 70 });
      render(<MonitoringPolicyEditor projectId={projectId} initial={{ ...samplePolicy, hard_token_cap: 500000 }} onSave={save} onRefresh={refresh} />);
      await userEvent.click(screen.getByText(/Advanced hard caps and manual estimates/i));
      await userEvent.clear(screen.getByLabelText(/Optional hard token cap/i));
      await userEvent.click(screen.getByRole('button', { name: /Save monitoring policy/i }));
      await screen.findByText(/Monitoring policy revision conflict/i);
      await userEvent.type(screen.getByLabelText(/Manual estimate tokens/i), '1.5');
      await userEvent.click(screen.getByRole('button', { name: /Load current revision and keep edits/i }));
      expect(screen.getByLabelText(/Optional hard token cap/i)).toHaveValue('');
      expect(screen.getByLabelText(/Optional hard context cap/i)).toHaveValue('250000');
      expect(screen.getByLabelText(/Manual estimate tokens/i)).toHaveValue('1.5');
      expect(screen.getByLabelText(/History limit/i)).toHaveValue(70);
      await userEvent.click(screen.getByRole('button', { name: /Save monitoring policy/i }));
      expect(save).toHaveBeenCalledTimes(1);
      await userEvent.clear(screen.getByLabelText(/Manual estimate tokens/i));
      await userEvent.click(screen.getByRole('button', { name: /Save monitoring policy/i }));
      expect(save).toHaveBeenLastCalledWith(expect.objectContaining({ expected_revision: 8,
        hard_token_cap: null, hard_context_cap: 250000, history_limit: 70 }));
    });

    test('updates the authoritative baseline after success before a later conflict', async () => {
      const save = vi.fn().mockResolvedValueOnce({ ...samplePolicy, revision: 3, hard_token_cap: 600000 })
        .mockRejectedValueOnce(new ApiError(409, 'stale-project-policy'))
        .mockResolvedValue({ ...samplePolicy, revision: 5 });
      const refresh = vi.fn().mockResolvedValue({ ...samplePolicy, revision: 4, hard_token_cap: 800000 });
      render(<MonitoringPolicyEditor projectId={projectId} initial={samplePolicy} onSave={save} onRefresh={refresh} />);
      await userEvent.type(screen.getByLabelText(/Optional owner note/i), 'Keep note');
      await userEvent.click(screen.getByRole('button', { name: /Save monitoring policy/i }));
      await userEvent.click(screen.getByRole('button', { name: /Save monitoring policy/i }));
      await screen.findByText(/Monitoring policy revision conflict/i);
      await userEvent.click(screen.getByRole('button', { name: /Load current revision and keep edits/i }));
      await userEvent.click(screen.getByText(/Advanced hard caps and manual estimates/i));
      expect(screen.getByLabelText(/Optional hard token cap/i)).toHaveValue('800000');
      await userEvent.click(screen.getByRole('button', { name: /Save monitoring policy/i }));
      expect(save).toHaveBeenLastCalledWith(expect.objectContaining({ expected_revision: 4,
        hard_token_cap: 800000, note: 'Keep note' }));
    });

    test('rebases repeatedly from a null initial policy without dropping an owner note', async () => {
      const save = vi.fn().mockRejectedValueOnce(new ApiError(409, 'stale-project-policy'))
        .mockRejectedValueOnce(new ApiError(409, 'stale-project-policy'))
        .mockResolvedValue({ ...samplePolicy, revision: 4 });
      const refresh = vi.fn().mockResolvedValueOnce({ ...samplePolicy, revision: 1, hard_token_cap: 300000 })
        .mockResolvedValueOnce({ ...samplePolicy, revision: 3, hard_token_cap: 700000, window_days: 14 });
      render(<MonitoringPolicyEditor projectId={projectId} initial={null} onSave={save} onRefresh={refresh} />);
      await userEvent.type(screen.getByLabelText(/Optional owner note/i), 'New policy note');
      await userEvent.click(screen.getByRole('button', { name: /Save monitoring policy/i }));
      await screen.findByText(/Monitoring policy revision conflict/i);
      await userEvent.click(screen.getByRole('button', { name: /Load current revision and keep edits/i }));
      await userEvent.click(screen.getByRole('button', { name: /Save monitoring policy/i }));
      await screen.findByText(/Monitoring policy revision conflict/i);
      await userEvent.click(screen.getByRole('button', { name: /Load current revision and keep edits/i }));
      await userEvent.click(screen.getByRole('button', { name: /Save monitoring policy/i }));
      expect(save.mock.calls.map(([request]) => request.expected_revision)).toEqual([0, 1, 3]);
      expect(save).toHaveBeenLastCalledWith(expect.objectContaining({ hard_token_cap: 700000,
        window_days: 14, note: 'New policy note' }));
    });

    test('keeps draft and old revision while current-policy read fails, then rebases on retry', async () => {
      const save = vi.fn().mockRejectedValueOnce(new ApiError(409, 'stale-project-policy'))
        .mockResolvedValue({ ...samplePolicy, revision: 5 });
      let completeRead!: (value: MonitoringPolicyResponse) => void;
      const refresh = vi.fn().mockRejectedValueOnce(new Error('Policy read unavailable'))
        .mockImplementationOnce(() => new Promise<MonitoringPolicyResponse>(resolve => { completeRead = resolve; }));
      render(<MonitoringPolicyEditor projectId={projectId} initial={samplePolicy} onSave={save} onRefresh={refresh} />);
      await userEvent.type(screen.getByLabelText(/Optional owner note/i), 'Pending note');
      await userEvent.click(screen.getByRole('button', { name: /Save monitoring policy/i }));
      await screen.findByText(/Monitoring policy revision conflict/i);
      await userEvent.click(screen.getByRole('button', { name: /Load current revision and keep edits/i }));
      expect(await screen.findByText('Policy read unavailable')).toBeInTheDocument();
      expect(screen.getByRole('heading', { name: /Revision 2/i })).toBeInTheDocument();
      expect(screen.getByLabelText(/Optional owner note/i)).toHaveValue('Pending note');
      await userEvent.click(screen.getByRole('button', { name: /Load current revision and keep edits/i }));
      expect(screen.getByRole('button', { name: /Loading current revision/i })).toBeDisabled();
      expect(screen.getByLabelText(/Optional owner note/i)).toHaveValue('Pending note');
      completeRead({ ...samplePolicy, revision: 4, hard_token_cap: 750000 });
      await screen.findByRole('heading', { name: /Revision 4/i });
      await userEvent.click(screen.getByRole('button', { name: /Save monitoring policy/i }));
      expect(save).toHaveBeenLastCalledWith(expect.objectContaining({ expected_revision: 4,
        hard_token_cap: 750000, note: 'Pending note' }));
    });

    test('prevents duplicate submissions while save is in flight', async () => {
      let completeSave!: (value: MonitoringPolicyResponse) => void;
      const save = vi.fn().mockReturnValue(new Promise<MonitoringPolicyResponse>(resolve => { completeSave = resolve; }));

      render(<MonitoringPolicyEditor projectId={projectId} initial={samplePolicy} onSave={save} />);

      const saveBtn = screen.getByRole('button', { name: /Save monitoring policy/i });
      await userEvent.click(saveBtn);

      expect(saveBtn).toBeDisabled();
      expect(saveBtn).toHaveTextContent(/Saving…/i);
      expect(save).toHaveBeenCalledTimes(1);

      // Attempt second click while busy
      fireEvent.click(saveBtn);
      expect(save).toHaveBeenCalledTimes(1);

      completeSave(samplePolicy);
      await waitFor(() => expect(saveBtn).not.toBeDisabled());
    });
  });
});
