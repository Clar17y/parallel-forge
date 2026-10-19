'use client';

import { LoadingStatus } from '@/components/ui/loading-status';
import { useRef, useState, type FormEvent } from 'react';
import { useApi } from '@/hooks/use-api';
import type { components } from '@/lib/api/schema';
import { AuditEventCard } from '@/components/audit/audit-event-card';
import { Button } from '@/components/ui/button';

const pageSize = 25;

export default function AuditPage() {
  const [offset, setOffset] = useState(0);
  const [filters, setFilters] = useState('');
  const [projectId, setProjectId] = useState('');
  const formRef = useRef<HTMLFormElement>(null);
  const page = Math.floor(offset / pageSize) + 1;

  const projects = useApi<components['schemas']['ProjectResponse'][]>('/projects');
  const projectList = Array.isArray(projects.value) ? projects.value : [];

  const audit = useApi<components['schemas']['ListPage_AuditItem_']>(
    `/audit?offset=${offset}&limit=${pageSize}${filters ? `&${filters}` : ''}`
  );

  function handleFilterSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const values = new FormData(event.currentTarget);
    const query = new URLSearchParams();
    for (const [key, value] of values) {
      if (typeof value === 'string' && value.trim()) {
        query.set(key, value.trim());
      }
    }
    setOffset(0);
    setFilters(query.toString());
  }

  function handleClearFilters() {
    formRef.current?.reset();
    setProjectId('');
    setOffset(0);
    setFilters('');
  }

  return (
    <div className="audit-layout">
      <header className="page-header">
        <h1>Audit</h1>
        <p className="page-description">
          Persisted operator actions and causal run events. Operation statuses describe their current state; they do not rewrite what an earlier event recorded.
        </p>
      </header>

      <section className="filter-panel" aria-label="Audit filters">
        <form ref={formRef} onSubmit={handleFilterSubmit}>
          <div className="filter-grid">
            <div className="form-field">
              <label htmlFor="project_id">Project ID</label>
              <input
                id="project_id"
                name="project_id"
                value={projectId}
                onChange={e => setProjectId(e.target.value)}
                pattern="[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
                title="Enter a UUID"
                placeholder="All accessible projects or exact UUID"
                list="audit-project-options"
              />
              <datalist id="audit-project-options">
                {projectList.map(p => (
                  <option key={p.id} value={p.id}>{p.name}</option>
                ))}
              </datalist>
              {projectList.length > 0 && (
                <select
                  aria-label="Authorized project picker"
                  value={projectList.some(p => p.id === projectId) ? projectId : ''}
                  onChange={e => setProjectId(e.target.value)}
                  style={{ fontSize: '0.75rem', marginTop: '4px' }}
                >
                  <option value="">Choose an authorized project…</option>
                  {projectList.map(p => (
                    <option key={p.id} value={p.id}>{p.name} ({p.id})</option>
                  ))}
                </select>
              )}
              <span className="field-hint">Select accessible project or enter exact UUID</span>
            </div>

            <div className="form-field">
              <label htmlFor="run_id">Run ID</label>
              <input
                id="run_id"
                name="run_id"
                pattern="[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
                title="Enter a UUID"
                placeholder="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
              />
              <span className="field-hint">Optional exact UUID</span>
            </div>

            <div className="form-field">
              <label htmlFor="operation_status">Current operation status</label>
              <select id="operation_status" name="operation_status" defaultValue="">
                <option value="">Any</option>
                {['PENDING', 'SUCCEEDED', 'FAILED', 'NEEDS_RECONCILIATION'].map(status => (
                  <option key={status} value={status}>{status}</option>
                ))}
              </select>
              <span className="field-hint">Current state of linked operation</span>
            </div>

            <details className="disclosure-card" style={{ gridColumn: '1 / -1', marginTop: '4px' }}>
              <summary className="disclosure-summary" style={{ fontSize: '0.8125rem', cursor: 'pointer' }}>Advanced exact filters</summary>
              <div className="filter-grid" style={{ marginTop: '8px' }}>
                <div className="form-field">
                  <label htmlFor="actor_id">Actor ID</label>
                  <input
                    id="actor_id"
                    name="actor_id"
                    pattern="[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
                    title="Enter a UUID"
                    placeholder="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
                  />
                  <span className="field-hint">Optional exact UUID</span>
                </div>
                <div className="form-field">
                  <label htmlFor="operation_id">Operation ID</label>
                  <input
                    id="operation_id"
                    name="operation_id"
                    pattern="[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
                    title="Enter a UUID"
                    placeholder="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
                  />
                  <span className="field-hint">Optional exact UUID</span>
                </div>
              </div>
            </details>
          </div>
          <div className="filter-actions">
            <Button type="submit" variant="primary">Apply filters</Button>
            <Button type="button" variant="secondary" onClick={handleClearFilters}>Clear filters</Button>
          </div>
        </form>
      </section>

      <div className="results-bar">
        <div>
          <h2 style={{ margin: 0, fontSize: '1rem' }}>Audit timeline</h2>
          {audit.value && (
            <span className="meta">
              {audit.value.items.length === 0
                ? 'No matching events'
                : `Showing ${audit.value.items.length} events · page ${page}`}
            </span>
          )}
        </div>
        <Button onClick={audit.refresh} disabled={audit.loading}>Refresh audit</Button>
      </div>

      {audit.loading && <LoadingStatus>Loading audit…</LoadingStatus>}
      {audit.failed && (
        <p role="alert">
          Audit unavailable. <Button onClick={audit.refresh}>Retry</Button>
        </p>
      )}

      {audit.value && (
        <>
          {!audit.value.items.length && <p className="empty-state">No matching audit events on this page.</p>}
          {audit.value.items.map(item => (
            <AuditEventCard key={`${item.source}:${item.id}`} item={item} />
          ))}
        </>
      )}

      <nav aria-label="Audit pages" className="pagination-bar">
        <Button
          disabled={offset === 0 || audit.loading}
          onClick={() => setOffset(Math.max(0, offset - pageSize))}
        >
          Previous
        </Button>
        <span className="meta">Page {page}</span>
        <Button
          disabled={!audit.value?.truncated || audit.loading}
          onClick={() => setOffset(offset + pageSize)}
        >
          Next
        </Button>
      </nav>
    </div>
  );
}
