import { parseDiff } from '@/lib/diff';

export function UnifiedDiff({ text, artifactDigest, truncated: sourceTruncated }: { text: string; artifactDigest: string; truncated: boolean }) {
  const diff = parseDiff(text);
  return <section aria-label="Unified diff">
    <p>Artifact digest: <code>{artifactDigest}</code> · <a href={`/api/artifacts/${artifactDigest}/download`}>Download diff evidence</a></p>
    {(sourceTruncated || diff.truncated) && <p role="alert">Truncated diff: this view does not show all changes.</p>}
    {diff.unsupported && <p role="alert">Some diff content could not be interpreted. Inspect the evidence before approving.</p>}
    {!diff.files.length && !diff.unsupported && !sourceTruncated && !diff.truncated && <p>No changes in this diff.</p>}
    {diff.files.map((file, index) => <section key={index}><h3>{file.header}</h3><p>{file.status}{file.binary ? ' · Binary file' : ''}</p>
      <div className="overflow-x-auto"><table><caption className="sr-only">Diff for {file.header}</caption>
        <thead><tr><th scope="col">Old line</th><th scope="col">New line</th><th scope="col">Change</th></tr></thead>
        <tbody>{file.lines.map((line, index) => <tr key={index} className={line.kind === 'added' ? 'bg-green-950/20' : line.kind === 'removed' ? 'bg-red-950/20' : undefined}>
          <td>{line.oldLine ?? ''}</td><td>{line.newLine ?? ''}</td><td><pre>{line.text}</pre></td>
        </tr>)}</tbody>
      </table></div>
    </section>)}
  </section>;
}
