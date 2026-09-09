'use client';
import { useApi } from '@/hooks/use-api';
import type { components } from '@/lib/api/schema';
import { UnifiedDiff } from './unified-diff';

export function ChangesPanel({ projection }: { projection: components['schemas']['RunProjection'] }) {
  const reviewer = projection.agents.reviewer;
  const digest = reviewer?.input_artifact_digest;
  const artifact = useApi<components['schemas']['ReviewerDiffResponse']>(digest ? `/artifacts/${digest}/reviewer-diff` : null);
  const value = artifact.value;
  const changes = value && value.digest === digest && value.run_id === projection.run.id
    && value.producer_execution_id === reviewer?.execution_id
    && value.validation_evidence_set_id === reviewer?.validation_evidence_set_id
    && value.policy_version === projection.run.policy_version ? value : null;
  return <section aria-label="Run changes"><h2>Changes</h2>
    <p>Persisted diff supplied to the latest reviewer. Uncommitted work and later edits are not included.</p>
    {!digest && <p>No reviewer diff evidence recorded yet.</p>}
    {artifact.loading && <p role="status">Loading changes…</p>}
    {(artifact.failed || (value && !changes)) && <p role="alert">Changes evidence unavailable. <button onClick={artifact.refresh}>Retry</button></p>}
    {changes && <><p>Recorded head: <code>{changes.head_sha}</code> · Diff digest: <code>{changes.diff_digest}</code></p>
      {projection.candidate.commit && projection.candidate.commit !== changes.head_sha && <p role="alert">This recorded diff belongs to an earlier candidate. Current candidate: <code>{projection.candidate.commit}</code></p>}
      <UnifiedDiff text={changes.text} artifactDigest={changes.digest} truncated={changes.truncated} />
    </>}
  </section>;
}
