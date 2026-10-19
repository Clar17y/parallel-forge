import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, expect, test } from 'vitest';
import { AuditEventCard } from './audit-event-card';
import type { components } from '@/lib/api/schema';

type AuditItem = components['schemas']['AuditItem'];

afterEach(() => {
  cleanup();
});

test('renders human-readable description while preserving raw event type and evidence', () => {
  const item: AuditItem = {
    id: '11111111-1111-4111-8111-111111111111',
    created_at: new Date(Date.now() - 45_000).toISOString(),
    event_type: 'tool_call.completed',
    actor_class: 'agent',
    actor_id: '22222222-2222-4222-8222-222222222222',
    run_id: '33333333-3333-4333-8333-333333333333',
    project_id: '44444444-4444-4444-8444-444444444444',
    subject_id: null,
    subject_type: 'run',
    source: 'run',
    operations: [
      {
        id: '55555555-5555-4555-8555-555555555555',
        kind: 'command.run_named_check',
        status: 'SUCCEEDED',
      },
    ],
    payload: {
      tool_name: 'build.run_named_check',
      command_name: 'typecheck',
      exit_code: 0,
      status: 'succeeded',
      duration_ms: 120,
    },
  };

  render(<AuditEventCard item={item} />);

  // Shared describeActivity title and status
  expect(screen.getByRole('heading', { level: 2, name: 'Check passed: typecheck' })).toBeInTheDocument();
  expect(screen.getByText(/Exit 0/)).toBeInTheDocument();

  // Raw event type preserved
  expect(screen.getByText('tool_call.completed')).toBeInTheDocument();

  // Relative time and actor
  expect(screen.getByText(/45s ago/)).toBeInTheDocument();
  expect(screen.getByText(/agent · 22222222-2222-4222-8222-222222222222/)).toBeInTheDocument();

  // Links
  expect(screen.getByRole('link', { name: /Run 33333333/ })).toHaveAttribute('href', '/runs/33333333-3333-4333-8333-333333333333');
  expect(screen.getByRole('link', { name: /Project 44444444/ })).toHaveAttribute('href', '/projects/44444444-4444-4444-8444-444444444444');
  expect(screen.getByRole('link', { name: 'Event details' })).toHaveAttribute('href', '/audit/run/11111111-1111-4111-8111-111111111111');
  expect(screen.getByRole('link', { name: 'Raw JSON' })).toHaveAttribute('href', '/api/audit/run-events/11111111-1111-4111-8111-111111111111');

  // Operation status distinction
  expect(screen.getByText(/command\.run_named_check/)).toBeInTheDocument();
  expect(screen.getByText(/Current status: SUCCEEDED/)).toBeInTheDocument();

  // Recorded event fields disclosure
  expect(screen.getByText('Recorded event fields')).toBeInTheDocument();
});

test('links in-app detail as primary destination, labels raw response explicitly Raw JSON, and elevates operation warnings outside closed details', () => {
  const item: AuditItem = {
    id: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
    created_at: '2026-10-10T12:00:00Z',
    event_type: 'operator.override',
    actor_class: 'human',
    actor_id: 'op-1',
    run_id: null,
    project_id: null,
    subject_id: null,
    subject_type: 'project',
    source: 'operator',
    operations: [
      {
        id: 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb',
        kind: 'override_warning',
        status: 'NEEDS_RECONCILIATION',
      },
    ],
    payload: { note: 'manual intervention' },
  };

  render(<AuditEventCard item={item} />);

  // In-app detail is primary button
  const detailLink = screen.getByRole('link', { name: /Event details/i });
  expect(detailLink).toHaveAttribute('data-variant', 'primary');
  expect(detailLink).toHaveAttribute('href', '/audit/operator/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa');

  // Raw response is explicitly labeled Raw JSON
  expect(screen.getByText('Raw JSON')).toBeInTheDocument();

  // Important warning is visible
  expect(screen.getByText(/NEEDS_RECONCILIATION/)).toBeVisible();
});
