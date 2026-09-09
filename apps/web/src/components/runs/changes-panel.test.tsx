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
test.each(['digest', 'run_id', 'producer_execution_id', 'validation_evidence_set_id', 'policy_version'])('changes hide mismatched %s evidence', async field => {
  const { value, artifact } = fixture();
  vi.mocked(api).mockResolvedValue({ ...artifact, [field]: 'wrong' });
  render(<ChangesPanel projection={value} />);
  expect(await screen.findByRole('alert')).toHaveTextContent('Changes evidence unavailable');
  expect(screen.queryByText('+new')).not.toBeInTheDocument();
});
