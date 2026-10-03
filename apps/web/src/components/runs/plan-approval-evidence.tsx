import type { ReactNode } from 'react';
import { PlanContent, type Plan } from './plan-content';
import type { PlanEvidence } from './plan-evidence';

export function PlanApprovalEvidence({
  evidence,
  plan,
  rawEvidence,
}: {
  evidence: PlanEvidence;
  plan: Plan;
  rawEvidence?: string;
}): ReactNode {
  return (
    <section aria-label="Plan proposal">
      <PlanContent plan={plan} />
      <dl>
        <dt>Repository</dt>
        <dd>{evidence.repository}</dd>
        <dt>Base</dt>
        <dd>
          {evidence.base_ref} · <code>{evidence.base_sha}</code>
        </dd>
        <dt>Policy version</dt>
        <dd>{evidence.policy_version}</dd>
        <dt>Runner</dt>
        <dd>{evidence.runner_mode === 'trusted_host' ? 'Trusted host · unsandboxed' : 'Docker'}</dd>
        <dt>Remediation limit</dt>
        <dd>
          {evidence.local_remediation_limit} {evidence.local_remediation_limit === 1 ? 'attempt' : 'attempts'}
        </dd>
        <dt>Token limit</dt>
        <dd>{evidence.token_budget} tokens</dd>
        <dt>Cost limit</dt>
        <dd>{evidence.cost_budget_minor} minor currency units</dd>
        <dt>Duration limit</dt>
        <dd>{evidence.duration_budget_seconds} seconds</dd>
      </dl>
      <details>
        <summary>Technical details</summary>
        <dl>
          <dt>Plan digest</dt>
          <dd>
            <code>{evidence.plan_digest}</code>
          </dd>
          <dt>Task digest</dt>
          <dd>
            <code>{evidence.task_digest}</code>
          </dd>
          {'schema_version' in evidence && evidence.schema_version === 2 && (
            <>
              <dt>Result digest</dt>
              <dd>
                <code>{evidence.result_digest}</code>
              </dd>
              <dt>Producer attempt ID</dt>
              <dd>
                <code>{evidence.producer.attempt_id}</code>
              </dd>
              <dt>Producer run ID</dt>
              <dd>
                <code>{evidence.producer.run_id}</code>
              </dd>
              <dt>Producer task ID</dt>
              <dd>
                <code>{evidence.producer.task_id}</code>
              </dd>
              <dt>Producer envelope digest</dt>
              <dd>
                <code>{evidence.producer.envelope_digest}</code>
              </dd>
              <dt>Producer budget digest</dt>
              <dd>
                <code>{evidence.producer.budget_digest}</code>
              </dd>
              <dt>Producer route digest</dt>
              <dd>
                <code>{evidence.producer.route_digest}</code>
              </dd>
            </>
          )}
        </dl>
        <h4>Raw evidence</h4>
        <pre className="policy-document">{rawEvidence ?? JSON.stringify(evidence, null, 2)}</pre>
      </details>
    </section>
  );
}
