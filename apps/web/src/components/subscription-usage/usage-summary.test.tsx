import { act, cleanup, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, test, vi } from 'vitest';
import { SubscriptionUsageSummary } from './usage-summary';
import UsagePage from '@/app/usage/page';
import type { components } from '@/lib/api/schema';

type Page = components['schemas']['SubscriptionUsagePage'];
const empty: Page = { items: [], has_more: false };
function page(): Page {
  const unknown = { known_total: null, measured_attempts: 0, unknown_attempts: 3 };
  return { has_more: true, items: [{
    project_id: 'project-1', run_id: 'run-1', purpose: 'routine_implementation',
    effective_route: { provider: 'google', client: 'gemini_cli', model: 'flash', effort: 'medium',
      auth_mode: 'subscription', billing_mode: 'allowance_only' },
    currency: null, attempts: 3, recorded_results: 2, failed_results: 1, pending_results: 1,
    input_tokens: { known_total: 10, measured_attempts: 2, unknown_attempts: 1 },
    output_tokens: { known_total: 0, measured_attempts: 1, unknown_attempts: 2 },
    cached_input_tokens: unknown, duration_ms: unknown, tool_calls: unknown,
    named_checks: unknown, estimated_api_cost_minor: unknown,
  }] };
}
const response = (value: unknown) => new Response(JSON.stringify(value));

function assessed(): Page {
  const value = page();
  const missing = { numerator: null, denominator: null, primary_attempts: 1, all_attempts: 4,
    numerator_measured_attempts: 0, numerator_unknown_attempts: 1,
    denominator_measured_attempts: 0, denominator_unknown_attempts: 4, coverage: 0, share: null };
  value.assessment = {
    primary_turns: 1, all_attempts: 4, delegation_decisions: 0, wait_decisions: 0, repair_debits: 1,
    fallback_attempts: 1, preferred_attempts: 3, unknown_route_attempts: 0, unverified_decisions: 0,
    shares: {
      input_tokens: { ...missing, denominator: 30, denominator_measured_attempts: 2, denominator_unknown_attempts: 2, coverage: 0.5 },
      output_tokens: { ...missing, numerator: 0, denominator: 20, numerator_measured_attempts: 1, numerator_unknown_attempts: 0,
        denominator_measured_attempts: 2, denominator_unknown_attempts: 2, coverage: 0.5, share: 0 },
      duration_ms: missing,
    },
    waits: { decisions: 0, continued: 0, unfinished: 0, ended_without_continuation: 0, measured_intervals: 0, unknown_intervals: 0, elapsed_ms: null },
    outcomes: [{ project_id: 'project-1', run_id: 'run-1', purpose: 'routine_implementation',
      effective_route: value.items[0].effective_route, currency: null, attempts: 3, distinct_tasks: 2, terminal_tasks: 1,
      verified_results: 2, unverified_results: 0, pending_results: 1, failed_results: 1, applied_decisions: 1,
      completed_handoffs: 1, task_acceptances: 1, fallback_attempts: 1, latest_fallback_reason: 'Confirmed quota; probe pending',
      latest_result_disposition: 'handoff_completed' }],
    outcomes_has_more: true,
  };
  return value;
}
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

test('shows route, result counts and coverage beside zero and unknown measurements', async () => {
  vi.spyOn(globalThis, 'fetch').mockImplementation(async () => response(page()));
  render(<SubscriptionUsageSummary runId="run-1" />);
  const article = await screen.findByRole('article');
  expect(article).toHaveTextContent('routine_implementation · google / flash');
  expect(article).toHaveTextContent('Client: gemini_cli · Effort: medium · subscription · allowance_only');
  expect(article).toHaveTextContent('3 attempts · 2 recorded results (1 failed) · 1 without a recorded result');
  expect(within(article).getByText('Output tokens').parentElement).toHaveTextContent('0 · 1 of 3 attempts measured; 2 unknown');
  expect(within(article).getByText('Estimated API cost').parentElement).toHaveTextContent('Unknown · 0 of 3 attempts measured; 3 unknown');
  expect(within(article).getByText('Input tokens').parentElement).toHaveTextContent('10 · 2 of 3 attempts measured; 1 unknown');
  expect(within(article).getByRole('link', { name: 'Inspect run run-1' })).toHaveAttribute('href', '/runs/run-1');
  expect(screen.getByText('Cost currency unknown.')).toBeInTheDocument();
});

