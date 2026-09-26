'use client';
import { useApi } from '@/hooks/use-api';
import type { components } from '@/lib/api/schema';

const sections = [['assumptions', 'Assumptions'], ['affected_components', 'Affected components'], ['steps', 'Steps'],
  ['required_checks', 'Required checks'], ['risks', 'Risks'], ['security_considerations', 'Security considerations'], ['dependency_changes', 'Dependency changes']] as const;
type Plan = { summary: string; owned_paths?: string[] } & Record<typeof sections[number][0], string[]>;
function parsePlan(text: string): Plan | null {
  try {
    const value = JSON.parse(text);
    if (!value || typeof value.summary !== 'string' || value.summary.length > 10000) return null;
    for (const [key] of sections) {
      if (!Array.isArray(value[key]) || value[key].length > 100 || !value[key].every((item: unknown) => typeof item === 'string' && item.length <= 5000)) return null;
    }
    if ('owned_paths' in value && (!Array.isArray(value.owned_paths) || value.owned_paths.length > 64 || !value.owned_paths.every((item: unknown) => typeof item === 'string' && item.length > 0 && item.length <= 5000))) return null;
    return value as Plan;
  } catch { return null; }
}

export function PlanPanel({ projection }: { projection: components['schemas']['RunProjection'] }) {
  const digest = projection.plan.output_artifact_digest;
  const artifact = useApi<components['schemas']['ArtifactTextResponse']>(digest ? `/artifacts/${digest}/text` : null);
  const plan = artifact.value?.digest === digest ? parsePlan(artifact.value.text) : null;
  return <section aria-label="Plan evidence"><h2>Plan</h2>
    {!digest && <p>No plan has been published yet.</p>}
    {artifact.loading && <p role="status">Loading plan evidence…</p>}
    {(artifact.failed || (artifact.value && !plan)) && <p role="alert">Plan evidence unavailable. <button onClick={artifact.refresh}>Retry</button></p>}
    {plan && <><p>{plan.summary}</p>{plan.owned_paths !== undefined && <section><h3>Writable paths</h3>
      {plan.owned_paths.length ? <ol>{plan.owned_paths.map((path, index) => <li key={index}>{path}</li>)}</ol> : <p>No writable paths.</p>}
    </section>}{sections.map(([key, label]) => <section key={key}><h3>{label}</h3>
      {plan[key].length ? <ol>{plan[key].map((item, index) => <li key={index}>{item}</li>)}</ol> : <p>None recorded</p>}
    </section>)}</>}
    <dl><dt>Plan digest</dt><dd>{digest ?? 'Not yet available'}</dd>
      <dt>Base commit</dt><dd>{projection.run.base_sha ?? 'Unavailable'}</dd>
      <dt>Policy version</dt><dd>{projection.project.policy_version}</dd>
      <dt>Runner</dt><dd>{projection.security.runner_mode}</dd>
      <dt>Token limit</dt><dd>{projection.budgets.token_limit}</dd>
      <dt>Cost limit</dt><dd>{projection.budgets.cost_limit_minor} minor currency units</dd>
      <dt>Duration limit</dt><dd>{projection.budgets.duration_limit_seconds} seconds</dd>
    </dl>
  </section>;
}
