import { describe, expect, test } from 'vitest';
import {
  describeAuthoringOutcome,
  formatUtcTimestamp,
  formatSubstantiveOperation,
  readableDiagnosticEventLabel,
  readableToolName,
} from './authoring-presentation';
import type { AuthoringOutcome } from '@/hooks/epics/types';

describe('describeAuthoringOutcome diagnostics', () => {
  const baseOutcome: AuthoringOutcome = {
    schema_version: 1,
    job_id: 'job-123',
    job_version: 1,
    state: 'running',
    process_settled: false,
    usage_known: null,
  };

  test.each([
    ['client_initialize', 'Preparing AI client', 'client initialization'],
    ['account_read', 'Preparing AI client', 'account check'],
    ['model_list', 'Preparing AI client', 'model availability check'],
    ['config_read', 'Preparing AI client', 'client configuration check'],
    ['thread_start', 'Preparing AI client', 'conversation setup'],
    ['turn_start', 'Preparing AI client', 'turn setup'],
    ['tool_reply', 'Returning repository result', 'repository result delivery'],
  ])('identifies the actual client wait stage %s', (operation, title, label) => {
    const event = {
      sequence: 1, kind: 'provider_wait' as const, operation,
      occurred_at: '2026-10-09T20:22:08Z', elapsed_ms: 2000,
    };
    const outcome = { ...baseOutcome, diagnostics: {
      attempt_id: 'attempt-setup', dropped_events: 0, events: [event],
    } };
    const presentation = describeAuthoringOutcome(outcome);
    expect(presentation.title).toBe(title);
    expect(presentation.description.toLowerCase()).toContain(label);
    expect(presentation.description).not.toContain('model response');
    expect(readableDiagnosticEventLabel(event).toLowerCase()).toContain(label);
    expect(formatSubstantiveOperation(event)).toContain(label);
    expect(presentation.isWaiting).toBe(true);
  });

  test('retains provider retry identity when the operation is provider_receive', () => {
    expect(formatSubstantiveOperation({
      sequence: 1, kind: 'provider_retry', operation: 'provider_receive',
      occurred_at: '2026-10-09T20:22:08Z', elapsed_ms: 2000,
    })).toContain('model retry');
  });

  test.each(['process_settled', 'attempt_stopped'] as const)(
    'shows finalization after %s without claiming the process is still stopping', kind => {
      const description = describeAuthoringOutcome({ ...baseOutcome, diagnostics: {
        attempt_id: 'attempt-buffered', dropped_events: 0, events: [{
          sequence: 2, kind, occurred_at: '2026-10-09T20:22:08Z', elapsed_ms: 2000,
        }],
      } });
      expect(description.title).toBe('Finalizing assistant job');
      expect(description.description).not.toContain('settlement in progress');
      expect(description.isExecuting).toBe(false);
    }
  );

  test.each(['settlement_started', 'provider_wait'] as const)(
    'uses confirmed settlement over a late %s event', kind => {
      const presentation = describeAuthoringOutcome({ ...baseOutcome, process_settled: true, diagnostics: {
        attempt_id: 'attempt-settled', dropped_events: 0, events: [
          { sequence: 1, kind: 'process_settled', occurred_at: '2026-10-09T20:22:08Z', elapsed_ms: 2000 },
          { sequence: 2, kind, operation: 'process_settlement', occurred_at: '2026-10-09T20:22:09Z', elapsed_ms: 3000 },
        ],
      } });
      expect(presentation.title).toBe('Finalizing assistant job');
      expect(presentation.description).toContain('process stopped');
      expect(presentation.description).not.toContain('settlement in progress');
      expect(presentation.isExecuting).toBe(false);
    }
  );

  test('distinguishes repository read in progress when tool_started event is present', () => {
    const outcome: AuthoringOutcome = {
      ...baseOutcome,
      diagnostics: {
        attempt_id: 'attempt-1',
        dropped_events: 0,
        events: [
          {
            sequence: 0,
            kind: 'launch',
            occurred_at: '2026-10-09T20:22:06Z',
            elapsed_ms: 0,
          },
          {
            sequence: 1,
            kind: 'tool_started',
            operation: 'repository_read',
            tool_name: 'list_files',
            occurred_at: '2026-10-09T20:22:07Z',
            elapsed_ms: 1200,
          },
        ],
      },
    };

    const desc = describeAuthoringOutcome(outcome);
    expect(desc.title).toBe('Reading repository');
    expect(desc.description).toContain('List files');
    expect(desc.description).toContain('1,200 ms');
    expect(desc.description).toContain('Recorded at 1,200 ms into the attempt');
    expect(desc.isExecuting).toBe(true);
    expect(desc.isWaiting).toBe(false);
  });

  test('distinguishes waiting for model when provider_wait or tool_completed is present', () => {
    const waitOutcome: AuthoringOutcome = {
      ...baseOutcome,
      diagnostics: {
        attempt_id: 'attempt-1',
        dropped_events: 0,
        events: [
          {
            sequence: 0,
            kind: 'provider_wait',
            operation: 'provider_receive',
            occurred_at: '2026-10-09T20:22:08Z',
            elapsed_ms: 2500,
          },
        ],
      },
    };
    const desc = describeAuthoringOutcome(waitOutcome);
    expect(desc.title).toBe('Waiting for model');
    expect(desc.description).toContain('2,500 ms');
    expect(desc.isExecuting).toBe(true);
    expect(desc.isWaiting).toBe(true);
    expect(desc.tone).toBe('info');

    const completedOutcome: AuthoringOutcome = {
      ...baseOutcome,
      diagnostics: {
        attempt_id: 'attempt-1',
        dropped_events: 0,
        events: [
          {
            sequence: 1,
            kind: 'tool_completed',
            operation: 'repository_read',
            tool_name: 'read_file',
            duration_ms: 350,
            occurred_at: '2026-10-09T20:22:09Z',
            elapsed_ms: 3200,
          },
        ],
      },
    };
    const completedDesc = describeAuthoringOutcome(completedOutcome);
    expect(completedDesc.title).toBe('Waiting for model');
    expect(completedDesc.isWaiting).toBe(true);
  });

  test('distinguishes retry and error states', () => {
    const retryOutcome: AuthoringOutcome = {
      ...baseOutcome,
      diagnostics: {
        attempt_id: 'attempt-1',
        dropped_events: 0,
        events: [
          {
            sequence: 0,
            kind: 'provider_retry',
            occurred_at: '2026-10-09T20:22:15Z',
            elapsed_ms: 9000,
          },
        ],
      },
    };
    const retryDesc = describeAuthoringOutcome(retryOutcome);
    expect(retryDesc.title).toBe('Waiting for model retry');
    expect(retryDesc.tone).toBe('warning');
    expect(retryDesc.isWaiting).toBe(true);

    const errorOutcome: AuthoringOutcome = {
      ...baseOutcome,
      diagnostics: {
        attempt_id: 'attempt-1',
        dropped_events: 0,
        events: [
          {
            sequence: 0,
            kind: 'provider_error',
            failure: 'quota_exhausted',
            occurred_at: '2026-10-09T20:22:20Z',
            elapsed_ms: 14000,
          },
        ],
      },
    };
    const errorDesc = describeAuthoringOutcome(errorOutcome);
    expect(errorDesc.title).toBe('Model provider error');
    expect(errorDesc.description).toContain('Quota exhausted');
    expect(errorDesc.tone).toBe('warning');

    const toolFailedOutcome: AuthoringOutcome = {
      ...baseOutcome,
      diagnostics: {
        attempt_id: 'attempt-1',
        dropped_events: 0,
        events: [
          {
            sequence: 0,
            kind: 'tool_failed',
            tool_name: 'search',
            occurred_at: '2026-10-09T20:22:20Z',
            elapsed_ms: 14000,
          },
        ],
      },
    };
    const toolFailedDesc = describeAuthoringOutcome(toolFailedOutcome);
    expect(toolFailedDesc.title).toBe('Repository read failed');
    expect(toolFailedDesc.description).toContain('Search');
    expect(toolFailedDesc.tone).toBe('warning');
  });

  test('distinguishes proposal received and stopping/settlement', () => {
    const proposalOutcome: AuthoringOutcome = {
      ...baseOutcome,
      diagnostics: {
        attempt_id: 'attempt-1',
        dropped_events: 0,
        events: [
          {
            sequence: 2,
            kind: 'proposal_received',
            occurred_at: '2026-10-09T20:23:00Z',
            elapsed_ms: 54000,
          },
        ],
      },
    };
    const proposalDesc = describeAuthoringOutcome(proposalOutcome);
    expect(proposalDesc.title).toBe('Proposal received');
    expect(proposalDesc.isExecuting).toBe(true);
    expect(proposalDesc.isWaiting).toBe(false);

    const settlementOutcome: AuthoringOutcome = {
      ...baseOutcome,
      diagnostics: {
        attempt_id: 'attempt-1',
        dropped_events: 0,
        events: [
          {
            sequence: 3,
            kind: 'settlement_started',
            occurred_at: '2026-10-09T20:23:05Z',
            elapsed_ms: 59000,
          },
        ],
      },
    };
    const settlementDesc = describeAuthoringOutcome(settlementOutcome);
    expect(settlementDesc.title).toBe('Stopping assistant process');
    expect(settlementDesc.tone).toBe('warning');
    expect(settlementDesc.isWaiting).toBe(true);
  });

  test('older outcomes lacking diagnostics work unchanged', () => {
    const outcome: AuthoringOutcome = {
      ...baseOutcome,
      diagnostics: null,
    };
    const desc = describeAuthoringOutcome(outcome);
    expect(desc.title).toBe('Assistant drafting proposal');
    expect(desc.description).toBe('The assistant is actively drafting a proposal based on your conversation.');
    expect(desc.isExecuting).toBe(true);
    expect(desc.isTerminal).toBe(false);

    const cancelledOutcome: AuthoringOutcome = {
      ...baseOutcome,
      state: 'cancelled',
      diagnostics: null,
    };
    const cancelledDesc = describeAuthoringOutcome(cancelledOutcome);
    expect(cancelledDesc.title).toBe('Assistant job cancelled');
    expect(cancelledDesc.description).toBe('The assistant job was cancelled.');
    expect(cancelledDesc.isTerminal).toBe(true);
    expect(cancelledDesc.isExecuting).toBe(false);
  });

  test('stale state remains last-known without asserting live execution', () => {
    const outcome: AuthoringOutcome = {
      ...baseOutcome,
      diagnostics: {
        attempt_id: 'attempt-1',
        dropped_events: 0,
        events: [
          {
            sequence: 1,
            kind: 'tool_started',
            tool_name: 'read_file',
            occurred_at: '2026-10-09T20:22:07Z',
            elapsed_ms: 1200,
          },
        ],
      },
    };
    const desc = describeAuthoringOutcome(outcome, { stale: true });
    expect(desc.state).toBe('stale');
    expect(desc.title).toContain('Last known state: Running');
    expect(desc.isExecuting).toBe(false);
    expect(desc.tone).toBe('warning');
  });

  test('on timeout explains actual stop reason and last substantive operation with timing', () => {
    const settledTimeout: AuthoringOutcome = {
      ...baseOutcome,
      state: 'failed',
      failure: 'timeout',
      process_settled: true,
      diagnostics: {
        attempt_id: 'attempt-timeout',
        dropped_events: 0,
        events: [
          {
            sequence: 0,
            kind: 'launch',
            occurred_at: '2026-10-09T20:22:00Z',
            elapsed_ms: 0,
          },
          {
            sequence: 1,
            kind: 'tool_started',
            operation: 'repository_read',
            tool_name: 'read_file',
            occurred_at: '2026-10-09T20:22:45Z',
            elapsed_ms: 45000,
          },
          {
            sequence: 2,
            kind: 'settlement_started',
            operation: 'process_settlement',
            occurred_at: '2026-10-09T20:27:00Z',
            elapsed_ms: 300000,
          },
          {
            sequence: 3,
            kind: 'process_settled',
            operation: 'process_settlement',
            occurred_at: '2026-10-09T20:27:01Z',
            elapsed_ms: 300059,
          },
        ],
      },
    };

    const settledDesc = describeAuthoringOutcome(settledTimeout);
    expect(settledDesc.state).toBe('failed');
    expect(settledDesc.isTerminal).toBe(true);
    expect(settledDesc.isExecuting).toBe(false);
    expect(settledDesc.description).toContain('The assistant timed out and its process stopped.');
    expect(settledDesc.description).toContain('Last substantive operation: repository read (Read file) at 45,000 ms.');

    const pendingTimeout: AuthoringOutcome = {
      ...settledTimeout,
      process_settled: false,
    };
    const pendingDesc = describeAuthoringOutcome(pendingTimeout);
    expect(pendingDesc.description).toContain('The assistant timed out. The process is still stopping or awaiting settlement.');
    expect(pendingDesc.description).toContain('Last substantive operation: repository read (Read file) at 45,000 ms.');
  });

  test('on cancellation explains actual stop reason and last substantive operation with timing', () => {
    const cancelledOutcome: AuthoringOutcome = {
      ...baseOutcome,
      state: 'cancelled',
      process_settled: true,
      diagnostics: {
        attempt_id: 'attempt-cancel',
        dropped_events: 0,
        events: [
          {
            sequence: 0,
            kind: 'provider_wait',
            operation: 'provider_receive',
            occurred_at: '2026-10-09T20:20:00Z',
            elapsed_ms: 120000,
          },
          {
            sequence: 1,
            kind: 'process_settled',
            occurred_at: '2026-10-09T20:21:00Z',
            elapsed_ms: 180000,
          },
        ],
      },
    };

    const desc = describeAuthoringOutcome(cancelledOutcome);
    expect(desc.state).toBe('cancelled');
    expect(desc.isTerminal).toBe(true);
    expect(desc.isExecuting).toBe(false);
    expect(desc.description).toContain('The assistant job was cancelled and its process stopped.');
    expect(desc.description).toContain('Last substantive operation: waiting for model at 120,000 ms.');
  });

  test('terminal states never show active execution even if last event is tool_started', () => {
    const terminalWithToolStarted: AuthoringOutcome = {
      ...baseOutcome,
      state: 'failed',
      failure: 'timeout',
      process_settled: false,
      diagnostics: {
        attempt_id: 'attempt-stuck',
        dropped_events: 0,
        events: [
          {
            sequence: 0,
            kind: 'tool_started',
            tool_name: 'search',
            occurred_at: '2026-10-09T20:22:00Z',
            elapsed_ms: 290000,
          },
        ],
      },
    };

    const desc = describeAuthoringOutcome(terminalWithToolStarted);
    expect(desc.isExecuting).toBe(false);
    expect(desc.isTerminal).toBe(true);
  });

  test('formatting helpers format timestamps, tool labels and operations sanitarily', () => {
    expect(readableToolName('list_files')).toBe('List files');
    expect(readableToolName('read_file')).toBe('Read file');
    expect(readableToolName('search')).toBe('Search');
    expect(readableToolName('read_instructions')).toBe('Read instructions');

    expect(formatUtcTimestamp('2026-10-09T20:22:06Z')).toBe('2026-10-09 20:22:06 UTC');

    const event = {
      sequence: 1,
      kind: 'tool_completed' as const,
      operation: 'repository_read',
      tool_name: 'read_instructions',
      duration_ms: 450,
      occurred_at: '2026-10-09T20:22:06Z',
      elapsed_ms: 5000,
    };
    expect(readableDiagnosticEventLabel(event)).toBe('Repository read completed (Read instructions)');
    expect(formatSubstantiveOperation(event)).toBe('repository read (Read instructions) completed at 5,000 ms (duration: 450 ms)');
  });
});
