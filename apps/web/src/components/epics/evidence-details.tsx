import type { ReactNode } from 'react';

export function EvidenceDetails({
  summary,
  children,
  defaultOpen = false,
}: {
  summary: ReactNode;
  children: ReactNode;
  defaultOpen?: boolean;
}) {
  return (
    <details className="evidence-details" open={defaultOpen}>
      <summary className="cursor-pointer text-sm text-[var(--muted)] hover:text-[var(--text)]">
        {summary}
      </summary>
      <div className="mt-2 pl-4 border-l-2 border-[var(--border)] text-xs font-mono text-[var(--muted)] break-all space-y-1">
        {children}
      </div>
    </details>
  );
}
