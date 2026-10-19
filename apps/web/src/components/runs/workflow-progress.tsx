import type { components } from '@/lib/api/schema';
import { workflowSteps } from './run-presentation';
import { Disclosure } from '@/components/ui/disclosure';

export function WorkflowProgress({ projection, stale }: { projection: components['schemas']['RunProjection']; stale: boolean }) {
  const steps = workflowSteps(projection);
  const currentStep = steps.find(step => step.current);
  const recordedCount = steps.filter(step => step.recorded).length;

  return (
    <section className="workflow" aria-label="Workflow progress">
      <Disclosure className="workflow-details" summary={<div className="summary-line">
          <span>Workflow milestones</span>
          <span className="summary-meta">
            {currentStep
              ? `${currentStep.label} · ${currentStep.current && stale ? 'Last known phase' : currentStep.detail}`
              : `${recordedCount} of ${steps.length} milestones recorded`}
          </span>
        </div>}>
          <ol>
            {steps.map(step => (
              <li key={step.key} aria-current={step.current ? 'step' : undefined} data-recorded={step.recorded}>
                <span className="workflow-label">{step.label}</span>
                <span className="workflow-detail">{step.current && stale ? 'Last known phase' : step.detail}</span>
              </li>
            ))}
          </ol>
          <p className="workflow-note">Milestones reflect recorded evidence, not permission to publish or merge.</p>
      </Disclosure>
    </section>
  );
}
