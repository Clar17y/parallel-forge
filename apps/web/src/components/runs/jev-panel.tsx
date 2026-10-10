'use client';

import { LoadingStatus } from '@/components/ui/loading-status';
import { useApi } from '@/hooks/use-api';
import type { components } from '@/lib/api/schema';
import { readableLabel } from './run-presentation';

export function JevPanel({ runId }: { runId: string }) {
  const report = useApi<components['schemas']['JevReportResponse']>(`/runs/${encodeURIComponent(runId)}/jev`);
  return <section aria-label="Jev advisory usage"><h2>Jev advisory usage</h2>
    <p>Read-only worker report. Source excerpts leave the project only when its immutable policy allows remote processing.</p>
    <button disabled={report.loading} onClick={report.refresh}>Refresh Jev report</button>
    {report.loading && <LoadingStatus>Loading Jev report…</LoadingStatus>}
    {report.failed && <p role="alert">Jev report unavailable. <button onClick={report.refresh}>Retry</button></p>}
    {report.value && <>
      {report.value.calls === 0 && !Object.keys(report.value.by_kind).length && !Object.keys(report.value.by_status).length && <p>No Jev activity has been recorded for this run.</p>}
      <p>Configured mode: {readableLabel(report.value.requested_mode)} · observed mode: {readableLabel(report.value.effective_mode)}</p>
      {report.value.requested_mode === 'shadow' && <p>Shadow mode records the ranking projection while agents keep the original results.</p>}
      <p>Requested model: {report.value.requested_model ?? 'Legacy global configuration'} · reported model: {report.value.actual_model ?? 'Not yet observed'}</p>
      <p>Worker availability: {readableLabel(report.value.availability)}</p>
      <p>{report.value.attempts} attempt{report.value.attempts === 1 ? '' : 's'} · {report.value.calls} provider calls · {report.value.cache_hits} cache hits · {report.value.unknown} unknown outcomes</p>
      <p>{report.value.actual_input_units} reported input tokens · {report.value.actual_output_units} reported output tokens</p>
      <p>{report.value.reserved_input_units} reserved allowance · {report.value.duration_ms} ms total</p>
      <p>{report.value.remaining_requests} requests and {report.value.remaining_input_units} input allowance remaining</p>
      <p>Input allowance reserves one unit per request byte, or more if reported usage is higher. It is separate from reported tokens.</p>
      <p>Recorded review focus evaluation: {report.value.review_focus_available ? 'successful' : 'none recorded'}</p>
      <h3>Activity by kind</h3>{Object.keys(report.value.by_kind).length
        ? <ul>{Object.entries(report.value.by_kind).map(([kind, count]) => <li key={kind}>{readableLabel(kind)}: {count}</li>)}</ul>
        : <p>No activity by kind recorded.</p>}
      <h3>Activity by status</h3>{Object.keys(report.value.by_status).length
        ? <ul>{Object.entries(report.value.by_status).map(([status, count]) => <li key={status}>{readableLabel(status)}: {count}</li>)}</ul>
        : <p>No activity by status recorded.</p>}
      {Object.keys(report.value.by_diagnostic ?? {}).length > 0 && <>
        <h3>Recorded limitations</h3>
        <ul>{Object.entries(report.value.by_diagnostic ?? {}).map(([reason, count]) => <li key={reason}>{readableLabel(reason)}: {count}</li>)}</ul>
      </>}
    </>}
  </section>;
}
