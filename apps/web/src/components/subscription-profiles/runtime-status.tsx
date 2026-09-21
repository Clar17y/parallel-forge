'use client';

import { useState } from 'react';
import { useApi } from '@/hooks/use-api';
import type { components } from '@/lib/api/schema';

type Page = components['schemas']['SubscriptionRuntimeStatusPage'];
type Route = components['schemas']['SubscriptionRuntimeRouteView'];
type Reason = Route['effective_reason'];
const pageSize = 25;
const refreshStorageKey = 'forge:subscription-runtime:refresh';
const states = {
  current: 'Current report',
  stale: 'Stale report — current registration is unknown',
  stopped: 'Worker stopped reporting',
};
const reasonTitles: Record<Reason, string> = {
  ready: 'Capability ready',
  missing_executable: 'Official client missing',
  executable_digest_mismatch: 'Client build differs',
  version_mismatch: 'Client version differs',
  unsupported_model_or_effort: 'Model or effort unsupported',
  signed_out: 'Signed out',
  account_authentication_unproved: 'Account authentication unproved',
  subscription_route_unbound: 'Subscription route unproved',
  isolation_unproved: 'Isolation unproved',
  evidence_missing: 'Capability evidence missing',
  evidence_stale_or_invalid: 'Capability evidence stale or invalid',
  provider_unsupported: 'Provider client unsupported',
  configuration_invalid: 'Installation configuration invalid',
  unknown: 'Readiness unknown',
  stale_worker: 'Worker report stale',
  quota_exhausted: 'Quota exhausted',
};

function loginGuidance(client: string) {
  if (client === 'codex_app_server') {
    return <>Run <code>codex login</code> in the official Codex client and select the intended personal ChatGPT account, then refresh.</>;
  }
  if (client === 'claude_code') {
    return <>Run <code>claude auth login</code> in the official Claude client, verify with <code>claude auth status</code>, then refresh.</>;
  }
  if (client === 'antigravity_cli' || client === 'gemini_cli') {
    return <>Sign in with Google inside the official Antigravity client, then refresh. Sign-in does not override an isolation blocker.</>;
  }
  return <>Complete sign-in inside the configured official client, then refresh.</>;
}

function guidance(route: Route) {
  switch (route.effective_reason) {
    case 'ready':
      return 'Current durable evidence matches this exact subscription route. Invocation still rechecks authority.';
    case 'missing_executable':
      return 'Install the configured official client at the operator-managed location, then restart or refresh the worker.';
    case 'executable_digest_mismatch':
    case 'version_mismatch':
      return 'Restore the pinned supported official-client build or update the installation manifest and publish fresh evidence.';
    case 'unsupported_model_or_effort':
      return 'Select a supported model and effort combination, or revise the exact installation entry.';
    case 'signed_out':
    case 'account_authentication_unproved':
    case 'subscription_route_unbound':
      return loginGuidance(route.client);
    case 'isolation_unproved':
      return 'Signing in cannot resolve this isolation blocker. Keep the route blocked until an operator-authorized capability workflow publishes complete isolation evidence.';
    case 'evidence_missing':
      return 'Run subscription-capabilities status and verify offline, then explicitly authorize the bounded evidence publication workflow for this installation and scope.';
    case 'evidence_stale_or_invalid':
      return 'Inspect the pinned client and installation, then publish fresh bounded evidence. Stale or invalid evidence never grants readiness.';
    case 'provider_unsupported':
      return 'This client is not production-admitted. Choose an admitted official client; sign-in alone cannot enable it.';
    case 'configuration_invalid':
      return 'Inspect the closed installation manifest and exact quota mapping, correct them, then restart the worker.';
    case 'stale_worker':
      return 'Start or restart the Forge worker and wait for a current readiness report before attempting this route.';
    case 'quota_exhausted':
      return 'Wait for the retained reset or next-probe time. Forge will not switch to API billing, credits, or a paid fallback.';
    case 'unknown':
      return 'Readiness could not be established. Inspect the installation and refresh; unknown never grants admission.';
  }
}

function QuotaStatus({ route }: { route: Route }) {
  if (route.quota === 'eligible') {
    return <p>Quota gate: eligible to attempt; this is not a remaining-allowance balance{route.quota_revision == null ? '.' : ` (revision ${route.quota_revision}).`}</p>;
  }
  if (route.quota === 'unknown') {
    return <p>Quota gate: unknown; this is not a zero-balance or availability claim.</p>;
  }
  return <p>Quota gate: blocked{route.quota_revision == null ? '.' : ` at revision ${route.quota_revision}.`}{route.quota_reset_at ? <> Retained reset <time dateTime={route.quota_reset_at}>{route.quota_reset_at}</time>.</> : null}{route.quota_next_probe_at ? <> Retained next probe <time dateTime={route.quota_next_probe_at}>{route.quota_next_probe_at}</time>.</> : null}</p>;
}

