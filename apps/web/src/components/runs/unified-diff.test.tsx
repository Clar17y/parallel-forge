import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, expect, test } from 'vitest';
import { UnifiedDiff } from './unified-diff';

afterEach(cleanup);
test('diff renders untrusted text literally with line numbers and truncation warning', () => {
  render(<UnifiedDiff text={'diff --git a/a b/a\n@@ -1 +1 @@\n-old\n+<script>untrusted</script>'} artifactDigest={'a'.repeat(64)} truncated />);
  expect(screen.getByText('+<script>untrusted</script>')).toBeInTheDocument();
  expect(document.querySelector('script')).toBeNull();
  expect(screen.getAllByRole('cell', { name: '1' })).toHaveLength(2);
  expect(screen.getByRole('alert')).toHaveTextContent('Truncated diff');
  expect(screen.getByRole('link', { name: 'Download diff evidence' })).toHaveAttribute('href', `/api/artifacts/${'a'.repeat(64)}/download`);
});
