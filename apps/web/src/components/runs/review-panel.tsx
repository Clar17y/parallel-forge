'use client';
import { useApi } from '@/hooks/use-api';
import type { components } from '@/lib/api/schema';
import { ObservationEvidence } from './observation-evidence';

const severities = ['blocker', 'major', 'minor', 'suggestion'] as const;
export function ReviewPanel({ projection }: { projection: components['schemas']['RunProjection'] }) {
  const remote = projection.remote_observation;
  const digest = projection.review.evidence_digest;
  const artifact = useApi<components['schemas']['ReviewArtifactResponse']>(digest ? `/artifacts/${digest}/review` : null);
  const value = artifact.value;
  const decision = value && value.digest === digest && value.run_id === projection.run.id
    && value.producer_execution_id === projection.review.execution_id
    && value.head_sha === projection.review.head_sha && value.policy_version === projection.run.policy_version ? value : null;
  return <section aria-label="Review evidence"><h2>Review</h2>
    <section><h3>Independent local review</h3><p>Head: <code>{projection.review.head_sha ?? 'Unavailable'}</code></p>
      {projection.review.evidence_digest ? <p><a href={`/api/artifacts/${projection.review.evidence_digest}/download`}>Local review evidence</a> · <code>{projection.review.evidence_digest}</code></p> : <p>No local review evidence recorded.</p>}
      {artifact.loading && <p role="status">Loading review decision…</p>}
      {(artifact.failed || (value && !decision)) && <p role="alert">Review decision evidence unavailable. <button onClick={artifact.refresh}>Retry</button></p>}
      {decision && <><p>Decision: {decision.decision.replaceAll('_', ' ')}</p><p>{decision.summary}</p>
        <h4>Tested claims</h4>{decision.tested_claims.length ? <ul>{decision.tested_claims.map((claim, index) => <li key={index}>{claim}</li>)}</ul> : <p>None recorded</p>}
        <h4>Missing evidence</h4>{decision.missing_evidence.length ? <ul>{decision.missing_evidence.map((claim, index) => <li key={index}>{claim}</li>)}</ul> : <p>None recorded</p>}
      </>}
      {severities.map(severity => {
        const findings = projection.review.findings.filter(finding => finding.severity.toLowerCase() === severity);
        return findings.length > 0 && <section key={severity}><h4>{severity[0].toUpperCase() + severity.slice(1)}</h4>
          {findings.map(finding => <article key={finding.id}><h5>{finding.id}: {finding.summary}</h5><p>{finding.status}</p>
            {finding.path && <p>{finding.path}{finding.start_line !== null ? `:${finding.start_line}` : ''}</p>}
            <p>{finding.evidence}</p>{finding.proposed_resolution && <p>Proposed resolution: {finding.proposed_resolution}</p>}
          </article>)}</section>;
      })}
    </section>
    <section><h3>GitHub reviews</h3>
      {!remote ? <p>No remote observation recorded.</p> : <><ObservationEvidence observation={remote} />
        {!remote.reviews.length && <p>No reviews returned in this observation.</p>}
        {remote.reviews.map((review, index) => <article key={`${review.reviewer}:${index}`}><h4>{review.reviewer} · {review.state}</h4>
          {review.requested_changes && <p>Changes requested</p>}<p>{review.unresolved_threads} unresolved threads</p><p>{review.comment_count} comments</p>
          {review.body && <p>{review.body}</p>}{review.feedback.map((feedback, index) => <p key={index}>{feedback}</p>)}
        </article>)}</>}
    </section>
  </section>;
}
