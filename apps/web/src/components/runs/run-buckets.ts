import type { components } from '@/lib/api/schema';

type State = components['schemas']['RunState'];
type Summary = components['schemas']['DashboardSummary'];

export interface RunBucket {
  id: string;
  label: string;
  states: State[];
}

/**
 * Plain-language groupings of the 18-state run machine. The non-"all"
 * buckets partition every state exactly once, so selecting a bucket is
 * equivalent to picking its states from the old raw-state dropdown.
 */
export const RUN_BUCKETS: RunBucket[] = [
  { id: 'all', label: 'All runs', states: [] },
  {
    id: 'working', label: 'Working',
    states: ['CREATED', 'PLANNING', 'PREPARING_WORKTREE', 'IMPLEMENTING', 'VALIDATING', 'REVIEWING', 'REMEDIATING', 'PUBLISHING_PR', 'MONITORING_PR', 'MERGING'],
  },
  {
    id: 'waiting', label: 'Waiting on you',
    states: ['AWAITING_PLAN_APPROVAL', 'AWAITING_PR_APPROVAL', 'AWAITING_MERGE_APPROVAL'],
  },
  { id: 'blocked', label: 'Blocked', states: ['AWAITING_HUMAN_INTERVENTION', 'PAUSED'] },
  { id: 'done', label: 'Done', states: ['COMPLETED', 'CANCELLED'] },
  { id: 'failed', label: 'Failed', states: ['FAILED'] },
];

export function bucketById(id: string | null): RunBucket {
  return RUN_BUCKETS.find(bucket => bucket.id === id) ?? RUN_BUCKETS[0];
}

/** Per-bucket totals from the dashboard summary; unknown summary, no counts. */
export function bucketCounts(summary: Summary | undefined): Record<string, number> {
  if (!summary) return {};
  const counts: Record<string, number> = { all: summary.total_runs };
  for (const bucket of RUN_BUCKETS) {
    if (bucket.id === 'all') continue;
    counts[bucket.id] = bucket.states.reduce((total, state) => total + (summary.runs[state] ?? 0), 0);
  }
  return counts;
}
