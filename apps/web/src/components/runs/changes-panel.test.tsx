import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, expect, test, vi } from 'vitest';
import { ChangesPanel } from './changes-panel';
import { api } from '@/lib/api/client';
import { projection } from '@/test/projection';

vi.mock('@/lib/api/client', () => ({ api: vi.fn() }));
afterEach(() => { cleanup(); vi.mocked(api).mockReset(); });
function fixture() {
  const value = projection();
  value.candidate.commit = 'c'.repeat(40);
  value.agents.reviewer = { role: 'reviewer', provider: 'fixture', model: 'fixture',
    execution_id: 'execution-1', status: 'SUCCEEDED', instruction_version: 'v1', input_artifact_digest: 'a'.repeat(64),
    output_artifact_digest: null, validation_evidence_set_id: 'validation-1', independent: true, allowed_tools: [] };
  const artifact = { digest: 'a'.repeat(64), run_id: value.run.id, producer_execution_id: 'execution-1',
    validation_evidence_set_id: 'validation-1', policy_version: value.run.policy_version, head_sha: 'b'.repeat(40),
    diff_digest: 'd'.repeat(64), text: 'diff --git a/file b/file\n@@ -1 +1 @@\n-old\n+new', truncated: false, original_byte_count: 60 };
  return { value, artifact };
}
test('changes render recorded diff and disclose when its head differs from the current candidate', async () => {
  const { value, artifact } = fixture();
  vi.mocked(api).mockResolvedValue(artifact);
  render(<ChangesPanel projection={value} />);
  expect(await screen.findByText('+new')).toBeInTheDocument();
  expect(screen.getByRole('alert')).toHaveTextContent('earlier candidate');
  expect(screen.getByText('b'.repeat(40))).toBeInTheDocument();
});
test('legacy PR gates keep the reviewer diff without subscription acceptance evidence', async () => {
  const { value, artifact } = fixture();
  value.run.state = 'AWAITING_PR_APPROVAL'; value.next_gate = 'pr';
  value.candidate.commit = artifact.head_sha;
  value.candidate.pending_evidence_digest = 'e'.repeat(64);
  value.candidate.acceptance_evidence_digest = null;
  value.candidate.review_evidence_digest = 'f'.repeat(64);
  vi.mocked(api).mockResolvedValue(artifact);
  render(<ChangesPanel projection={value} />);
  expect(await screen.findByText('+new')).toBeInTheDocument();
  expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  expect(vi.mocked(api).mock.calls.map(([path]) => path)).toEqual([`/artifacts/${artifact.digest}/reviewer-diff`]);
});
test.each(['digest', 'run_id', 'producer_execution_id', 'validation_evidence_set_id', 'policy_version'])('changes hide mismatched %s evidence', async field => {
  const { value, artifact } = fixture();
  vi.mocked(api).mockResolvedValue({ ...artifact, [field]: 'wrong' });
  render(<ChangesPanel projection={value} />);
  expect(await screen.findByRole('alert')).toHaveTextContent('Changes evidence unavailable');
  expect(screen.queryByText('+new')).not.toBeInTheDocument();
});

function publicationFixture() {
  const value = projection();
  value.run.state = 'AWAITING_PR_APPROVAL'; value.run.base_ref = 'main';
  value.next_gate = 'pr';
  value.candidate = { commit: 'c'.repeat(40), pending_evidence_digest: '1'.repeat(64),
    validation_evidence_digest: '2'.repeat(64), review_evidence_digest: null,
    acceptance_evidence_digest: '3'.repeat(64), candidate_tree_digest: '4'.repeat(64) };
  const evidence = { schema_version: 2, candidate_commit: value.candidate.commit,
    candidate_tree_digest: value.candidate.candidate_tree_digest,
    acceptance_digest: value.candidate.acceptance_evidence_digest,
    validation_digest: value.candidate.validation_evidence_digest, diff_digest: '5'.repeat(64),
    repository: value.project.github_repository, base_ref: 'main', base_sha: value.run.base_sha,
    title: 'Change', body_digest: '6'.repeat(64), runner_mode: 'docker',
    runner_evidence_digest: '7'.repeat(64), remote_remediation_limit: 3 };
  return { value, evidence };
}
test('shows the exact subscription publication diff without requiring a reviewer', async () => {
  const { value, evidence } = publicationFixture();
  vi.mocked(api).mockImplementation(async path => path === `/artifacts/${value.candidate.pending_evidence_digest}/text`
    ? { digest: value.candidate.pending_evidence_digest, text: JSON.stringify(evidence) }
    : { digest: evidence.diff_digest, text: 'diff --git a/file b/file\n@@ -1 +1 @@\n-old\n+subscription change' });
  render(<ChangesPanel projection={value} />);
  expect(await screen.findByText('+subscription change')).toBeInTheDocument();
  expect(screen.queryByText(/No reviewer diff/)).not.toBeInTheDocument();
  expect(vi.mocked(api).mock.calls.every(([path]) => !path.includes('reviewer-diff'))).toBe(true);
});
test.each(['candidate_commit', 'candidate_tree_digest', 'acceptance_digest', 'validation_digest', 'base_sha', 'repository'])('hides a publication diff with mismatched %s', async field => {
  const { value, evidence } = publicationFixture();
  vi.mocked(api).mockResolvedValue({ digest: value.candidate.pending_evidence_digest,
    text: JSON.stringify({ ...evidence, [field]: 'wrong' }) });
  render(<ChangesPanel projection={value} />);
  expect(await screen.findByRole('alert')).toHaveTextContent('Changes evidence unavailable');
  expect(api).toHaveBeenCalledTimes(1);
});
test('hides text returned for a different diff digest', async () => {
  const { value, evidence } = publicationFixture();
  vi.mocked(api).mockImplementation(async path => path === `/artifacts/${value.candidate.pending_evidence_digest}/text`
    ? { digest: value.candidate.pending_evidence_digest, text: JSON.stringify(evidence) }
    : { digest: '0'.repeat(64), text: '+wrong candidate' });
  render(<ChangesPanel projection={value} />);
  expect(await screen.findByRole('alert')).toHaveTextContent('Changes evidence unavailable');
  expect(screen.queryByText('+wrong candidate')).not.toBeInTheDocument();
});
