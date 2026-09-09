'use client';
import { useState } from 'react';
import { useApi } from '@/hooks/use-api';
import type { components } from '@/lib/api/schema';
import { AuditEventCard } from '@/components/audit/audit-event-card';

const ids = [['run_id', 'Run ID'], ['project_id', 'Project ID'], ['actor_id', 'Actor ID'], ['operation_id', 'Operation ID']] as const;
export default function AuditPage() {
  const [offset, setOffset] = useState(0);
  const [filters, setFilters] = useState('');
  const audit = useApi<components['schemas']['ListPage_AuditItem_']>(`/audit?offset=${offset}&limit=25${filters ? `&${filters}` : ''}`);
  return <><h1>Audit</h1><p>Persisted operator actions and causal run events. Operation statuses describe their current state; they do not rewrite what an earlier event recorded.</p>
    <form onSubmit={event => {
      event.preventDefault();
      const values = new FormData(event.currentTarget);
      const query = new URLSearchParams();
      for (const [key, value] of values) if (typeof value === 'string' && value.trim()) query.set(key, value.trim());
      setOffset(0); setFilters(query.toString());
    }}>
      {ids.map(([name, label]) => <label key={name}>{label}<input name={name} pattern="[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}" title="Enter a UUID" /></label>)}
      <label>Current operation status<select name="operation_status" defaultValue=""><option value="">Any</option>
        {['PENDING', 'SUCCEEDED', 'FAILED', 'NEEDS_RECONCILIATION'].map(status => <option key={status} value={status}>{status}</option>)}</select></label>
      <button type="submit">Apply filters</button>
    </form>
    <button onClick={audit.refresh} disabled={audit.loading}>Refresh audit</button>
    {audit.loading && <p role="status">Loading audit…</p>}
    {audit.failed && <p role="alert">Audit unavailable. <button onClick={audit.refresh}>Retry</button></p>}
    {audit.value && <>{!audit.value.items.length && <p>No matching audit events on this page.</p>}
      {audit.value.items.map(item => <AuditEventCard key={`${item.source}:${item.id}`} item={item} />)}</>}
    <nav aria-label="Audit pages"><button disabled={offset === 0 || audit.loading} onClick={() => setOffset(Math.max(0, offset - 25))}>Previous</button>
      <button disabled={!audit.value?.truncated || audit.loading} onClick={() => setOffset(offset + 25)}>Next</button></nav>
  </>;
}
