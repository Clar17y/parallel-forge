'use client';
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
      {!digest && <p>No plan has been published yet.</p>}
      {artifact.loading && <p role="status">Loading plan evidence…</p>}
      {(artifact.failed || (artifact.value && !plan)) && (
        <p role="alert">
          Plan evidence unavailable. <button onClick={artifact.refresh}>Retry</button>
        </p>
      )}
      {plan && <PlanContent plan={plan} />}
      <dl>
        <dt>Plan digest</dt>
        <dd>{digest ?? 'Not yet available'}</dd>
        <dt>Base commit</dt>
        <dd>{projection.run.base_sha ?? 'Unavailable'}</dd>
        <dt>Policy version</dt>
        <dd>{projection.project.policy_version}</dd>
        <dt>Runner</dt>
        <dd>{projection.security.runner_mode}</dd>
        <dt>Token limit</dt>
        <dd>{projection.budgets.token_limit}</dd>
        <dt>Cost limit</dt>
        <dd>{projection.budgets.cost_limit_minor} minor currency units</dd>
        <dt>Duration limit</dt>
        <dd>{projection.budgets.duration_limit_seconds} seconds</dd>
      </dl>
    </section>
  );
}