function RouteReadiness({ route }: { route: Route }) {
  const evidence = route.evidence ?? [];
  return <li className="space-y-2 rounded border p-3">
    <p>{route.provider} / {route.client} / {route.model} · {route.effort} · {route.auth_mode} · {route.billing_mode}</p>
    <h4>{reasonTitles[route.effective_reason]}</h4>
    <p>Configured: {route.configured ? 'yes' : 'no'} · Admitted: {route.admitted ? 'yes' : 'no'} · Evidence schema: {route.schema_version}</p>
    <p>{guidance(route)}</p>
    <QuotaStatus route={route} />
    {evidence.length === 0 ? <p>Capability evidence: none retained for this route.</p> : <ul aria-label={`Capability evidence for ${route.provider} ${route.model}`}>
      {evidence.map(item => <li key={`${item.scope}:${item.evidence_id}:${item.revision}`}>
        {item.scope} · evidence {item.evidence_id} · revision {item.revision} · observed <time dateTime={item.observed_at}>{item.observed_at}</time> · expires <time dateTime={item.expires_at}>{item.expires_at}</time>
      </li>)}
    </ul>}
  </li>;
}

export function SubscriptionRuntimeStatus() {
  const [offset, setOffset] = useState(0);
  const reports = useApi<Page>(`/subscription-runtime?offset=${offset}&limit=${pageSize}`, {
    refreshIntervalMs: 5000,
    keepPreviousOnRefresh: true,
    refreshStorageKey,
  });
  return <section id="client-setup" aria-label="Worker registration" className="my-6 space-y-3 rounded border p-4">
    <h2>Subscription readiness</h2>
    <p>This read-only view uses retained worker, capability-evidence, and quota metadata. It never launches a provider client or reserves quota. Forge never collects or copies provider credentials, API keys, credits, or overage settings.</p>
    <button type="button" disabled={reports.refreshing} onClick={reports.refresh}>Refresh subscription readiness</button>
    {reports.loading ? <p role="status">Loading worker registration…</p> : reports.failed ?
      <p role="alert">Worker registration unavailable. Refresh to retry; current registration is unknown.</p> : reports.value ? <>
        <p>Observed: <time dateTime={reports.value.observed_at}>{reports.value.observed_at}</time>. Reports become stale after {reports.value.fresh_for_seconds} seconds without renewal.</p>
        {reports.refreshing ? <p role="status">Refreshing worker registration…</p> : null}
        {reports.value.workers.length === 0 ? <p>Worker registration is unknown: no reports on this page. Start the Forge worker to publish its registration.</p> :
          <ul className="space-y-3">{reports.value.workers.map(worker => <li key={worker.worker_instance_id} className="rounded border p-3 [overflow-wrap:anywhere]">
            <h3>{states[worker.state]}</h3>
            <p>Instance {worker.worker_instance_id} · last report <time dateTime={worker.last_seen_at}>{worker.last_seen_at}</time></p>
            {worker.routes.length > 0 ? <ul className="space-y-2">{worker.routes.map(route =>
              <RouteReadiness key={`${route.provider}:${route.client}:${route.model}:${route.effort}`} route={route} />)}</ul> :
              worker.state === 'current' ? <p>No subscription routes registered by this worker. It does not confirm sign-in, capability, or remaining allowance. Complete client setup and capability verification before launching these routes.</p> : <p>No routes in this historical report.</p>}
          </li>)}</ul>}
        {reports.value.has_more ? <p>This page is not the complete worker inventory. Inspect the next page before drawing conclusions about other workers.</p> : null}
      </> : null}
    <nav aria-label="Worker report pages" className="flex gap-4">
      <button type="button" disabled={offset === 0 || reports.refreshing} onClick={() => setOffset(Math.max(0, offset - pageSize))}>Previous worker reports</button>
      <span>Page {Math.floor(offset / pageSize) + 1}</span>
      <button type="button" disabled={!reports.value?.has_more || reports.refreshing || reports.failed} onClick={() => setOffset(offset + pageSize)}>Next worker reports</button>
    </nav>
  </section>;
}