test('retains measured zero with its identified currency', async () => {
  const value = page();
  value.items[0].currency = 'GBP';
  value.items[0].estimated_api_cost_minor = { known_total: 0, measured_attempts: 1, unknown_attempts: 2 };
  vi.spyOn(globalThis, 'fetch').mockImplementation(async () => response(value));
  render(<SubscriptionUsageSummary />);
  expect(await screen.findByText('0 GBP minor units')).toBeInTheDocument();
  expect(screen.queryByText('Cost currency unknown.')).not.toBeInTheDocument();
});

test('pages groups and resets pagination when the selected run changes', async () => {
  const fetcher = vi.spyOn(globalThis, 'fetch').mockImplementation(async input =>
    response(String(input).includes('offset=25') ? empty : page()));
  const view = render(<SubscriptionUsageSummary runId="run-1" />);
  await screen.findByRole('article');
  await userEvent.click(screen.getByRole('button', { name: 'Next subscription groups' }));
  await screen.findByText('No subscription attempts on this page.');
  expect(fetcher.mock.calls.at(-1)?.[0]).toBe('/api/subscription-usage?run_id=run-1&offset=25&limit=25&include_assessment=true');
  expect(screen.getByRole('button', { name: 'Next subscription groups' })).toBeDisabled();
  view.rerender(<SubscriptionUsageSummary runId="run-2" />);
  await screen.findByRole('article');
  expect(fetcher.mock.calls.at(-1)?.[0]).toBe('/api/subscription-usage?run_id=run-2&offset=0&limit=25&include_assessment=true');
  expect(screen.getByRole('button', { name: 'Previous subscription groups' })).toBeDisabled();
});

test('retains the same page during refresh and prevents overlapping refresh or page requests', async () => {
  let finish: ((value: Response) => void) | undefined;
  const fetcher = vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(response(page()))
    .mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
  render(<SubscriptionUsageSummary />);
  await screen.findByRole('article');
  await userEvent.click(screen.getByRole('button', { name: 'Refresh subscription usage' }));
  expect(screen.getByRole('article')).toHaveTextContent('10 · 2 of 3 attempts measured');
  expect(screen.getByRole('button', { name: 'Refresh subscription usage' })).toBeDisabled();
  expect(screen.getByRole('button', { name: 'Next subscription groups' })).toBeDisabled();
  expect(fetcher.mock.calls).toHaveLength(2);
  await act(async () => { finish?.(response(empty)); });
  expect(await screen.findByText('No subscription attempts on this page.')).toBeInTheDocument();
});

test('a failed projection is retryable and does not invent an empty result', async () => {
  vi.spyOn(globalThis, 'fetch').mockRejectedValueOnce(new Error('offline')).mockResolvedValueOnce(response(empty));
  render(<SubscriptionUsageSummary />);
  expect(await screen.findByRole('alert')).toHaveTextContent('Subscription usage unavailable');
  expect(screen.queryByText('No subscription attempts on this page.')).not.toBeInTheDocument();
  await userEvent.click(screen.getByRole('button', { name: 'Retry subscription usage' }));
  expect(await screen.findByText('No subscription attempts on this page.')).toBeInTheDocument();
});

