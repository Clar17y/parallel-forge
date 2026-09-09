'use client';

import type { components } from '@/lib/api/schema';
import { ProjectForm, type ProjectInput } from './project-form';

const mutableFields = ['runner_mode', 'trusted_project', 'local_remediation_limit', 'remote_remediation_limit',
  'planner_model', 'developer_model', 'reviewer_model', 'database', 'commands', 'allowed_environment_files',
  'secret_paths', 'allowed_merge_methods', 'publication_blocking_severities', 'merge_blocking_severities'] as const;

export function PolicyEditor({ policy, onSave }: {
  policy: components['schemas']['ProjectPolicyResponse'];
  onSave: (request: components['schemas']['ProjectPolicyUpdateRequest']) => Promise<unknown>;
}) {
  const initial = Object.fromEntries(mutableFields.filter(key => key in policy.document)
    .map(key => [key, policy.document[key]])) as Partial<ProjectInput>;
  return <section>
    <h2>Policy version {policy.version}</h2>
    <p>Digest: <code>{policy.policy_digest}</code></p>
    <p>Saving creates a new immutable policy version. Existing run approvals retain their bound policy.</p>
    <ProjectForm key={policy.version} policyOnly initial={initial} onSave={request => onSave({
      ...Object.fromEntries(mutableFields.map(key => [key, request[key]])), expected_policy_version: policy.version,
    })} />
  </section>;
}
