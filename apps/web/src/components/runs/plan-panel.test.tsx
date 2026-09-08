import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, expect, test, vi } from 'vitest';
import { PlanPanel } from './plan-panel';
import { projection } from '@/test/projection';

afterEach(() => { cleanup(); vi.restoreAllMocks(); });
test('shows structured plan evidence as text alongside the bound base and policy', async () => {
  const plan = { summary: 'Implement the approved feature', assumptions: ['Repository available'], affected_components: ['api'],
    steps: ['Add the endpoint'], required_checks: ['unit-tests'], risks: ['Compatibility'],
    security_considerations: ['<script>not executable</script>'], dependency_changes: ['None'] };
  vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({ digest: 'c'.repeat(64), text: JSON.stringify(plan) })));
  render(<PlanPanel projection={projection()} />);
  await screen.findByText('Add the endpoint');
  expect(screen.getByText('<script>not executable</script>')).toBeInTheDocument();
  expect(document.querySelector('script')).toBeNull();
  expect(screen.getByText('a'.repeat(40))).toBeInTheDocument();
  expect(screen.getByText('unit-tests')).toBeInTheDocument();
});
