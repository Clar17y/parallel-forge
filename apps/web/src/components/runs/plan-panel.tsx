'use client';
import { LoadingStatus } from '@/components/ui/loading-status';
import { useApi } from '@/hooks/use-api';
import type { components } from '@/lib/api/schema';
import { parsePlan, PlanContent } from './plan-content';

export function PlanPanel({ projection }: { projection: components['schemas']['RunProjection'] }) {
  const digest = projection.plan.output_artifact_digest;
  const artifact = useApi<components['schemas']['ArtifactTextResponse']>(digest ? `/artifacts/${digest}/text` : null);
  const plan = artifact.value?.digest === digest ? parsePlan(artifact.value.text) : null;
  return (
    <section aria-label="Plan evidence">
      <h2>Plan</h2>
      {!digest && (
        <div className="empty-state-notice">
          <p>No plan has been published yet.</p>
          <p className="meta">
            {projection.run.state === 'PLANNING' || projection.run.state === 'CREATED'
              ? 'Forge is preparing the plan. When completed, proposed steps and affected scope will appear here for review.'
              : projection.run.state === 'CANCELLED' || projection.run.state === 'FAILED'
                ? 'Execution ended before a plan artifact was generated.'
                : 'Implementation proceeded with retained workspace policy.'}
          </p>
        </div>
      )}
      {artifact.loading && <LoadingStatus>Loading plan evidence…</LoadingStatus>}
      {(artifact.failed || (artifact.value && !plan)) && (
        <p role="alert">
          Plan evidence unavailable. <button onClick={artifact.refresh}>Retry</button>
        </p>
      )}
      {plan && <PlanContent plan={plan} />}
      <details className="disclosure-card mt-4">
        <summary className="disclosure-summary cursor-pointer text-sm font-semibold">
          Technical provenance and policy limits
        </summary>
        <div className="disclosure-body pt-2">
          <dl className="key-values">
            <dt>Plan digest</dt>
            <dd>{digest ?? 'Not yet available'}</dd>
            <dt>Base commit</dt>
            <dd>{projection.run.base_sha ?? 'Unavailable'}</dd>
            <dt>Policy version</dt>
            <dd>{projection.project.policy_version}</dd>
            <dt>Runner</dt>
            <dd>{projection.security.runner_mode}</dd>
            <dt>Token limit</dt>
            <dd>{projection.budgets.token_limit ?? 'Unbounded'}</dd>
            <dt>Cost limit</dt>
            <dd>{projection.budgets.cost_limit_minor != null ? `${projection.budgets.cost_limit_minor} minor currency units` : 'Unbounded'}</dd>
            <dt>Duration limit</dt>
            <dd>{projection.budgets.duration_limit_seconds != null ? `${projection.budgets.duration_limit_seconds} seconds` : 'Unbounded'}</dd>
          </dl>
        </div>
      </details>
    </section>
  );
}
