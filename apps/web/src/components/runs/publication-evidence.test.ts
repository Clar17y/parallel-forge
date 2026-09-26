import { expect, test } from 'vitest';
import { parsePrEvidence } from './pr-publication-evidence';
import { parseMergeEvidence } from './merge-approval-evidence';

const common = { repository: 'owner/repo', base_ref: 'main', base_sha: 'a'.repeat(40),
  validation_digest: 'b'.repeat(64), runner_mode: 'docker', runner_evidence_digest: 'c'.repeat(64) };
const cases = [
  { gate: 'PR', parse: parsePrEvidence, fields: { ...common, candidate_commit: 'd'.repeat(40),
    diff_digest: 'e'.repeat(64), title: 'Accepted change', body_digest: 'f'.repeat(64), remote_remediation_limit: 3 } },
  { gate: 'merge', parse: parseMergeEvidence, fields: { ...common, head_sha: 'd'.repeat(40), pull_request_number: 12,
    required_checks: { ci: 'success' }, unresolved_blocking_findings: 0, protection_digest: 'e'.repeat(64),
    merge_method: 'squash', policy_version: 2 } },
];

test.each(cases)('$gate evidence preserves the explicit legacy or subscription decision', ({ parse, fields }) => {
  const legacy = { ...fields, review_digest: '1'.repeat(64) };
  const accepted = { ...fields, schema_version: 2, acceptance_digest: '2'.repeat(64), candidate_tree_digest: '3'.repeat(64) };
  expect(parse(legacy)).toBe(legacy); expect(parse(accepted)).toBe(accepted);
  for (const schema_version of [true, 1, 3, '2', null, undefined]) {
    expect(() => parse({ ...accepted, schema_version })).toThrow();
  }
  expect(() => parse({ ...fields, acceptance_digest: '2'.repeat(64), candidate_tree_digest: '3'.repeat(64) })).toThrow();
  expect(() => parse({ ...accepted, review_digest: legacy.review_digest })).toThrow();
  expect(() => parse({ ...legacy, acceptance_digest: '2'.repeat(64) })).toThrow();
  for (const field of ['acceptance_digest', 'candidate_tree_digest']) {
    expect(() => parse({ ...accepted, [field]: 'invalid' })).toThrow();
    expect(() => parse({ ...accepted, [field]: undefined })).toThrow();
  }
  expect(() => parse({ ...accepted, extra_authority: true })).toThrow();
});
