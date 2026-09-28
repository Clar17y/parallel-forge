import { describe, expect, test } from 'vitest';
import {
  describeActivity,
  formatRelativeTime,
  groupRecentActivity,
} from './activity-presentation';

describe('activity-presentation', () => {
  test('describes read file tool completion with path subject and duration', () => {
    const event = {
      event_type: 'tool_call.completed',
      payload: {
        tool_name: 'repository.read_file',
        path: 'src/main.ts',
        status: 'succeeded',
        duration_ms: 45,
      },
    };
    const desc = describeActivity(event);
    expect(desc.title).toBe('Read src/main.ts');
    expect(desc.status).toBe('succeeded');
    expect(desc.outcome).toContain('45ms');
    expect(desc.subject).toBe('src/main.ts');
  });

  test('describes write, delete, rename, list, and search tools', () => {
    expect(
      describeActivity({
        event_type: 'tool_call.completed',
        payload: { tool_name: 'repository.write_file', path: 'new.ts', created: true, status: 'succeeded' },
      }).title,
    ).toBe('Created new.ts');

    expect(
      describeActivity({
        event_type: 'tool_call.completed',
        payload: { tool_name: 'repository.write_file', path: 'edit.ts', created: false, status: 'succeeded' },
      }).title,
    ).toBe('Wrote edit.ts');

    expect(
      describeActivity({
        event_type: 'tool_call.completed',
        payload: { tool_name: 'repository.delete_file', path: 'old.ts', status: 'succeeded' },
      }).title,
    ).toBe('Deleted old.ts');

    expect(
      describeActivity({
        event_type: 'tool_call.completed',
        payload: { tool_name: 'repository.rename_file', source: 'a.ts', destination: 'b.ts', status: 'succeeded' },
      }).title,
    ).toBe('Renamed a.ts → b.ts');

    expect(
      describeActivity({
        event_type: 'tool_call.completed',
        payload: { tool_name: 'repository.list_files', entry_count: 42, status: 'succeeded' },
      }).title,
    ).toBe('Listed files (42 entries)');

    expect(
      describeActivity({
        event_type: 'tool_call.completed',
        payload: { tool_name: 'repository.search', match_count: 5, status: 'succeeded' },
      }).title,
    ).toBe('Searched repository (5 matches)');

    expect(
      describeActivity({
        event_type: 'tool_call.completed',
        payload: { tool_name: 'git.commit', commit_sha: '1234567890abcdef1234567890abcdef12345678', status: 'succeeded' },
      }).title,
    ).toBe('Committed changes (1234567)');
  });

  test('describes named check with exit code and never claims pass without exit 0', () => {
    const checkPassed = {
      event_type: 'tool_call.completed',
      payload: {
        tool_name: 'build.run_named_check',
        command_name: 'unit',
        exit_code: 0,
        status: 'succeeded',
        duration_ms: 250,
      },
    };
    const passDesc = describeActivity(checkPassed);
    expect(passDesc.title).toBe('Check passed: unit');
    expect(passDesc.status).toBe('succeeded');
    expect(passDesc.tone).toBe('success');
    expect(passDesc.outcome).toContain('Exit 0 (250ms)');

    const checkFailed = {
      event_type: 'tool_call.completed',
      payload: {
        tool_name: 'build.run_named_check',
        command_name: 'lint',
        exit_code: 1,
        status: 'failed',
      },
    };
    const failDesc = describeActivity(checkFailed);
    expect(failDesc.title).toBe('Check failed: lint');
    expect(failDesc.status).toBe('failed');
    expect(failDesc.tone).toBe('danger');
    expect(failDesc.outcome).toBe('Exit 1');

    const checkTimedOut = {
      event_type: 'tool_call.completed',
      payload: {
        tool_name: 'build.run_named_check',
        command_name: 'e2e',
        timed_out: true,
        status: 'failed',
      },
    };
    const timeDesc = describeActivity(checkTimedOut);
    expect(timeDesc.title).toBe('Check timed out: e2e');
    expect(timeDesc.status).toBe('failed');
    expect(timeDesc.tone).toBe('danger');

    // Without exit_code, success alone NEVER claims check passed
    const checkNoExit = {
      event_type: 'tool_call.completed',
      payload: {
        tool_name: 'build.run_named_check',
        command_name: 'unit',
        status: 'succeeded',
      },
    };
    const incompleteDesc = describeActivity(checkNoExit);
    expect(incompleteDesc.title).not.toContain('passed');
    expect(incompleteDesc.status).toBe('unknown');
    expect(incompleteDesc.outcome).toBe('Outcome incomplete');
  });

  test('describes explicit denied, cancelled, and failed tool states', () => {
    const denied = {
      event_type: 'tool_call.completed',
      payload: {
        tool_name: 'repository.write_file',
        path: 'secrets.json',
        status: 'denied',
        authorized: false,
      },
    };
    const deniedDesc = describeActivity(denied);
    expect(deniedDesc.status).toBe('denied');
    expect(deniedDesc.tone).toBe('warning');
    expect(deniedDesc.outcome).toBe('Write denied');
    expect(deniedDesc.title).toBe('Write denied: secrets.json');
    expect(describeActivity({ event_type: 'tool_call.completed', payload: { tool_name: 'repository.delete_file', path: 'old.ts', status: 'cancelled' } }).title).toBe('Delete cancelled: old.ts');
    expect(describeActivity({ event_type: 'tool_call.completed', payload: { tool_name: 'repository.rename_file', source: 'a', destination: 'b', status: 'failed' } }).title).toBe('Rename failed: a → b');

    const cancelled = {
      event_type: 'tool_call.completed',
      payload: {
        tool_name: 'build.run_named_check',
        command_name: 'test',
        caller_cancelled: true,
        status: 'cancelled',
      },
    };
    const cancelDesc = describeActivity(cancelled);
    expect(cancelDesc.status).toBe('cancelled');
    expect(cancelDesc.tone).toBe('neutral');
    expect(cancelDesc.title).toBe('Check cancelled: test');
  });

  test('describes operation intent events with requested suffix and familiar names', () => {
    const op = {
      event_type: 'operation.intent_created',
      payload: { operation_kind: 'worktree.create' },
    };
    const desc = describeActivity(op);
    expect(desc.title).toBe('Create workspace requested');
    expect(desc.status).toBe('pending');

    const dbOp = {
      event_type: 'operation.intent_created',
      payload: { operation_kind: 'database.provision' },
    };
    expect(describeActivity(dbOp).title).toBe('Provision database requested');

    const unknownOp = {
      event_type: 'operation.intent_created',
      payload: { operation_kind: 'custom.something_unusual' },
    };
    expect(describeActivity(unknownOp).title).toBe('Custom something unusual requested');
    expect(describeActivity({ event_type: 'operation.settled', payload: { operation_kind: 'worktree.create' } }).title).not.toContain('requested');
  });

  test('provides neutral readable fallback for unknown events without crashing', () => {
    const unknown = {
      event_type: 'custom.unrecognized_event',
      payload: null,
    };
    const desc = describeActivity(unknown);
    expect(desc.title).toBe('Custom unrecognized event');
    expect(desc.status).toBe('unknown');
    expect(desc.tone).toBe('neutral');
  });

  test('groups adjacent repetitive successful read-only activity within 60s sharing lineage', () => {
    const events = [
      {
        sequence: 4,
        event_type: 'tool_call.completed',
        occurred_at: '2026-09-28T10:00:20Z',
        run_version: 1,
        payload: {
          tool_name: 'repository.read_file',
          path: 'b.ts',
          status: 'succeeded',
          agent_execution_id: 'exec-1',
        },
      },
      {
        sequence: 3,
        event_type: 'tool_call.completed',
        occurred_at: '2026-09-28T10:00:10Z',
        run_version: 1,
        payload: {
          tool_name: 'repository.read_file',
          path: 'a.ts',
          status: 'succeeded',
          agent_execution_id: 'exec-1',
        },
      },
      {
        sequence: 2,
        event_type: 'tool_call.completed',
        occurred_at: '2026-09-28T10:00:05Z',
        run_version: 1,
        payload: {
          tool_name: 'repository.write_file',
          path: 'out.txt',
          status: 'succeeded',
          agent_execution_id: 'exec-1',
        },
      },
      {
        sequence: 1,
        event_type: 'tool_call.completed',
        occurred_at: '2026-09-28T10:00:00Z',
        run_version: 1,
        payload: {
          tool_name: 'repository.read_file',
          path: 'c.ts',
          status: 'succeeded',
          agent_execution_id: 'exec-1',
        },
      },
    ];

    const grouped = groupRecentActivity(events);
    expect(grouped).toHaveLength(3);
    expect(grouped[0].isGroup).toBe(true);
    expect(grouped[0].count).toBe(2);
    expect(grouped[0].items).toHaveLength(2);
    expect(grouped[0].summaryTitle).toBe('2 file reads');
    expect(grouped[1].isGroup).toBe(false);
    expect(grouped[2].isGroup).toBe(false);
  });

  test('does not group across mutations, errors, different executions, missing lineage, run versions, or gaps > 60s', () => {
    // 1. Mutation between reads:
    const withWrite = [
      { sequence: 3, event_type: 'tool_call.completed', occurred_at: '2026-09-28T10:00:10Z', run_version: 1, payload: { tool_name: 'repository.read_file', status: 'succeeded', agent_execution_id: 'e1' } },
      { sequence: 2, event_type: 'tool_call.completed', occurred_at: '2026-09-28T10:00:05Z', run_version: 1, payload: { tool_name: 'repository.delete_file', status: 'succeeded', agent_execution_id: 'e1' } },
      { sequence: 1, event_type: 'tool_call.completed', occurred_at: '2026-09-28T10:00:00Z', run_version: 1, payload: { tool_name: 'repository.read_file', status: 'succeeded', agent_execution_id: 'e1' } },
    ];
    expect(groupRecentActivity(withWrite)).toHaveLength(3);

    // 2. Failed read:
    const withError = [
      { sequence: 2, event_type: 'tool_call.completed', occurred_at: '2026-09-28T10:00:10Z', run_version: 1, payload: { tool_name: 'repository.read_file', status: 'failed', agent_execution_id: 'e1' } },
      { sequence: 1, event_type: 'tool_call.completed', occurred_at: '2026-09-28T10:00:00Z', run_version: 1, payload: { tool_name: 'repository.read_file', status: 'succeeded', agent_execution_id: 'e1' } },
    ];
    expect(groupRecentActivity(withError)).toHaveLength(2);

    // 3. Different executions:
    const diffExec = [
      { sequence: 2, event_type: 'tool_call.completed', occurred_at: '2026-09-28T10:00:10Z', run_version: 1, payload: { tool_name: 'repository.read_file', status: 'succeeded', agent_execution_id: 'e2' } },
      { sequence: 1, event_type: 'tool_call.completed', occurred_at: '2026-09-28T10:00:00Z', run_version: 1, payload: { tool_name: 'repository.read_file', status: 'succeeded', agent_execution_id: 'e1' } },
    ];
    expect(groupRecentActivity(diffExec)).toHaveLength(2);

    // 4. Missing lineage:
    const missingLineage = [
      { sequence: 2, event_type: 'tool_call.completed', occurred_at: '2026-09-28T10:00:10Z', run_version: 1, payload: { tool_name: 'repository.read_file', status: 'succeeded' } },
      { sequence: 1, event_type: 'tool_call.completed', occurred_at: '2026-09-28T10:00:00Z', run_version: 1, payload: { tool_name: 'repository.read_file', status: 'succeeded' } },
    ];
    expect(groupRecentActivity(missingLineage)).toHaveLength(2);

    // 5. Gap > 60s:
    const longGap = [
      { sequence: 2, event_type: 'tool_call.completed', occurred_at: '2026-09-28T10:02:00Z', run_version: 1, payload: { tool_name: 'repository.read_file', status: 'succeeded', agent_execution_id: 'e1' } },
      { sequence: 1, event_type: 'tool_call.completed', occurred_at: '2026-09-28T10:00:00Z', run_version: 1, payload: { tool_name: 'repository.read_file', status: 'succeeded', agent_execution_id: 'e1' } },
    ];
    expect(groupRecentActivity(longGap)).toHaveLength(2);

    // 6. Different run version:
    const diffVersion = [
      { sequence: 2, event_type: 'tool_call.completed', occurred_at: '2026-09-28T10:00:10Z', run_version: 2, payload: { tool_name: 'repository.read_file', status: 'succeeded', agent_execution_id: 'e1' } },
      { sequence: 1, event_type: 'tool_call.completed', occurred_at: '2026-09-28T10:00:00Z', run_version: 1, payload: { tool_name: 'repository.read_file', status: 'succeeded', agent_execution_id: 'e1' } },
    ];
    expect(groupRecentActivity(diffVersion)).toHaveLength(2);
  });

  test('formats relative time neutrally and accurately', () => {
    const baseTime = Date.parse('2026-09-28T12:00:00Z');
    expect(formatRelativeTime('2026-09-28T11:59:58Z', baseTime)).toBe('just now');
    expect(formatRelativeTime('2026-09-28T11:59:30Z', baseTime)).toBe('30s ago');
    expect(formatRelativeTime('2026-09-28T11:55:00Z', baseTime)).toBe('5m ago');
    expect(formatRelativeTime('2026-09-28T10:00:00Z', baseTime)).toBe('2h ago');
    expect(formatRelativeTime('2026-09-25T12:00:00Z', baseTime)).toBe('3d ago');
    expect(formatRelativeTime('not-a-date', baseTime)).toBe('Unknown time');
    expect(formatRelativeTime(undefined, baseTime)).toBe('Unknown time');
    expect(formatRelativeTime('2026-09-28T12:02:00Z', baseTime)).toBe('in 2m');
  });

  test('only coherent named check evidence can pass', () => {
    const base = { tool_name: 'build.run_named_check', command_name: 'unit', exit_code: 0, status: 'succeeded' };
    const describe = (payload: Record<string, unknown>) => describeActivity({ event_type: 'tool_call.completed', payload });
    for (const conflict of [
      { status: 'failed' }, { status: 'denied' }, { authorized: false },
      { caller_cancelled: true }, { timed_out: true }, { error_code: 'adapter_error' },
      { exit_code: 0.5 }, { exit_code: Number.NaN }, { exit_code: Number.POSITIVE_INFINITY },
    ]) expect(describe({ ...base, ...conflict }).title).not.toContain('passed');
    expect(describe({ ...base, status: 'pending' }).status).toBe('pending');
    expect(describe({ ...base, status: 'running' }).status).toBe('running');
  });

  test('keeps lifecycle truth in collapsed titles', () => {
    const describe = (tool_name: string, status: string, extra = {}) => describeActivity({ event_type: 'tool_call.completed', payload: { tool_name, status, ...extra } });
    expect(describe('repository.write_file', 'failed', { path: 'x' }).title).toBe('Write failed: x');
    expect(describe('repository.delete_file', 'cancelled', { path: 'x' }).title).toBe('Delete cancelled: x');
    expect(describe('repository.rename_file', 'unknown', { source: 'a', destination: 'b' }).title).toBe('Rename outcome unknown: a → b');
    expect(describe('git.commit', 'pending').title).toBe('Commit pending');
    expect(describe('git.status', 'succeeded').outcome).toBe('Status read');
    expect(describe('validation-results.read', 'succeeded', { has_evidence: false }).outcome).toBe('No evidence available');
  });

  test.each([
    ['validation-results.read', 'validation'],
    ['review-artifacts.read', 'review'],
  ])('only claims evidence was read when its presence is recorded: %s', (toolName, kind) => {
    const describe = (hasEvidence?: boolean) => describeActivity({
      event_type: 'tool_call.completed',
      payload: { tool_name: toolName, status: 'succeeded', has_evidence: hasEvidence },
    });
    expect(describe(true)).toMatchObject({ title: `Read ${kind} evidence`, outcome: 'Evidence read' });
    expect(describe(false)).toMatchObject({ title: `Checked for ${kind} evidence`, outcome: 'No evidence available' });
    expect(describe()).toMatchObject({ title: `Checked for ${kind} evidence`, outcome: 'Availability not recorded' });
  });

  test.each(['repository.read_file', 'repository.search_semantic'])('read grouping requires coherent lineage, authorization and total window: %s', toolName => {
    const event = (seconds: number, extra = {}, version = 1) => ({ event_type: 'tool_call.completed', occurred_at: new Date(Date.parse('2026-09-28T10:00:00Z') + seconds * 1000).toISOString(), run_version: version, payload: { tool_name: toolName, status: 'succeeded', authorized: true, agent_execution_id: 'exec', step_id: 'step', ...extra } });
    expect(groupRecentActivity([event(110), event(55), event(0)])).toHaveLength(2);
    for (const extra of [{ status: 'failed' }, { status: 'denied' }, { authorized: false }, { caller_cancelled: true }, { timed_out: true }, { error_code: 'adapter_error' }, { step_id: 'other' }, { agent_execution_id: 'other' }, { agent_execution_id: 5 }, { subscription_attempt_id: 'different' }]) {
      expect(groupRecentActivity([event(10), event(0, extra)])).toHaveLength(2);
    }
    expect(groupRecentActivity([event(10), event(0, {}, 2)])).toHaveLength(2);
    expect(groupRecentActivity([event(10), { ...event(0), occurred_at: 'bad' }])).toHaveLength(2);
    expect(groupRecentActivity([event(10), event(0)])).toHaveLength(1);
    expect(groupRecentActivity([event(10), event(0)])[0].summaryTitle).toBe(toolName === 'repository.read_file' ? '2 file reads' : '2 read actions');
  });

  test.each([
    ['repository.read_file', { path: 'a.ts' }, ['Read denied: a.ts', 'Read denied'], ['Read failed: a.ts', 'Read failed'], ['Read cancelled: a.ts', 'Read cancelled'], ['Read pending: a.ts', 'pending'], ['Read outcome unknown: a.ts', 'outcome unknown']],
    ['repository.write_file', { path: 'a.ts' }, ['Write denied: a.ts', 'Write denied'], ['Write failed: a.ts', 'Write failed'], ['Write cancelled: a.ts', 'Write cancelled'], ['Write pending: a.ts', 'pending'], ['Write outcome unknown: a.ts', 'outcome unknown']],
    ['repository.delete_file', { path: 'a.ts' }, ['Delete denied: a.ts', 'Delete denied'], ['Delete failed: a.ts', 'Delete failed'], ['Delete cancelled: a.ts', 'cancelled'], ['Delete pending: a.ts', 'pending'], ['Delete outcome unknown: a.ts', 'outcome unknown']],
    ['repository.rename_file', { source: 'a.ts', destination: 'b.ts' }, ['Rename denied: a.ts → b.ts', 'Rename denied'], ['Rename failed: a.ts → b.ts', 'Rename failed'], ['Rename cancelled: a.ts → b.ts', 'cancelled'], ['Rename pending: a.ts → b.ts', 'pending'], ['Rename outcome unknown: a.ts → b.ts', 'outcome unknown']],
    ['repository.list_files', {}, ['List files denied', 'denied'], ['List files failed', 'failed'], ['List files cancelled', 'cancelled'], ['List files pending', 'pending'], ['List files outcome unknown', 'outcome unknown']],
    ['repository.search', {}, ['Search repository denied', 'denied'], ['Search repository failed', 'failed'], ['Search repository cancelled', 'cancelled'], ['Search repository pending', 'pending'], ['Search repository outcome unknown', 'outcome unknown']],
    ['repository.search_semantic', {}, ['Search repository denied', 'denied'], ['Search repository failed', 'failed'], ['Search repository cancelled', 'cancelled'], ['Search repository pending', 'pending'], ['Search repository outcome unknown', 'outcome unknown']],
    ['repository.read_instructions', {}, ['Read project instructions denied', 'denied'], ['Read project instructions failed', 'failed'], ['Read project instructions cancelled', 'cancelled'], ['Read project instructions pending', 'pending'], ['Read project instructions outcome unknown', 'outcome unknown']],
    ['git.status', {}, ['Git status denied', 'denied'], ['Git status failed', 'failed'], ['Git status cancelled', 'cancelled'], ['Git status pending', 'pending'], ['Git status outcome unknown', 'outcome unknown']],
    ['git.diff', {}, ['Git diff denied', 'denied'], ['Git diff failed', 'failed'], ['Git diff cancelled', 'cancelled'], ['Git diff pending', 'pending'], ['Git diff outcome unknown', 'outcome unknown']],
    ['git.commit', {}, ['Commit denied', 'denied'], ['Commit failed', 'Commit failed'], ['Commit cancelled', 'Commit cancelled'], ['Commit pending', 'pending'], ['Commit outcome unknown', 'outcome unknown']],
    ['validation-results.read', {}, ['Evidence read denied', 'denied'], ['Evidence read failed', 'failed'], ['Evidence read cancelled', 'cancelled'], ['Evidence read pending', 'pending'], ['Evidence read outcome unknown', 'outcome unknown']],
    ['review-artifacts.read', {}, ['Evidence read denied', 'denied'], ['Evidence read failed', 'failed'], ['Evidence read cancelled', 'cancelled'], ['Evidence read pending', 'pending'], ['Evidence read outcome unknown', 'outcome unknown']],
  ] as const)('preserves non-success presentation for %s', (tool, fields, denied, failed, cancelled, pending, unknown) => {
    for (const [status, expected] of [['denied', denied], ['failed', failed], ['cancelled', cancelled], ['pending', pending], ['unknown', unknown]] as const) {
      expect(describeActivity({ event_type: 'tool_call.completed', payload: { tool_name: tool, ...fields, status } })).toMatchObject({ title: expected[0], outcome: expected[1] });
    }
  });

  test('preserves contradictory historical flag presentation and unknown tool fallback', () => {
    const event = (tool_name: string, extra: Record<string, unknown>) => describeActivity({ event_type: 'tool_call.completed', payload: { tool_name, status: 'succeeded', ...extra } });
    expect(event('repository.read_file', { path: 'x', caller_cancelled: true, timed_out: true })).toMatchObject({ title: 'Read cancelled: x', outcome: 'cancelled', status: 'cancelled' });
    expect(event('repository.write_file', { path: 'x', authorized: false, caller_cancelled: true })).toMatchObject({ title: 'Write denied: x', outcome: 'Write denied', status: 'denied' });
    expect(event('git.commit', { authorized: false, caller_cancelled: true })).toMatchObject({ title: 'Commit denied', outcome: 'denied', status: 'denied' });
    expect(describeActivity({ event_type: 'tool.foo', payload: null })).toMatchObject({ title: 'Tool foo outcome unknown', outcome: 'outcome unknown' });
  });
});
