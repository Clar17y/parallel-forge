'use client';

import Link from 'next/link';
import { useState } from 'react';
import { useApi } from '@/hooks/use-api';
import { LoadingStatus } from '@/components/ui/loading-status';
import { StatusBadge } from '@/components/ui/status-badge';
import { Disclosure } from '@/components/ui/disclosure';
import { formatDurationMs } from '@/lib/format';
import type { Tone } from '@/components/ui/tone';
import type { components } from '@/lib/api/schema';

function evaluationStatusTone(status: string | null | undefined): Tone {
  switch (status?.toLowerCase()) {
    case 'passed':
    case 'succeeded':
      return 'success';
    case 'failed':
      return 'danger';
    case 'skipped':
      return 'neutral';
    case 'pending':
    case 'running':
      return 'warning';
    default:
      return 'neutral';
  }
}

function suiteStatusTone(status: string | null | undefined): Tone {
  switch (status?.toLowerCase()) {
    case 'passed':
    case 'completed':
    case 'succeeded':
      return 'success';
    case 'failed':
      return 'danger';
    case 'running':
    case 'pending':
      return 'warning';
    default:
      return 'neutral';
  }
}

export default function EvaluationsPage() {
  const [offset, setOffset] = useState(0);
  const evaluations = useApi<components['schemas']['ListPage_EvaluationItem_']>(`/evaluations?offset=${offset}&limit=25`);

  return (
    <div className="evaluations-layout">
      <header className="page-header">
        <h1>Evaluations</h1>
        <p className="page-description">Persisted fixture results and model usage. Pending or missing results do not establish a pass.</p>
      </header>

      <div style={{ display: 'flex', justifyContent: 'flex-end', marginBottom: '16px' }}>
        <button onClick={evaluations.refresh} disabled={evaluations.loading}>Refresh evaluations</button>
      </div>

      {evaluations.loading && <LoadingStatus>Loading evaluations…</LoadingStatus>}
      {evaluations.failed && (
        <p role="alert">
          Evaluations unavailable. <button onClick={evaluations.refresh}>Retry</button>
        </p>
      )}

      {evaluations.value && (
        <>
          {!evaluations.value.items.length && (
            <div className="empty-state" style={{ padding: '24px', border: '1px solid var(--border)', borderRadius: 'var(--radius-sm)' }}>
              <h2>No evaluations recorded</h2>
              <p>Evaluations capture persisted fixture results and model usage from test suites and benchmarks. Start or view runs to generate evaluation evidence.</p>
              <div style={{ display: 'flex', gap: '8px', marginTop: '12px' }}>
                <Link href="/runs" className="button" data-variant="secondary">Browse runs</Link>
                <Link href="/runs/new" className="button" data-variant="primary">Start new run</Link>
              </div>
            </div>
          )}

          {evaluations.value.items.length > 0 && (
            <div className="evaluations-list" style={{ display: 'grid', gap: '12px' }}>
              {evaluations.value.items.map(item => {
                const caseTitle = item.case_key ?? 'Suite without recorded cases';
                const roleModel = [item.role, item.model].filter(Boolean).join(' · ') || (item.role ?? 'Role unavailable');
                return (
                  <article key={`${item.suite_id}:${item.case_id ?? 'suite'}`}>
                    <Disclosure
                      summary={
                        <div className="summary-line">
                          <div className="summary-title" style={{ display: 'flex', alignItems: 'center', gap: '8px', flexWrap: 'wrap' }}>
                            <h2 style={{ fontSize: '0.9375rem', margin: 0, fontWeight: 600 }}>{caseTitle}</h2>
                            <StatusBadge label={item.status ?? 'No case result'} tone={evaluationStatusTone(item.status)} />
                            <StatusBadge label={`Suite: ${item.suite_status}`} tone={suiteStatusTone(item.suite_status)} />
                          </div>
                          <span className="summary-meta">{roleModel}</span>
                        </div>
                      }
                    >
                      <div style={{ display: 'grid', gap: '8px', fontSize: '0.875rem' }}>
                        <p style={{ margin: 0 }}>Suite {item.suite_id} · {item.suite_status}</p>
                        <p style={{ margin: 0 }}>Fixture {item.fixture_version} · Metrics {item.metric_version} · {item.role ?? 'Role unavailable'}</p>
                        <p style={{ margin: 0 }}>Prompt version: {item.prompt_version ?? 'Unavailable'}</p>
                        {item.provider === null || item.model === null ? (
                          <p style={{ margin: 0 }}>Model usage unavailable</p>
                        ) : (
                          <>
                            <p style={{ margin: 0 }}>{item.provider} / {item.model}</p>
                            <p style={{ margin: 0 }}>
                              Input / output tokens: {item.input_tokens ?? 'Unknown'} / {item.output_tokens ?? 'Unknown'} · Duration: {item.duration_ms != null ? formatDurationMs(item.duration_ms) : 'Unknown'}
                            </p>
                            <p style={{ margin: 0 }}>
                              Estimated cost: {item.estimated_cost_minor === null ? 'Unknown' : `${item.estimated_cost_minor} ${item.currency} minor units`}
                            </p>
                          </>
                        )}
                        {item.metrics && (
                          <details className="disclosure-card">
                            <summary className="disclosure-summary">Recorded metrics</summary>
                            <div className="disclosure-body">
                              <pre className="evidence-scroll" style={{ margin: 0 }}>{JSON.stringify(item.metrics, null, 2)}</pre>
                            </div>
                          </details>
                        )}
                        {item.input_artifact_digest && (
                          <p style={{ margin: 0 }}>
                            <a href={`/api/artifacts/${item.input_artifact_digest}/download`}>Input evidence</a> · <code>{item.input_artifact_digest}</code>
                          </p>
                        )}
                        {item.output_artifact_digest && (
                          <p style={{ margin: 0 }}>
                            <a href={`/api/artifacts/${item.output_artifact_digest}/download`}>Output evidence</a> · <code>{item.output_artifact_digest}</code>
                          </p>
                        )}
                      </div>
                    </Disclosure>
                  </article>
                );
              })}
            </div>
          )}
        </>
      )}

      {(offset > 0 || (evaluations.value?.items && evaluations.value.items.length > 0)) && (
        <nav aria-label="Evaluation pages">
          <button disabled={offset === 0 || evaluations.loading} onClick={() => setOffset(Math.max(0, offset - 25))}>Previous</button>
          <button disabled={!evaluations.value?.truncated || evaluations.loading} onClick={() => setOffset(offset + 25)}>Next</button>
        </nav>
      )}
    </div>
  );
}
