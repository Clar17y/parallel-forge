import Link from 'next/link';
import { StatusBadge } from '@/components/ui/status-badge';
import type { EpicResponse } from '@/hooks/epics/types';

export function EpicList({ epics }: { epics: EpicResponse[] }) {
  if (epics.length === 0) {
    return (
      <div className="p-8 text-center bg-[var(--surface-muted)] rounded border border-[var(--border)] text-sm text-[var(--muted)]">
        No epics registered for this project yet. Click &quot;New Epic&quot; to begin requirements decomposition.
      </div>
    );
  }

  return (
    <div className="space-y-3">
      {epics.map(epic => (
        <div
          key={epic.epic_id}
          className="p-4 border border-[var(--border)] rounded bg-[var(--surface)] hover:border-[var(--control-border)] transition-colors grid grid-cols-1 md:grid-cols-12 gap-4 items-center"
        >
          <div className="md:col-span-6 space-y-1 min-w-0">
            <div className="flex items-center space-x-2">
              <Link
                href={`/epics/${epic.epic_id}`}
                className="font-semibold text-base text-[var(--focus)] hover:underline truncate"
                title={epic.title}
              >
                {epic.title}
              </Link>
              <span className="text-xs px-2 py-0.5 rounded bg-[var(--surface-muted)] text-[var(--muted)] border border-[var(--border)] shrink-0">
                v{epic.version}
              </span>
            </div>
            {epic.draft?.problem && (
              <details className="text-xs text-[var(--muted)] group">
                <summary className="cursor-pointer hover:text-[var(--foreground)] line-clamp-1">
                  {epic.draft.problem}
                </summary>
                <p className="mt-1 p-2 bg-[var(--surface-muted)] rounded text-xs text-[var(--foreground)] border border-[var(--border)]">
                  {epic.draft.problem}
                </p>
              </details>
            )}
            <div className="flex flex-wrap items-center gap-3 pt-0.5 text-xs text-[var(--muted)]">
              <span>Updated: {new Date(epic.updated_at).toLocaleDateString()}</span>
              <span>•</span>
              <span>ID: {epic.epic_id.slice(0, 8)}…</span>
            </div>
          </div>

          <div className="md:col-span-4 flex flex-wrap items-center gap-2">
            {epic.accepted_brief_revision_id ? (
              <StatusBadge label="Brief Accepted" tone="success" />
            ) : (
              <StatusBadge label="Draft Brief Only" tone="neutral" />
            )}
            {epic.accepted_graph_revision_id ? (
              <StatusBadge label="Graph Accepted" tone="success" />
            ) : (
              <StatusBadge label="No Graph" tone="neutral" />
            )}
          </div>

          <div className="md:col-span-2 flex justify-start md:justify-end">
            <Link
              href={`/epics/${epic.epic_id}`}
              className="button text-xs whitespace-nowrap"
              data-variant="secondary"
            >
              Open Workspace
            </Link>
          </div>
        </div>
      ))}
    </div>
  );
}
