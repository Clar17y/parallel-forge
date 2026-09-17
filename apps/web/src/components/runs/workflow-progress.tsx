import type { components } from '@/lib/api/schema';
import { workflowSteps } from './run-presentation';

export function WorkflowProgress({ projection, stale }: { projection: components['schemas']['RunProjection']; stale: boolean }) {
  return <section className="workflow" aria-label="Workflow progress">
    <ol>{workflowSteps(projection).map(step => <li key={step.key} aria-current={step.current ? 'step' : undefined} data-recorded={step.recorded}>
      <span className="workflow-label">{step.label}</span><span className="workflow-detail">{step.current && stale ? 'Last known phase' : step.detail}</span>
    </li>)}</ol>
    <p className="workflow-note">Milestones reflect recorded evidence, not permission to publish or merge.</p>
  </section>;
}
