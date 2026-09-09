import type { components } from '@/lib/api/schema';

export type MergeEvidence = {
  repository: string; pull_request_number: number; head_sha: string; base_ref: string; base_sha: string;
  required_checks: Record<string, string>; unresolved_blocking_findings: number;
  validation_digest: string; review_digest: string; runner_mode: 'docker' | 'trusted_host';
  runner_evidence_digest: string; protection_digest: string; merge_method: string; policy_version: number;
};

export function parseMergeEvidence(value: Record<string, unknown>): MergeEvidence {
  const digests = ['validation_digest', 'review_digest', 'runner_evidence_digest', 'protection_digest'];
  const commits = ['head_sha', 'base_sha'];
  const fields = [...digests, ...commits, 'repository', 'pull_request_number', 'base_ref', 'required_checks', 'unresolved_blocking_findings', 'runner_mode', 'merge_method', 'policy_version'];
  const checks = value.required_checks;
  if (Object.keys(value).some(key => !fields.includes(key))
    || digests.some(key => typeof value[key] !== 'string' || !/^[a-f0-9]{64}$/.test(value[key]))
    || commits.some(key => typeof value[key] !== 'string' || !/^[a-f0-9]{40}$/.test(value[key]))
    || ['repository', 'base_ref'].some(key => typeof value[key] !== 'string' || !value[key].trim())
    || !['docker', 'trusted_host'].includes(String(value.runner_mode))
    || !['merge', 'squash', 'rebase'].includes(String(value.merge_method))
    || ['pull_request_number', 'policy_version'].some(key => !Number.isSafeInteger(value[key]) || Number(value[key]) < 1)
    || value.unresolved_blocking_findings !== 0 || !checks || typeof checks !== 'object' || Array.isArray(checks)
    || !Object.keys(checks).length || Object.entries(checks).some(([name, status]) => !name.trim() || status !== 'success')) {
    throw new Error('Merge evidence unavailable');
  }
  return value as MergeEvidence;
}

export function MergeApprovalEvidence({ evidence, protection }: { evidence: MergeEvidence; protection: components['schemas']['ProtectionSnapshotResponse'] }) {
  return <section aria-label="Merge authority"><h3>{evidence.repository} #{evidence.pull_request_number}</h3>
    <p>Authorizes merge of this exact head using {evidence.merge_method}. Changes to head, base, checks, reviews, policy or method invalidate approval. GitHub enforces the expected head.</p>
    <dl><dt>Remote head</dt><dd><code>{evidence.head_sha}</code></dd>
      <dt>Observed base</dt><dd>{evidence.base_ref} · <code>{evidence.base_sha}</code></dd>
      <dt>Blocking findings</dt><dd>{evidence.unresolved_blocking_findings}</dd>
      <dt>Validation digest</dt><dd><code>{evidence.validation_digest}</code></dd>
      <dt>Review digest</dt><dd><code>{evidence.review_digest}</code></dd>
      <dt>Runner</dt><dd>{evidence.runner_mode === 'trusted_host' ? 'Trusted host · unsandboxed' : 'Docker'}</dd>
      <dt>Runner evidence digest</dt><dd><code>{evidence.runner_evidence_digest}</code></dd>
      <dt>GitHub protection source</dt><dd>{protection.evidence_source}</dd>
      <dt>Protection digest</dt><dd><code>{evidence.protection_digest}</code></dd>
    </dl><h4>Required checks</h4><ul>{Object.entries(evidence.required_checks).map(([name, result]) => <li key={name}>{name}: {result}</li>)}</ul>
    <p>{protection.strict_required_checks ? 'Strict required checks enabled. ' : ''}{protection.merge_queue_enabled ? 'Merge queue enabled. ' : ''}Approval actor cannot bypass protections.</p>
  </section>;
}
