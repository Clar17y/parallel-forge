'use client';

import Link from 'next/link';
import { useState } from 'react';
import { useApi } from '@/hooks/use-api';
import type { components } from '@/lib/api/schema';

type Page = components['schemas']['SubscriptionUsagePage'];
type Item = components['schemas']['SubscriptionUsageItem'];
type Metric = components['schemas']['SubscriptionUsageMetric'];
type Outcome = components['schemas']['SubscriptionUsageOutcome'];
type Share = components['schemas']['SubscriptionUsageShare'];
const pageSize = 25;
const measured = (value: number | null) => value === null ? 'Unknown' : value.toLocaleString();

export function SubscriptionUsageSummary({ runId }: { runId?: string }) {
  return <UsageGroups key={runId ?? 'all-runs'} runId={runId} />;
}

function groupKey(item: Pick<Item, 'project_id' | 'run_id' | 'purpose' | 'effective_route' | 'currency'>) {
  const route = item.effective_route;
  return JSON.stringify([item.project_id, item.run_id, item.purpose, route.provider,
    route.client, route.model, route.effort, route.auth_mode, route.billing_mode, item.currency]);
}

function Measurement({ label, metric, unit = '' }: { label: string; metric: Metric; unit?: string }) {
  const attempts = metric.measured_attempts + metric.unknown_attempts;
  return <div><dt className="font-medium">{label}</dt><dd>
    <span>{metric.known_total === null ? 'Unknown' : `${metric.known_total.toLocaleString()}${unit ? ` ${unit}` : ''}`}</span>
    {' · '}{metric.measured_attempts} of {attempts} attempts measured; {metric.unknown_attempts} unknown
  </dd></div>;
}

function UsageGroups({ runId }: { runId?: string }) {
  const [offset, setOffset] = useState(0);
  const filter = runId ? `run_id=${encodeURIComponent(runId)}&` : '';
  const usage = useApi<Page>(`/subscription-usage?${filter}offset=${offset}&limit=${pageSize}&include_assessment=true`,
    { refreshIntervalMs: 5000, keepPreviousOnRefresh: true });
  const outcomes = new Map(usage.value?.assessment?.outcomes.map(outcome => [groupKey(outcome), outcome]));
  return <section aria-label="Subscription usage" className="my-6 min-w-0 space-y-4 rounded-lg border border-slate-300 p-4">
    <div className="flex flex-wrap items-center justify-between gap-3">
      <h2 className="text-lg font-semibold">Subscription usage</h2>
      <button type="button" disabled={usage.refreshing} onClick={usage.refresh}>Refresh subscription usage</button>
    </div>
    <p>Measured subtotals grouped by run, role, route used and currency. Each subtotal shows how many attempts reported that measurement.</p>
    <p>API estimates do not measure subscription allowance or account spend. Provider result counts do not indicate task acceptance.</p>
    {usage.loading ? <p role="status">Loading subscription usage…</p> : null}
    {usage.refreshing && !usage.loading ? <p role="status">Refreshing subscription usage…</p> : null}
    {usage.failed ? <p role="alert">Subscription usage unavailable. <button type="button" onClick={usage.refresh}>Retry subscription usage</button></p> : null}
    {usage.value ? <>
      {usage.value.assessment ? <Assessment assessment={usage.value.assessment} /> : null}
      {usage.value.items.length === 0 ? <p>No subscription attempts on this page.</p> : null}
      {usage.value.items.map(item => <article key={groupKey(item)} className="min-w-0 space-y-2 rounded border border-slate-300 p-3 [overflow-wrap:anywhere]">
        <h3 className="font-semibold">{item.purpose} · {item.effective_route.provider} / {item.effective_route.model}</h3>
        <p>Client: {item.effective_route.client} · Effort: {item.effective_route.effort} · {item.effective_route.auth_mode} · {item.effective_route.billing_mode}</p>
        <p><Link href={`/projects/${item.project_id}`}>Project {item.project_id}</Link>{' · '}
          <Link href={`/runs/${item.run_id}`}>Inspect run {item.run_id}</Link></p>
        <p>{item.attempts} attempts · {item.recorded_results} recorded results ({item.failed_results} failed) · {item.pending_results} without a recorded result</p>
        <dl className="grid gap-3 text-sm sm:grid-cols-2">
          <Measurement label="Input tokens" metric={item.input_tokens} />
          <Measurement label="Output tokens" metric={item.output_tokens} />
          <Measurement label="Cached input tokens" metric={item.cached_input_tokens} />
          <Measurement label="Duration" metric={item.duration_ms} unit="ms" />
          <Measurement label="Tool calls" metric={item.tool_calls} />
          <Measurement label="Named checks" metric={item.named_checks} />
          <Measurement label="Estimated API cost" metric={item.estimated_api_cost_minor}
            unit={item.currency ? `${item.currency} minor units` : ''} />
        </dl>
        {item.currency === null ? <p className="text-sm">Cost currency unknown.</p> : null}
        {outcomes.has(groupKey(item)) ? <GroupOutcome outcome={outcomes.get(groupKey(item))!} /> : null}
      </article>)}
    </> : null}
    <nav aria-label="Subscription usage pages" className="flex flex-wrap items-center gap-4">
      <button type="button" disabled={offset === 0 || usage.refreshing} onClick={() => setOffset(Math.max(0, offset - pageSize))}>Previous subscription groups</button>
      <span>Page {Math.floor(offset / pageSize) + 1}</span>
      <button type="button" disabled={!usage.value?.has_more || usage.refreshing} onClick={() => setOffset(offset + pageSize)}>Next subscription groups</button>
    </nav>
  </section>;
}

