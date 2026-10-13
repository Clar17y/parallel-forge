import { expect, test } from 'vitest';
import { RUN_BUCKETS, bucketById, bucketCounts } from './run-buckets';

const ALL_STATES = [
  'CREATED', 'PLANNING', 'AWAITING_PLAN_APPROVAL', 'PREPARING_WORKTREE', 'IMPLEMENTING',
  'VALIDATING', 'REVIEWING', 'REMEDIATING', 'AWAITING_PR_APPROVAL', 'PUBLISHING_PR',
  'MONITORING_PR', 'AWAITING_HUMAN_INTERVENTION', 'AWAITING_MERGE_APPROVAL', 'MERGING',
  'PAUSED', 'COMPLETED', 'FAILED', 'CANCELLED',
];

test('buckets partition every run state exactly once, in plain language', () => {
  const assigned = RUN_BUCKETS.filter(bucket => bucket.id !== 'all').flatMap(bucket => bucket.states);
  expect([...assigned].sort()).toEqual([...ALL_STATES].sort());
  expect(RUN_BUCKETS.map(bucket => bucket.label)).toEqual([
    'All runs', 'Working', 'Waiting on you', 'Blocked', 'Done', 'Failed',
  ]);
});

test('unknown bucket ids fall back to all runs', () => {
  expect(bucketById('nonsense').id).toBe('all');
  expect(bucketById(null).id).toBe('all');
  expect(bucketById('waiting').states).toContain('AWAITING_PLAN_APPROVAL');
  expect(bucketById('all').states).toEqual([]);
});

test('bucket counts sum summary states and treat missing states as zero', () => {
  const counts = bucketCounts({
    total_runs: 9,
    runs: { IMPLEMENTING: 2, AWAITING_PLAN_APPROVAL: 1, AWAITING_MERGE_APPROVAL: 2, FAILED: 1, COMPLETED: 3 },
  });
  expect(counts).toMatchObject({ all: 9, working: 2, waiting: 3, blocked: 0, done: 3, failed: 1 });
  expect(bucketCounts(undefined)).toEqual({});
});
