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

test('groups adjacent repetitive successful read-only activity before selecting six visible entries', async () => {
  const baseTime = Date.now() - 100_000;
  // Create 8 read events followed by a check and a commit
  const events: EventItem[] = [
    {
      sequence: 10,
      run_version: 1,
      actor_class: 'agent',
      event_type: 'tool_call.completed',
      occurred_at: new Date(baseTime + 90_000).toISOString(),
      payload: {
        tool_name: 'git.commit',
        commit_sha: 'abcdef0123456789abcdef0123456789abcdef01',
        status: 'succeeded',
      },
    },
    {
      sequence: 9,
      run_version: 1,
      actor_class: 'agent',
      event_type: 'tool_call.completed',
      occurred_at: new Date(baseTime + 85_000).toISOString(),
      payload: {
        tool_name: 'build.run_named_check',
        command_name: 'unit',
        exit_code: 0,
        status: 'succeeded',
      },
    },
    ...Array.from({ length: 8 }, (_, i) => ({
      sequence: 8 - i,
      run_version: 1,
      actor_class: 'agent',
      event_type: 'tool_call.completed',
      occurred_at: new Date(baseTime + (8 - i) * 5_000).toISOString(),
      payload: {
        tool_name: 'repository.read_file',
        path: `file_${8 - i}.txt`,
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
  expect(screen.getByText('8 file reads')).toBeInTheDocument();

  // Expanding the group shows individual reads
  await userEvent.click(screen.getByText('8 file reads'));
  expect(screen.getByText('Read file_1.txt')).toBeInTheDocument();
  expect(screen.getByText('Read file_8.txt')).toBeInTheDocument();
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