function Assessment({ assessment }: { assessment: Page['assessment'] }) {
  if (!assessment) return null;
  const { waits, shares } = assessment;
  return <section aria-label="Subscription usage assessment" className="space-y-2 rounded border border-slate-300 p-3">
    <h3 className="font-semibold">Operator workload assessment</h3>
    <p>All admitted attempts in the selected run or across all runs, independent of this page. Shares use reported measurements; missing telemetry can change the overall share.</p>
    <p>Admitted primary turns: {assessment.primary_turns} · {assessment.delegation_decisions} applied delegations · {assessment.wait_decisions} applied waits · {assessment.repair_debits} repair debits</p>
    <p>{assessment.all_attempts} admitted attempts · {assessment.preferred_attempts} preferred route · {assessment.fallback_attempts} approved fallback · {assessment.unknown_route_attempts} unknown route attribution</p>
    <p>Recorded wait until next primary admission: {measured(waits.elapsed_ms)}{waits.elapsed_ms === null ? '' : ' ms'}. This includes scheduling delay.</p>
    <p>{waits.continued} continued · {waits.unfinished} unfinished · {waits.ended_without_continuation} ended without continuation · {waits.measured_intervals} measured intervals · {waits.unknown_intervals} unknown</p>
    <div className="grid gap-3 text-sm lg:grid-cols-3">
      <PrimaryShare label="Input tokens" share={shares.input_tokens} />
      <PrimaryShare label="Output tokens" share={shares.output_tokens} />
      <PrimaryShare label="Duration" share={shares.duration_ms} />
    </div>
    <p>{assessment.unverified_decisions} recorded applications could not be verified from retained source records. Task acceptance by a primary is separate from human approval.</p>
    <p>Outcomes and latest fallback reasons follow the complete groups on this page. A task can appear in more than one route or currency group.</p>
  </section>;
}

function PrimaryShare({ label, share }: { label: string; share: Share }) {
  return <div className="space-y-1">
    <p className="font-medium">{label} primary measured share: {share.share === null ? 'Unknown' : `${(share.share * 100).toFixed(1)}%`}</p>
    <p>{measured(share.numerator)} / {measured(share.denominator)} measured units</p>
    <p>Primary: {share.numerator_measured_attempts} of {share.primary_attempts} measured; {share.numerator_unknown_attempts} unknown</p>
    <p>All: {share.denominator_measured_attempts} of {share.all_attempts} measured; {share.denominator_unknown_attempts} unknown ({share.coverage === null ? 'Unknown' : `${(share.coverage * 100).toFixed(1)}%`} coverage)</p>
  </div>;
}

function GroupOutcome({ outcome }: { outcome: Outcome }) {
  return <div className="space-y-1 border-t border-slate-300 pt-2 text-sm">
    <h4 className="font-semibold">Recorded outcomes for admitted attempts</h4>
    <p>{outcome.distinct_tasks} distinct tasks · {outcome.terminal_tasks} currently terminal · {outcome.attempts} admitted attempts</p>
    <p>{outcome.verified_results} source-verified results ({outcome.failed_results} failed) · {outcome.unverified_results} unverified results · {outcome.pending_results} without a result</p>
    <p>{outcome.applied_decisions} decision applications with matching source records · {outcome.completed_handoffs} completed handoffs · Tasks accepted by primary: {outcome.task_acceptances}</p>
    <p>Latest recorded disposition: {outcome.latest_result_disposition ?? 'Unknown'}</p>
    {outcome.latest_fallback_reason !== null ? <p>{outcome.fallback_attempts} approved fallback attempts · Latest reason: {outcome.latest_fallback_reason}</p> : null}
    <Link href={`/runs/${outcome.run_id}#subscription-tasks`}>Inspect tasks and attempts for run {outcome.run_id}</Link>
  </div>;
}
