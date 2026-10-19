import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, expect, test, vi } from 'vitest';
import AuditDetailEventPage from './page';
import { api } from '@/lib/api/client';

let mockParams = { source: 'run', eventId: '11111111-1111-4111-8111-111111111111' };

vi.mock('next/navigation', () => ({
  useParams: () => mockParams,
}));

vi.mock('@/lib/api/client', () => ({ api: vi.fn() }));

afterEach(() => {
  cleanup();
  vi.mocked(api).mockReset();
});

beforeEach(() => {
  mockParams = { source: 'run', eventId: '11111111-1111-4111-8111-111111111111' };
});

test('valid run event loads via /audit/run-events/{id} and displays readable evidence context', async () => {
  vi.mocked(api).mockResolvedValue({
    id: '11111111-1111-4111-8111-111111111111',
    created_at: '2026-10-10T12:00:00Z',
    event_type: 'tool_call.completed',
    actor_class: 'agent',
    actor_id: 'actor-123',
    run_id: '33333333-3333-4333-8333-333333333333',
    project_id: '44444444-4444-4444-8444-444444444444',
    subject_id: null,
    subject_type: 'run',
    source: 'run',
    payload: {
      tool_name: 'build.run_named_check',
      command_name: 'typecheck',
      exit_code: 0,
      duration_ms: 120,
    },
  });

  render(<AuditDetailEventPage />);

  expect(await screen.findByRole('heading', { level: 1, name: /Ran check: typecheck/ })).toBeInTheDocument();
  expect(screen.getByText(/Source: run/)).toBeInTheDocument();
  expect(screen.getByText(/agent · actor-123/)).toBeInTheDocument();
  expect(screen.getByRole('link', { name: /Back to Audit log/ })).toHaveAttribute('href', '/audit');
  expect(screen.getByRole('link', { name: /Run 33333333/ })).toHaveAttribute('href', '/runs/33333333-3333-4333-8333-333333333333');
  expect(screen.getByRole('link', { name: /Project 44444444/ })).toHaveAttribute('href', '/projects/44444444-4444-4444-8444-444444444444');
  expect(screen.getByRole('link', { name: /Raw JSON evidence/ })).toHaveAttribute('href', '/api/audit/run-events/11111111-1111-4111-8111-111111111111');
  expect(screen.getByText(/"build\.run_named_check"/)).toBeInTheDocument();
});

test('valid operator event loads via /audit/{id}', async () => {
  mockParams = { source: 'operator', eventId: '22222222-2222-4222-8222-222222222222' };
  vi.mocked(api).mockResolvedValue({
    id: '22222222-2222-4222-8222-222222222222',
    created_at: '2026-10-10T12:00:00Z',
    event_type: 'operator.override',
    actor_class: 'human',
    actor_id: 'operator-1',
    run_id: null,
    project_id: null,
    subject_id: null,
    subject_type: 'project',
    source: 'operator',
    payload: { action: 'override_warning' },
  });

  render(<AuditDetailEventPage />);

  expect(await screen.findByRole('heading', { level: 1 })).toBeInTheDocument();
  expect(vi.mocked(api)).toHaveBeenCalledWith(
    expect.stringContaining('/audit/22222222-2222-4222-8222-222222222222'),
    expect.anything()
  );
  expect(screen.getByRole('link', { name: /Raw JSON evidence/ })).toHaveAttribute('href', '/api/audit/22222222-2222-4222-8222-222222222222');
});

test('rejects invalid source or eventId without querying the API', async () => {
  mockParams = { source: 'invalid-source', eventId: 'not-a-uuid' };
  render(<AuditDetailEventPage />);

  expect(screen.getByRole('alert')).toHaveTextContent(/Invalid audit event parameters/i);
  expect(vi.mocked(api)).not.toHaveBeenCalled();
});

test('handles loading and failed API states with retry', async () => {
  vi.mocked(api)
    .mockRejectedValueOnce(new Error('Network error'))
    .mockResolvedValueOnce({
      id: '11111111-1111-4111-8111-111111111111',
      created_at: '2026-10-10T12:00:00Z',
      event_type: 'tool_call.completed',
      actor_class: 'agent',
      actor_id: null,
      run_id: null,
      project_id: null,
      subject_id: null,
      subject_type: 'run',
      source: 'run',
      payload: {},
    });

  render(<AuditDetailEventPage />);

  expect(await screen.findByRole('alert')).toHaveTextContent(/Audit event evidence unavailable/i);
  const retryBtn = screen.getByRole('button', { name: /Retry/i });
  await userEvent.click(retryBtn);

  expect(await screen.findByText(/Source: run/)).toBeInTheDocument();
});
