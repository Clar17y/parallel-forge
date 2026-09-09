export type DiffLine = { text: string; kind: 'context' | 'added' | 'removed' | 'metadata'; oldLine: number | null; newLine: number | null };
export type DiffFile = { header: string; status: 'modified' | 'added' | 'deleted' | 'renamed' | 'copied'; binary: boolean; lines: DiffLine[] };

/** Display-only parser. Git headers remain literal; no paths are opened or interpreted. */
export function parseDiff(text: string) {
  const files: DiffFile[] = [];
  const bounded = text.slice(0, 1_048_576);
  const lines = bounded.split('\n', 5001);
  let truncated = bounded.length < text.length || lines.length > 5000;
  let unsupported = false;
  let current: DiffFile | undefined;
  let oldLine = 0, newLine = 0, oldRemaining = 0, newRemaining = 0;
  for (const original of lines.slice(0, 5000)) {
    const line = original.slice(0, 10000);
    truncated ||= line.length < original.length;
    if (line.startsWith('diff --git ')) {
      if (oldRemaining > 0 || newRemaining > 0) unsupported = true;
      if (files.length >= 100) { truncated = true; break; }
      current = { header: line.slice(11), status: 'modified', binary: false, lines: [] };
      files.push(current);
      oldRemaining = newRemaining = 0;
      continue;
    }
    if (!current) { if (line.trim()) unsupported = true; continue; }
    const item: DiffLine = { text: line, kind: 'metadata', oldLine: null, newLine: null };
    const hunk = /^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@/.exec(line);
    if (hunk) {
      if (oldRemaining > 0 || newRemaining > 0) unsupported = true;
      [oldLine, oldRemaining, newLine, newRemaining] = [Number(hunk[1]), Number(hunk[2] ?? 1), Number(hunk[3]), Number(hunk[4] ?? 1)];
      if (![oldLine, oldRemaining, newLine, newRemaining, oldLine + oldRemaining, newLine + newRemaining].every(Number.isSafeInteger)) {
        unsupported = true; oldRemaining = newRemaining = 0;
      }
    } else if (oldRemaining > 0 || newRemaining > 0) {
      if (line.startsWith(' ') && oldRemaining > 0 && newRemaining > 0) {
        item.kind = 'context'; item.oldLine = oldLine++; item.newLine = newLine++; oldRemaining--; newRemaining--;
      } else if (line.startsWith('-') && oldRemaining > 0) {
        item.kind = 'removed'; item.oldLine = oldLine++; oldRemaining--;
      } else if (line.startsWith('+') && newRemaining > 0) {
        item.kind = 'added'; item.newLine = newLine++; newRemaining--;
      } else if (!line.startsWith('\\ No newline')) {
        unsupported = true; oldRemaining = newRemaining = 0;
      }
    } else if (line.startsWith('new file mode ')) current.status = 'added';
    else if (line.startsWith('deleted file mode ')) current.status = 'deleted';
    else if (line.startsWith('rename from ')) current.status = 'renamed';
    else if (line.startsWith('copy from ')) current.status = 'copied';
    if (line.startsWith('Binary files ') || line === 'GIT binary patch') current.binary = true;
    current.lines.push(item);
  }
  if (oldRemaining > 0 || newRemaining > 0) unsupported = true;
  return { files, truncated, unsupported };
}