test('the global usage page loads both subscription and API projections', async () => {
  const fetcher = vi.spyOn(globalThis, 'fetch').mockImplementation(async input => response(
    String(input).startsWith('/api/subscription-usage') ? empty : { items: [], truncated: false }));
  render(<UsagePage />);
  expect(await screen.findByText('No subscription attempts on this page.')).toBeInTheDocument();
  expect(await screen.findByText('No usage on this page.')).toBeInTheDocument();
  expect(fetcher.mock.calls.map(([path]) => path)).toContain('/api/subscription-usage?offset=0&limit=25&include_assessment=true');
});

test('renders unknown primary measurements with complete coverage and inspection links', async () => {
  const fetcher = vi.spyOn(globalThis, 'fetch').mockResolvedValue(response(assessed()));
  render(<SubscriptionUsageSummary runId="run-1" />);
  const assessment = await screen.findByRole('region', { name: 'Subscription usage assessment' });
  expect(assessment).toHaveTextContent('Admitted primary turns: 1');
  expect(assessment).toHaveTextContent('Input tokens primary measured share: Unknown');
  expect(assessment).toHaveTextContent('Unknown / 30 measured units');
  expect(assessment).toHaveTextContent('Primary: 0 of 1 measured; 1 unknown');
  expect(assessment).toHaveTextContent('All: 2 of 4 measured; 2 unknown (50.0% coverage)');
  expect(assessment).toHaveTextContent('Output tokens primary measured share: 0.0%');
  expect(assessment).toHaveTextContent('Recorded wait until next primary admission: Unknown');
  expect(assessment).toHaveTextContent('includes scheduling delay');
  const outcome = screen.getByRole('article');
  expect(outcome).toHaveTextContent('2 distinct tasks');
  expect(outcome).toHaveTextContent('Tasks accepted by primary: 1');
  expect(outcome).toHaveTextContent('Latest recorded disposition: handoff_completed');
  expect(outcome).toHaveTextContent('Confirmed quota; probe pending');
  expect(within(outcome).getByRole('link', { name: 'Inspect tasks and attempts for run run-1' })).toHaveAttribute('href', '/runs/run-1#subscription-tasks');
  expect(fetcher.mock.calls[0][0]).toContain('include_assessment=true');
});

test('keeps route outcome identities distinct and shows continued, open and ended wait coverage', async () => {
  const value = assessed();
  const assessment = value.assessment!;
  assessment.primary_turns = 3;
  assessment.all_attempts = 9;
  assessment.fallback_attempts = 2;
  assessment.preferred_attempts = 7;
  for (const metric of Object.values(assessment.shares)) {
    metric.primary_attempts = 3;
    metric.all_attempts = 9;
    metric.numerator_unknown_attempts = 3 - metric.numerator_measured_attempts;
    metric.denominator_unknown_attempts = 9 - metric.denominator_measured_attempts;
    metric.coverage = metric.denominator_measured_attempts / 9;
  }
  assessment.delegation_decisions = 2;
  assessment.wait_decisions = 1;
  assessment.waits = { decisions: 3, continued: 1, unfinished: 1, ended_without_continuation: 1, measured_intervals: 1, unknown_intervals: 2, elapsed_ms: 5000 };
  assessment.outcomes.push({ ...assessment.outcomes[0], effective_route: { ...assessment.outcomes[0].effective_route, effort: 'high', client: 'other-client' } });
  value.items.push({ ...value.items[0], effective_route: assessment.outcomes[1].effective_route });
  vi.spyOn(globalThis, 'fetch').mockResolvedValue(response(value));
  const errors = vi.spyOn(console, 'error').mockImplementation(() => {});
  render(<SubscriptionUsageSummary />);
  const region = await screen.findByRole('region', { name: 'Subscription usage assessment' });
  expect(region).toHaveTextContent('5,000 ms');
  expect(region).toHaveTextContent('1 continued · 1 unfinished · 1 ended without continuation');
  expect(region).toHaveTextContent('1 measured intervals · 2 unknown');
  expect(screen.getAllByText(/Client:/)).toHaveLength(2);
  expect(errors).not.toHaveBeenCalled();
});
