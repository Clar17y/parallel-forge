import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, expect, test } from 'vitest';
import { PrPublicationEvidence, parsePrEvidence } from './pr-publication-evidence';
import { MergeApprovalEvidence, parseMergeEvidence } from './merge-approval-evidence';
import type { components } from '@/lib/api/schema';

afterEach(cleanup);

const common = {
  repository: 'owner/repo', base_ref: 'main', base_sha: 'a'.repeat(40),
  validation_digest: 'b'.repeat(64), runner_mode: 'docker', runner_evidence_digest: 'c'.repeat(64),
};

test('publication approval leads with what it authorizes and demotes binding digests', () => {
  const evidence = parsePrEvidence({
    ...common, review_digest: '1'.repeat(64), candidate_commit: 'd'.repeat(40),
    diff_digest: 'e'.repeat(64), title: 'Accepted change', body_digest: 'f'.repeat(64),
    remote_remediation_limit: 3,
  });
  render(<PrPublicationEvidence evidence={evidence} body="PR body text" />);
  expect(screen.getByRole('heading', { name: 'Accepted change' })).toBeInTheDocument();
  expect(screen.getByText('PR body text')).toBeInTheDocument();
  // Commits stay inline for comparison; 64-hex digests sit behind Technical details.
  expect(screen.getByText('d'.repeat(40)).closest('details')).toBeNull();
  expect(screen.getByText('e'.repeat(64)).closest('details')).not.toBeNull();
  expect(screen.getByText('b'.repeat(64)).closest('details')).not.toBeNull();
  expect(screen.getByText('f'.repeat(64)).closest('details')).not.toBeNull();
});

test('merge approval leads with the exact head and checks, digests demoted', () => {
  const evidence = parseMergeEvidence({
    ...common, review_digest: '1'.repeat(64), head_sha: 'd'.repeat(40), pull_request_number: 12,
    required_checks: { ci: 'success' }, unresolved_blocking_findings: 0,
    protection_digest: 'e'.repeat(64), merge_method: 'squash', policy_version: 2,
  });
  const protection: components['schemas']['ProtectionSnapshotResponse'] = {
    actor_can_bypass: false, evidence_source: 'github', merge_queue_enabled: false,
    required_check_names: ['ci'], strict_required_checks: true, verified: true,
  };
  render(<MergeApprovalEvidence evidence={evidence} protection={protection} />);
  expect(screen.getByText(/ci: success/)).toBeInTheDocument();
  expect(screen.getByText('d'.repeat(40)).closest('details')).toBeNull();
  expect(screen.getByText('e'.repeat(64)).closest('details')).not.toBeNull();
  expect(screen.getByText('b'.repeat(64)).closest('details')).not.toBeNull();
});
