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
          className="p-4 border border-[var(--border)] rounded bg-[var(--surface)] flex flex-col md:flex-row md:items-center justify-between gap-3 hover:border-[var(--control-border)] transition-colors"
        >
          <div className="space-y-1">
            <div className="flex items-center space-x-2">
              <Link
                href={`/epics/${epic.epic_id}`}
                className="font-semibold text-base text-[var(--focus)] hover:underline"
              >
                {epic.title}
              </Link>
              <span className="text-xs px-2 py-0.5 rounded bg-[var(--surface-muted)] text-[var(--muted)] border border-[var(--border)]">
                v{epic.version}
              </span>
            </div>
            {epic.draft?.problem && (
              <p className="text-xs text-[var(--muted)] line-clamp-1">{epic.draft.problem}</p>
            )}
            <div className="flex flex-wrap items-center gap-3 pt-1 text-xs text-[var(--muted)]">
              <span>Updated: {new Date(epic.updated_at).toLocaleDateString()}</span>
              <span>•</span>
              <span>ID: {epic.epic_id.slice(0, 8)}…</span>
            </div>
          </div>

          <div className="flex flex-wrap items-center gap-2">
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
            <Link
              href={`/epics/${epic.epic_id}`}
              className="button text-xs ml-2"
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
