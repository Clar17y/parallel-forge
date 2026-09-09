import { expect, test } from 'vitest';
import { parseDiff } from './diff';

test('unified diff preserves file status, binary markers and old/new line numbers', () => {
  const value = parseDiff('diff --git a/a.txt b/a.txt\n--- a/a.txt\n+++ b/a.txt\n@@ -2,2 +2,2 @@\n same\n-old\n+<script>new</script>\ndiff --git a/b.bin b/b.bin\nnew file mode 100644\nBinary files /dev/null and b/b.bin differ\n');
  expect(value.files).toHaveLength(2);
  expect(value.files[0].status).toBe('modified');
  expect(value.files[0].lines.filter(line => line.kind === 'removed')).toEqual([{ text: '-old', kind: 'removed', oldLine: 3, newLine: null }]);
  expect(value.files[0].lines.filter(line => line.kind === 'added')).toEqual([{ text: '+<script>new</script>', kind: 'added', oldLine: null, newLine: 3 }]);
  expect(value.files[1]).toMatchObject({ status: 'added', binary: true });
});

test('rename and deletion metadata are displayed without treating headers as hunks', () => {
  const value = parseDiff('diff --git a/old b/new\nsimilarity index 100%\nrename from old\nrename to new\ndiff --git a/dead b/dead\ndeleted file mode 100644\n--- a/dead\n+++ /dev/null\n@@ -1 +0,0 @@\n-gone');
  expect(value.files.map(file => file.status)).toEqual(['renamed', 'deleted']);
  expect(value.files[0].lines.every(line => line.oldLine === null)).toBe(true);
  expect(value.files[1].lines.at(-1)).toMatchObject({ oldLine: 1, newLine: null });
});

test('large diff is visibly bounded and unsupported text is not declared an empty diff', () => {
  expect(parseDiff('diff --git a/a b/a\n' + ' context\n'.repeat(6000)).truncated).toBe(true);
  expect(parseDiff('unrecognized response').unsupported).toBe(true);
  expect(parseDiff('').unsupported).toBe(false);
});
