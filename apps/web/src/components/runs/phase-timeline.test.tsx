import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, test } from 'vitest';
import { PhaseTimeline } from './phase-timeline';
import type { components } from '@/lib/api/schema';

type EventItem = components['schemas']['EventItem'];

afterEach(() => {
  cleanup();
});

test('renders empty state when no events recorded', () => {
  render(<PhaseTimeline events={[]} />);
  expect(screen.getByText('No events recorded yet.')).toBeInTheDocument();
});

test('renders human-readable action descriptions, relative times, and exact times on expansion', async () => {
  const events: EventItem[] = [
    {
      sequence: 1,
      run_version: 1,
      actor_class: 'agent',
      event_type: 'tool_call.completed',
      occurred_at: new Date(Date.now() - 30_000).toISOString(),
      payload: {
        tool_name: 'repository.read_file',
        path: 'src/index.ts',
        status: 'succeeded',
        duration_ms: 50,
      },
    },
  ];

  render(<PhaseTimeline events={events} />);
  expect(screen.getByText('Read src/index.ts')).toBeInTheDocument();
  expect(screen.getByText(/30s ago|just now/)).toBeInTheDocument();

  // Expand details
  const summary = screen.getByText('Read src/index.ts');
  await userEvent.click(summary);
  expect(screen.getByText('tool_call.completed')).toBeInTheDocument();
  expect(screen.getByText(/Sequence 1 · Run version 1/)).toBeInTheDocument();
});

test.each(['repository.read_file', 'repository.search', 'repository.search_semantic'])(
  'groups recent %s calls so older checks and commits remain visible', async toolName => {
  const baseTime = Date.now() - 100_000;
  // Create 8 read events followed by a check and a commit
  const events: EventItem[] = [
    {
      sequence: 1,
      run_version: 1,
      actor_class: 'agent',
      event_type: 'tool_call.completed',
      occurred_at: new Date(baseTime + 45_000).toISOString(),
      payload: {
        tool_name: 'git.commit',
        commit_sha: 'abcdef0123456789abcdef0123456789abcdef01',
        status: 'succeeded',
      },
    },
    {
      sequence: 2,
      run_version: 1,
      actor_class: 'agent',
      event_type: 'tool_call.completed',
      occurred_at: new Date(baseTime + 50_000).toISOString(),
      payload: {
        tool_name: 'build.run_named_check',
        command_name: 'unit',
        exit_code: 0,
        status: 'succeeded',
      },
    },
    ...Array.from({ length: 8 }, (_, i) => ({
      sequence: 10 - i,
      run_version: 1,
      actor_class: 'agent',
      event_type: 'tool_call.completed',
      occurred_at: new Date(baseTime + 90_000 - i * 5_000).toISOString(),
      payload: {
        tool_name: toolName,
        ...(toolName === 'repository.read_file' ? { path: `file_${8 - i}.txt` } : {}),
        status: 'succeeded',
        agent_execution_id: 'exec-1',
      },
    })),
  ];

  render(<PhaseTimeline events={events} />);

  // The 8 reads are adjacent, within 60s, same lineage -> grouped into 1 entry
  // Total entries = commit (1) + check (1) + grouped reads (1) = 3 entries
  // If grouping happened AFTER slice(0, 6), the commit and check wouldn't be visible or reads would crowd out
  expect(screen.getByText('Committed changes (abcdef0)')).toBeInTheDocument();
  expect(screen.getByText('Check passed: unit')).toBeInTheDocument();
  const groupTitle = toolName === 'repository.read_file' ? '8 file reads' : '8 read actions';
  expect(screen.getByText(groupTitle)).toBeInTheDocument();

  // Expanding the group shows individual reads
  await userEvent.click(screen.getByText(groupTitle));
  if (toolName === 'repository.read_file') {
    expect(screen.getByText('Read file_1.txt')).toBeVisible();
    expect(screen.getByText('Read file_8.txt')).toBeVisible();
  } else {
    const searches = screen.getAllByText('Searched repository');
    expect(searches).toHaveLength(8);
    searches.forEach(search => expect(search).toBeVisible());
  }
});

test('does not group across distinct writes or failures', () => {
  const baseTime = Date.now() - 50_000;
  const events: EventItem[] = [
    {
      sequence: 3,
      run_version: 1,
      actor_class: 'agent',
      event_type: 'tool_call.completed',
      occurred_at: new Date(baseTime + 20_000).toISOString(),
      payload: {
        tool_name: 'repository.read_file',
        path: 'file_b.txt',
        status: 'succeeded',
        agent_execution_id: 'exec-1',
      },
    },
    {
      sequence: 2,
      run_version: 1,
      actor_class: 'agent',
      event_type: 'tool_call.completed',
      occurred_at: new Date(baseTime + 10_000).toISOString(),
      payload: {
        tool_name: 'repository.write_file',
        path: 'out.txt',
        status: 'succeeded',
        agent_execution_id: 'exec-1',
      },
    },
    {
      sequence: 1,
      run_version: 1,
      actor_class: 'agent',
      event_type: 'tool_call.completed',
      occurred_at: new Date(baseTime).toISOString(),
      payload: {
        tool_name: 'repository.read_file',
        path: 'file_a.txt',
        status: 'succeeded',
        agent_execution_id: 'exec-1',
      },
    },
  ];

  render(<PhaseTimeline events={events} />);
  expect(screen.getByText('Read file_b.txt')).toBeInTheDocument();
  expect(screen.getByText('Wrote out.txt')).toBeInTheDocument();
  expect(screen.getByText('Read file_a.txt')).toBeInTheDocument();
  expect(screen.queryByText(/Read 2 files/)).not.toBeInTheDocument();
});
