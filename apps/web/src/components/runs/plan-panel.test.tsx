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
  expect(screen.queryByRole('heading', { name: 'Writable paths' })).toBeNull();
});

test.each([{ paths: ['src/api', 'tests/api'] }, { paths: [] }])('shows the explicit writable scope $paths', async ({ paths: ownedPaths }) => {
  const plan = { summary: 'Scoped plan', assumptions: [], affected_components: ['API service'],
    steps: ['Implement'], required_checks: ['unit'], risks: ['Regression'],
    security_considerations: [], dependency_changes: [], owned_paths: ownedPaths };
  vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({ digest: 'c'.repeat(64), text: JSON.stringify(plan) })));
  render(<PlanPanel projection={projection()} />);
  await screen.findByRole('heading', { name: 'Writable paths' });
  if (ownedPaths.length) for (const path of ownedPaths) expect(screen.getByText(path)).toBeInTheDocument();
  else expect(screen.getByText('No writable paths.')).toBeInTheDocument();
});

test.each([{ scope: null }, { scope: 'src' }, { scope: [''] }])('rejects malformed scope $scope', async ({ scope }) => {
  const plan = { summary: 'Scoped plan', assumptions: [], affected_components: ['API service'],
    steps: ['Implement'], required_checks: ['unit'], risks: ['Regression'],
    security_considerations: [], dependency_changes: [], owned_paths: scope };
  vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({ digest: 'c'.repeat(64), text: JSON.stringify(plan) })));
  render(<PlanPanel projection={projection()} />);
  expect(await screen.findByRole('alert')).toHaveTextContent('Plan evidence unavailable.');
  expect(screen.queryByRole('heading', { name: 'Writable paths' })).toBeNull();
});
