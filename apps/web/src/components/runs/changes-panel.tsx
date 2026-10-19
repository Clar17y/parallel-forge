'use client';
import { LoadingStatus } from '@/components/ui/loading-status';
import { useApi } from '@/hooks/use-api';
import type { components } from '@/lib/api/schema';
import { UnifiedDiff } from './unified-diff';
import { parsePrEvidence, type PrEvidence } from './pr-publication-evidence';
import { matchesPublicationDecision } from './publication-decision-evidence';

export function ChangesPanel({ projection }: { projection: components['schemas']['RunProjection'] }) {
  const digest = projection.next_gate === 'pr' && projection.candidate.acceptance_evidence_digest
    ? projection.candidate.pending_evidence_digest : null;
  return digest ? <PublicationChanges projection={projection} digest={digest} />
    : <ReviewerChanges projection={projection} />;
}

function PublicationChanges({ projection, digest }: { projection: components['schemas']['RunProjection']; digest: string }) {
  const checkpoint = useApi<components['schemas']['ArtifactTextResponse']>(`/artifacts/${digest}/text`);
  let evidence: PrEvidence | null = null;
  try {
    if (checkpoint.value?.digest === digest) {
      const value = parsePrEvidence(JSON.parse(checkpoint.value.text));
      if (value.schema_version === 2 && matchesPublicationDecision(value, projection.candidate)
        && value.candidate_commit === projection.candidate.commit
        && value.validation_digest === projection.candidate.validation_evidence_digest
        && value.repository === projection.project.github_repository
        && value.base_sha === projection.run.base_sha && value.base_ref === projection.run.base_ref
        && value.runner_mode === projection.security.runner_mode) evidence = value;
    }
  } catch { /* Invalid checkpoint evidence is shown as unavailable. */ }
  const artifact = useApi<components['schemas']['ArtifactTextResponse']>(evidence ? `/artifacts/${evidence.diff_digest}/text` : null);
  const changes = artifact.value?.digest === evidence?.diff_digest ? artifact.value : null;
  const failed = checkpoint.failed || (checkpoint.value && !evidence)
    || artifact.failed || (artifact.value && !changes);
  return <section aria-label="Run changes"><h2>Changes</h2>
    <p>Recorded changes in the candidate awaiting publication.</p>
    {(checkpoint.loading || artifact.loading) && <LoadingStatus>Loading changes…</LoadingStatus>}
    {failed && <p role="alert">Changes evidence unavailable. <button onClick={() => { checkpoint.refresh(); artifact.refresh(); }}>Retry</button></p>}
    {changes && evidence && <><p>Recorded head: <code>{evidence.candidate_commit}</code> · Diff digest: <code>{evidence.diff_digest}</code></p>
      <UnifiedDiff text={changes.text} artifactDigest={changes.digest} truncated={false} />
    </>}
  </section>;
}

function ReviewerChanges({ projection }: { projection: components['schemas']['RunProjection'] }) {
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
    {artifact.loading && <LoadingStatus>Loading changes…</LoadingStatus>}
    {(artifact.failed || (value && !changes)) && <p role="alert">Changes evidence unavailable. <button onClick={artifact.refresh}>Retry</button></p>}
    {changes && <><p>Recorded head: <code>{changes.head_sha}</code> · Diff digest: <code>{changes.diff_digest}</code></p>
      {projection.candidate.commit && projection.candidate.commit !== changes.head_sha && <p role="alert">This recorded diff belongs to an earlier candidate. Current candidate: <code>{projection.candidate.commit}</code></p>}
      <UnifiedDiff text={changes.text} artifactDigest={changes.digest} truncated={changes.truncated} />
    </>}
  </section>;
}
