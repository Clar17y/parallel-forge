export type PrEvidence = {
  candidate_commit: string; diff_digest: string; validation_digest: string; review_digest: string;
  repository: string; base_ref: string; base_sha: string; title: string; body_digest: string;
  runner_mode: 'docker' | 'trusted_host'; runner_evidence_digest: string; remote_remediation_limit: number;
};

export function parsePrEvidence(value: Record<string, unknown>): PrEvidence {
  const digests = ['diff_digest', 'validation_digest', 'review_digest', 'body_digest', 'runner_evidence_digest'];
  const commits = ['candidate_commit', 'base_sha'];
  const fields = [...digests, ...commits, 'repository', 'base_ref', 'title', 'runner_mode', 'remote_remediation_limit'];
  if (Object.keys(value).some(key => !fields.includes(key))
    || digests.some(key => typeof value[key] !== 'string' || !/^[a-f0-9]{64}$/.test(value[key]))
    || commits.some(key => typeof value[key] !== 'string' || !/^[a-f0-9]{40}$/.test(value[key]))
    || ['repository', 'base_ref', 'title'].some(key => typeof value[key] !== 'string' || !value[key].trim())
    || !['docker', 'trusted_host'].includes(String(value.runner_mode))
    || !Number.isSafeInteger(value.remote_remediation_limit) || Number(value.remote_remediation_limit) < 0) {
    throw new Error('Publication evidence unavailable');
  }
  return value as PrEvidence;
}

export function PrPublicationEvidence({ evidence, body }: { evidence: PrEvidence; body: string }) {
  return <section aria-label="Publication authority">
    <p>Authorizes the managed push and PR creation, followed by up to {evidence.remote_remediation_limit} remote remediation cycles. This does not authorize merge.</p>
    <h3>{evidence.title}</h3><p>{evidence.repository} → {evidence.base_ref}</p>
    <dl><dt>Candidate commit</dt><dd><code>{evidence.candidate_commit}</code></dd>
      <dt>Base commit</dt><dd><code>{evidence.base_sha}</code></dd>
      <dt>Diff digest</dt><dd><code>{evidence.diff_digest}</code></dd>
      <dt>Validation digest</dt><dd><code>{evidence.validation_digest}</code></dd>
      <dt>Review digest</dt><dd><code>{evidence.review_digest}</code></dd>
      <dt>Runner</dt><dd>{evidence.runner_mode === 'trusted_host' ? 'Trusted host · unsandboxed' : 'Docker'}</dd>
      <dt>Runner evidence digest</dt><dd><code>{evidence.runner_evidence_digest}</code></dd>
      <dt>PR body digest</dt><dd><code>{evidence.body_digest}</code></dd>
    </dl><h3>Exact PR body</h3><pre>{body}</pre>
  </section>;
}
