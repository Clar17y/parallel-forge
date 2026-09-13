import type { components } from '@/lib/api/schema';

export type PublicationDecision =
  | { schema_version?: never; review_digest: string }
  | { schema_version: 2; acceptance_digest: string; candidate_tree_digest: string };

export function publicationDecisionFields(value: Record<string, unknown>): string[] {
  const versioned = Object.hasOwn(value, 'schema_version');
  const digests = versioned ? ['acceptance_digest', 'candidate_tree_digest'] : ['review_digest'];
  if ((versioned && value.schema_version !== 2)
    || digests.some(key => typeof value[key] !== 'string' || !/^[a-f0-9]{64}$/.test(value[key]))) {
    throw new Error('Decision evidence unavailable');
  }
  return versioned ? ['schema_version', ...digests] : digests;
}

export function matchesPublicationDecision(evidence: PublicationDecision, candidate: components['schemas']['CandidateSection']): boolean {
  return evidence.schema_version === 2
    ? evidence.acceptance_digest === candidate.acceptance_evidence_digest
      && evidence.candidate_tree_digest === candidate.candidate_tree_digest
    : evidence.review_digest === candidate.review_evidence_digest;
}

export function PublicationDecisionEvidence({ evidence }: { evidence: PublicationDecision }) {
  return evidence.schema_version === 2
    ? <><dt>Acceptance digest</dt><dd><code>{evidence.acceptance_digest}</code></dd>
      <dt>Candidate contents</dt><dd><code>{evidence.candidate_tree_digest}</code></dd></>
    : <><dt>Review digest</dt><dd><code>{evidence.review_digest}</code></dd></>;
}
