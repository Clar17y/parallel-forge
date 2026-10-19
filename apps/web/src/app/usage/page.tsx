'use client';

import { LoadingStatus } from '@/components/ui/loading-status';
import Link from 'next/link';
import { useEffect, useState } from 'react';
import { useApi } from '@/hooks/use-api';
import { SubscriptionUsageSummary } from '@/components/subscription-usage/usage-summary';
import { getProviderCue } from '@/components/ui/provider-badge';
import { HistoricalUsageReport } from '@/components/task-usage/historical-usage-report';
import { formatDurationMs } from '@/lib/format';
import type { components } from '@/lib/api/schema';

export default function UsagePage() {
  const [offset, setOffset] = useState(0);
  const [activeView, setActiveView] = useState<string>('historical');
  const showHistorical = activeView === 'all' || activeView === 'historical';
  const showSubscription = activeView === 'all' || activeView === 'subscription';
  const showApi = activeView === 'all' || activeView === 'api';
  const usage = useApi<components['schemas']['ListPage_UsageItem_']>(showApi ? `/usage?offset=${offset}&limit=25` : null);

  useEffect(() => {
    function handleHash() {
      if (typeof window === 'undefined') return;
      const hash = window.location.hash.replace('#', '');
      if (hash === 'historical-usage') {
        setActiveView('historical');
      } else if (hash === 'subscription-usage') {
        setActiveView('subscription');
      } else if (hash === 'api-usage' || hash === 'api-usage-heading') {
        setActiveView('api');
      } else if (hash === 'all') {
        setActiveView('all');
      } else {
        setActiveView('historical');
      }
    }
    handleHash();
    window.addEventListener('hashchange', handleHash);
    return () => window.removeEventListener('hashchange', handleHash);
  }, []);

  function selectView(view: string, hash: string) {
    setActiveView(view);
    if (typeof window !== 'undefined') {
      window.location.hash = hash;
    }
  }

  return (
    <div className="usage-layout">
      <header className="page-header">
        <h1>Usage</h1>
        <p className="page-description">
          Observed task baselines, subscription operator assessment, and recorded API token usage.
        </p>
        <nav aria-label="Usage report views" style={{ display: 'flex', flexWrap: 'wrap', gap: '8px', marginTop: '12px' }}>
          <button
            type="button"
            className="button"
            data-variant={activeView === 'all' ? 'primary' : 'quiet'}
            aria-pressed={activeView === 'all'}
            onClick={() => selectView('all', 'all')}
            style={{ fontSize: '0.8125rem', padding: '4px 10px' }}
          >
            All reports
          </button>
          <button
            type="button"
            className="button"
            data-variant={activeView === 'historical' ? 'primary' : 'quiet'}
            aria-pressed={activeView === 'historical'}
            onClick={() => selectView('historical', 'historical-usage')}
            style={{ fontSize: '0.8125rem', padding: '4px 10px' }}
          >
            Historical baselines
          </button>
          <button
            type="button"
            className="button"
            data-variant={activeView === 'subscription' ? 'primary' : 'quiet'}
            aria-pressed={activeView === 'subscription'}
            onClick={() => selectView('subscription', 'subscription-usage')}
            style={{ fontSize: '0.8125rem', padding: '4px 10px' }}
          >
            Subscription assessment
          </button>
          <button
            type="button"
            className="button"
            data-variant={activeView === 'api' ? 'primary' : 'quiet'}
            aria-pressed={activeView === 'api'}
            onClick={() => selectView('api', 'api-usage')}
            style={{ fontSize: '0.8125rem', padding: '4px 10px' }}
          >
            API usage
          </button>
        </nav>
      </header>

      <div id="historical-usage" style={{ display: showHistorical ? 'block' : 'none' }}>
        <HistoricalUsageReport active={showHistorical} />
      </div>

      <div id="subscription-usage" style={{ display: showSubscription ? 'block' : 'none' }}>
        <SubscriptionUsageSummary active={showSubscription} />
      </div>

      <section
        id="api-usage"
        className="api-usage"
        aria-labelledby="api-usage-heading"
        style={{
          display: showApi ? 'block' : 'none',
          marginTop: '24px',
        }}
      >
        <div className="section-heading" style={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', justifyContent: 'space-between', gap: '12px', marginBottom: '16px' }}>
          <div>
            <h2 id="api-usage-heading" style={{ margin: 0 }}>API usage</h2>
            <p className="meta" style={{ margin: '4px 0 0 0' }}>
              Recorded tokens and duration, grouped by project, run, provider, model and currency. Costs are estimates; unpriced calls are excluded from known cost.
            </p>
          </div>
          <button onClick={usage.refresh} disabled={usage.loading}>Refresh usage</button>
        </div>

        {usage.loading && <LoadingStatus>Loading usage…</LoadingStatus>}
        {usage.failed && (
          <p role="alert">
            Usage unavailable. <button onClick={usage.refresh}>Retry</button>
          </p>
        )}

        {usage.value && (
          <>
            {!usage.value.items.length && <p>No usage on this page.</p>}
            <div className="usage-list" style={{ display: 'grid', gap: '12px' }}>
              {usage.value.items.map(item => {
                const cue = getProviderCue(item.provider);
                return (
                  <article
                    className="usage-row disclosure-card"
                    style={{ padding: '12px 16px', margin: 0, minWidth: 0, overflowWrap: 'anywhere' }}
                    key={`${item.project_id}:${item.run_id}:${item.provider}:${item.model}:${item.currency}`}
                  >
                    <div style={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', justifyContent: 'space-between', gap: '8px', marginBottom: '8px' }}>
                      <div style={{ display: 'flex', alignItems: 'center', gap: '8px', minWidth: 0 }}>
                        <span className="provider-badge-indicator" aria-hidden="true" data-provider={cue.tone} />
                        <span className="usage-title" style={{ fontWeight: 600, fontSize: '0.9375rem' }}>
                          {item.provider} / {item.model}
                        </span>
                        <span className="meta" style={{ margin: 0 }}>·</span>
                        <span className="meta" style={{ margin: 0 }}>
                          <Link href={`/projects/${item.project_id}`}>Project {item.project_id.slice(0, 8)}</Link> · <Link href={`/runs/${item.run_id}`}>Run {item.run_id.slice(0, 8)}</Link>
                        </span>
                      </div>
                      <div style={{ display: 'flex', alignItems: 'center', gap: '12px', fontSize: '0.875rem' }}>
                        <span style={{ fontWeight: 600 }}>
                          {item.known_cost_minor} {item.currency} minor units
                        </span>
                      </div>
                    </div>

                    <div style={{ display: 'flex', flexWrap: 'wrap', gap: '16px 24px', fontSize: '0.8125rem' }}>
                      <div>
                        <span style={{ color: 'var(--muted)', marginRight: '6px' }}>Tokens:</span>
                        <span>{item.input_tokens.toLocaleString()} in / {item.output_tokens.toLocaleString()} out</span>
                      </div>
                      <div>
                        <span style={{ color: 'var(--muted)', marginRight: '6px' }}>Duration:</span>
                        <span>{item.duration_ms != null ? formatDurationMs(item.duration_ms) : 'Unknown'}</span>
                      </div>
                      <div>
                        <span style={{ color: 'var(--muted)', marginRight: '6px' }}>Calls:</span>
                        <span>{item.model_calls.toLocaleString()}</span>
                      </div>
                      {item.unpriced_calls > 0 && (
                        <div style={{ color: 'var(--warning-text, #b45309)', fontWeight: 500 }}>
                          {item.unpriced_calls} {item.unpriced_calls === 1 ? 'call' : 'calls'} with unknown cost
                        </div>
                      )}
                    </div>

                    <details className="disclosure-card" style={{ marginTop: '8px', borderTop: '1px solid var(--border, #e2e8f0)', paddingTop: '6px' }}>
                      <summary className="disclosure-summary" style={{ fontSize: '0.75rem', cursor: 'pointer', color: 'var(--muted)' }}>
                        Exact values & identifiers
                      </summary>
                      <div className="disclosure-body" style={{ fontSize: '0.75rem', display: 'grid', gap: '4px', paddingTop: '6px' }}>
                        <div>Project ID: <code>{item.project_id}</code></div>
                        <div>Run ID: <code>{item.run_id}</code></div>
                        <div>Currency: <code>{item.currency ?? 'unknown'}</code></div>
                        <div>Raw duration: <code>{item.duration_ms != null ? `${item.duration_ms} ms` : 'null'}</code></div>
                      </div>
                    </details>
                  </article>
                );
              })}
            </div>
          </>
        )}

        {(offset > 0 || (usage.value?.items && usage.value.items.length > 0)) && (
          <nav aria-label="Usage pages" className="pagination-bar" style={{ display: 'flex', gap: '8px', marginTop: '16px' }}>
            <button disabled={offset === 0 || usage.loading} onClick={() => setOffset(Math.max(0, offset - 25))}>Previous</button>
            <button disabled={!usage.value?.truncated || usage.loading} onClick={() => setOffset(offset + 25)}>Next</button>
          </nav>
        )}
      </section>
    </div>
  );
}
