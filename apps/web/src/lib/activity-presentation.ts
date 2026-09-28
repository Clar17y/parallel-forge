import { readableLabel } from '@/components/runs/run-presentation';
import type { Tone } from '@/components/ui/tone';
import { formatDate } from '@/lib/format';

export type ActivityStatus =
  | 'succeeded'
  | 'failed'
  | 'denied'
  | 'cancelled'
  | 'pending'
  | 'running'
  | 'unknown';

export type ActivityTone = Tone;

export type ActivityDescription = {
  title: string;
  outcome?: string;
  subject?: string;
  status: ActivityStatus;
  actionKind: string;
  tone: ActivityTone;
  durationMs?: number;
};

export type BaseEvent = {
  event_type: string;
  payload?: Record<string, unknown> | null;
  occurred_at?: string;
  created_at?: string;
  sequence?: number;
  run_version?: number;
  id?: string;
};

function validCount(value: unknown): number | undefined {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0 ? value : undefined;
}

export function safeExactTime(value: string | undefined): string {
  if (!value || !Number.isFinite(Date.parse(value))) return 'Unknown time';
  return formatDate(value);
}

export function formatRelativeTime(dateInput: string | Date | number | undefined, now = Date.now()): string {
  if (!dateInput) return 'Unknown time';
  const ms = typeof dateInput === 'number'
    ? dateInput
    : dateInput instanceof Date
      ? dateInput.getTime()
      : Date.parse(dateInput);

  if (!Number.isFinite(ms)) return 'Unknown time';

  const diffMs = now - ms;
  let diffSec = Math.round(diffMs / 1000);
  if (diffSec < -5) {
    const future = -diffSec;
    if (future < 60) return `in ${future}s`;
    if (future < 3600) return `in ${Math.floor(future / 60)}m`;
    if (future < 86400) return `in ${Math.floor(future / 3600)}h`;
    return `in ${Math.floor(future / 86400)}d`;
  }
  if (diffSec < 0 && diffSec > -5) diffSec = 0;
  if (diffSec < 5) return 'just now';
  if (diffSec < 60) return `${diffSec}s ago`;
  const diffMin = Math.floor(diffSec / 60);
  if (diffMin < 60) return `${diffMin}m ago`;
  const diffHours = Math.floor(diffMin / 60);
  if (diffHours < 24) return `${diffHours}h ago`;
  const diffDays = Math.floor(diffHours / 24);
  return `${diffDays}d ago`;
}

function formatDuration(ms: number | undefined): string {
  if (typeof ms !== 'number' || !Number.isFinite(ms) || ms < 0) return '';
  if (ms < 1000) return `${ms}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

const READ_ONLY_TOOLS = new Set([
  'repository.read_file',
  'repository.list_files',
  'repository.search',
  'repository.read_instructions',
  'git.status',
  'git.diff',
  'validation-results.read',
  'review-artifacts.read',
]);

const FAMILIAR_OPERATIONS: Record<string, string> = {
  'worktree.create': 'Create workspace',
  'worktree.teardown': 'Teardown workspace',
  'database.provision': 'Provision database',
  'database.teardown': 'Teardown database',
  'git.commit.prepare': 'Prepare commit',
  'git.commit.publish': 'Publish commit',
  'git.commit': 'Git commit',
  'git.branch_delete': 'Delete branch',
  'command.run_named_check': 'Run check',
  'named_check': 'Run check',
  'validate': 'Validation',
};

const LIFECYCLE_LABELS: Record<string, string> = {
  'run.created': 'Run created',
  'run.planning_started': 'Planning started',
  'run.plan_revision_requested': 'Plan revision requested',
  'run.candidate_revision_requested': 'Candidate revision requested',
  'run.validation_started': 'Validation started',
  'run.validation_decided': 'Validation completed',
  'run.review_decided': 'Review completed',
  'run.pr_observed': 'GitHub PR observed',
  'run.pr_updated': 'Pull request updated',
  'run.merge_ready': 'Merge ready',
  'run.merge_queue_enqueued': 'Enqueued to merge queue',
  'run.merge_queue_observed': 'Merge queue observed',
  'run.paused': 'Run paused',
  'run.resumed': 'Run resumed',
  'run.local_remediation_exhausted': 'Local remediation limit reached',
  'resource.worktree_created': 'Workspace created',
  'resource.worktree_removed': 'Workspace removed',
  'resource.database_removed': 'Database removed',
  'resource.branch_removed': 'Branch removed',
};

function payloadOf(event: BaseEvent): Record<string, unknown> {
  return event.payload && typeof event.payload === 'object' ? event.payload : {};
}

function toolLifecycle(p: Record<string, unknown>, eventType: string) {
  const rawStatus = String(p.status ?? (eventType.endsWith('.denied') ? 'denied' : 'unknown')).toLowerCase();
  const denied = rawStatus === 'denied' || p.authorized === false;
  const cancelled = rawStatus === 'cancelled' || p.caller_cancelled === true;
  const failed = rawStatus === 'failed' || p.timed_out === true || p.error_code !== undefined;
  const succeeded = rawStatus === 'succeeded' && !denied && !cancelled && !failed;
  let status: ActivityStatus = 'unknown';
  let tone: ActivityTone = 'neutral';
  if (denied) { status = 'denied'; tone = 'warning'; }
  else if (cancelled) { status = 'cancelled'; }
  else if (failed) { status = 'failed'; tone = 'danger'; }
  else if (succeeded) { status = 'succeeded'; }
  else if (rawStatus === 'pending' || rawStatus === 'running') { status = rawStatus; tone = 'info'; }
  return { rawStatus, denied, cancelled, failed, succeeded, status, tone };
}

const ACTION_VERBS: Record<string, string> = {
  read: 'Read', write: 'Write', delete: 'Delete', rename: 'Rename', list: 'List files',
  search: 'Search repository', read_instructions: 'Read project instructions', status: 'Git status',
  diff: 'Git diff', commit: 'Commit', evidence_read: 'Evidence read',
};

function nonSuccessDescription(
  desc: ActivityDescription, p: Record<string, unknown>, event: BaseEvent,
  lifecycle: ReturnType<typeof toolLifecycle>,
): ActivityDescription {
  if (desc.status === 'succeeded' || desc.actionKind === 'named_check') return desc;
  const state = desc.status === 'unknown' ? 'outcome unknown' : desc.status;
  const verb = ACTION_VERBS[desc.actionKind] ?? readableLabel(String(p.tool_name ?? event.event_type));
  const { denied, cancelled, failed } = lifecycle;
  let prefixed: string | undefined;
  if (desc.actionKind === 'read' || desc.actionKind === 'write') {
    prefixed = denied ? `${verb} denied` : failed ? `${verb} failed` : cancelled ? `${verb} cancelled` : undefined;
  } else if (desc.actionKind === 'delete' || desc.actionKind === 'rename') {
    prefixed = denied ? `${verb} denied` : failed ? `${verb} failed` : undefined;
  } else if (desc.actionKind === 'commit') {
    prefixed = cancelled ? 'Commit cancelled' : 'Commit failed';
  }
  const outcome = prefixed?.toLowerCase().includes(state) ? prefixed : state;
  const duration = formatDuration(desc.durationMs);
  return {
    ...desc,
    title: `${verb} ${state}${desc.subject ? `: ${desc.subject}` : ''}`,
    outcome: `${outcome}${duration ? ` (${duration})` : ''}`,
  };
}

function describeBase(event: BaseEvent): ActivityDescription {
  const p = payloadOf(event);
  const durationMs = typeof p.duration_ms === 'number' && Number.isFinite(p.duration_ms) && p.duration_ms >= 0 ? p.duration_ms : undefined;
  const durText = formatDuration(durationMs);

  // 1. Tool call completion or execution
  if (
    event.event_type === 'tool_call.completed' ||
    event.event_type === 'tool.executed' ||
    event.event_type === 'tool.denied' ||
    typeof p.tool_name === 'string'
  ) {
    const toolName = String(p.tool_name ?? p.tool ?? '');
    const lifecycle = toolLifecycle(p, event.event_type);
    const { denied: isDenied, cancelled: isCancelled, failed: isFailed, succeeded: isSucceeded, status, tone } = lifecycle;

    if (toolName === 'repository.read_file') {
      const path = typeof p.path === 'string' && p.path ? p.path : undefined;
      const title = isSucceeded ? path ? `Read ${path}` : 'Read file' : '';
      const outcome = isSucceeded ? (durText ? `Succeeded (${durText})` : 'Succeeded') : undefined;
      return { title, outcome, subject: path, status, actionKind: 'read', tone, durationMs };
    }

    if (toolName === 'repository.write_file') {
      const path = typeof p.path === 'string' && p.path ? p.path : undefined;
      const created = p.created === true;
      const title = isSucceeded ? path ? (created ? `Created ${path}` : `Wrote ${path}`) : 'Wrote file' : '';
      const outcome = isSucceeded ? (durText ? `Written (${durText})` : 'Written') : undefined;
      return { title, outcome, subject: path, status, actionKind: 'write', tone: isSucceeded ? 'info' : tone, durationMs };
    }

    if (toolName === 'repository.delete_file') {
      const path = typeof p.path === 'string' && p.path ? p.path : undefined;
      const title = isSucceeded ? path ? `Deleted ${path}` : 'Deleted file' : '';
      const outcome = isSucceeded ? 'Deleted' : undefined;
      return { title, outcome, subject: path, status, actionKind: 'delete', tone: isSucceeded ? 'info' : tone, durationMs };
    }

    if (toolName === 'repository.rename_file') {
      const src = typeof p.source === 'string' ? p.source : '';
      const dest = typeof p.destination === 'string' ? p.destination : '';
      const title = isSucceeded ? (src && dest) ? `Renamed ${src} → ${dest}` : 'Renamed file' : '';
      const outcome = isSucceeded ? 'Renamed' : undefined;
      return { title, outcome, subject: (src && dest) ? `${src} → ${dest}` : undefined, status, actionKind: 'rename', tone: isSucceeded ? 'info' : tone, durationMs };
    }

    if (toolName === 'repository.list_files') {
      const count = validCount(p.entry_count);
      const title = isSucceeded ? count !== undefined ? `Listed files (${count} entries)` : 'Listed files' : '';
      return { title, outcome: isSucceeded ? (durText ? `Completed (${durText})` : 'Completed') : undefined, status, actionKind: 'list', tone, durationMs };
    }

    if (toolName === 'repository.search') {
      const count = validCount(p.match_count);
      const title = isSucceeded ? count !== undefined ? `Searched repository (${count} matches)` : 'Searched repository' : '';
      return { title, outcome: isSucceeded ? (durText ? `Completed (${durText})` : 'Completed') : undefined, status, actionKind: 'search', tone, durationMs };
    }

    if (toolName === 'repository.read_instructions') {
      const count = validCount(p.document_count);
      const title = isSucceeded ? count !== undefined ? `Read project instructions (${count} docs)` : 'Read project instructions' : '';
      return { title, outcome: isSucceeded ? (durText ? `Completed (${durText})` : 'Completed') : undefined, status, actionKind: 'read_instructions', tone, durationMs };
    }

    if (toolName === 'git.status') {
      return { title: isSucceeded ? 'Checked Git status' : '', outcome: isSucceeded ? 'Status read' : undefined, status, actionKind: 'status', tone, durationMs };
    }

    if (toolName === 'git.diff') {
      const count = validCount(p.changed_path_count);
      const title = isSucceeded ? count !== undefined ? `Computed Git diff (${count} changed paths)` : 'Computed Git diff' : '';
      return { title, outcome: isSucceeded ? 'Diff computed' : undefined, status, actionKind: 'diff', tone, durationMs };
    }

    if (toolName === 'git.commit') {
      const sha = typeof p.commit_sha === 'string' ? p.commit_sha : (typeof p.new_sha === 'string' ? p.new_sha : undefined);
      const shortSha = sha ? sha.slice(0, 7) : undefined;
      const title = isSucceeded ? shortSha ? `Committed changes (${shortSha})` : 'Committed changes' : '';
      return { title, outcome: isSucceeded ? 'Committed' : undefined, subject: shortSha, status, actionKind: 'commit', tone: isSucceeded ? 'success' : tone, durationMs };
    }

    if (toolName === 'build.run_named_check') {
      const cmd = typeof p.command_name === 'string' && p.command_name ? p.command_name : undefined;
      const exitCode = Number.isSafeInteger(p.exit_code) ? p.exit_code as number : undefined;
      const timedOut = p.timed_out === true;
      const check = (word: string, outcome: string, checkStatus: ActivityStatus, checkTone: ActivityTone): ActivityDescription => ({
        title: `Check ${word}${cmd ? `: ${cmd}` : ''}`, outcome, subject: cmd,
        status: checkStatus, actionKind: 'named_check', tone: checkTone, durationMs,
      });

      if (isDenied) {
        return check('denied', 'Denied', 'denied', 'warning');
      }

      if (isCancelled) return check('cancelled', 'Cancelled', 'cancelled', 'neutral');
      if (timedOut) {
        return check('timed out', 'Timed out', 'failed', 'danger');
      }
      if (isSucceeded && exitCode === 0) {
        return check('passed', durText ? `Exit 0 (${durText})` : 'Exit 0', 'succeeded', 'success');
      }
      if ((isFailed || isSucceeded) && exitCode !== undefined && exitCode !== 0) {
        return check('failed', durText ? `Exit ${exitCode} (${durText})` : `Exit ${exitCode}`, 'failed', 'danger');
      }
      if (isFailed) return check('failed', 'Failed', 'failed', 'danger');
      if (status === 'pending' || status === 'running') {
        return check(status, status, status, 'info');
      }
      // Without valid recorded exit code 0, tool success NEVER alone establishes pass
      return {
        title: cmd ? `Ran check: ${cmd}` : 'Ran check',
        outcome: 'Outcome incomplete',
        subject: cmd,
        status: 'unknown',
        actionKind: 'named_check',
        tone: 'neutral',
        durationMs,
      };
    }

    if (toolName === 'validation-results.read' || toolName === 'review-artifacts.read') {
      const kind = toolName === 'validation-results.read' ? 'validation' : 'review';
      const hasEvidence = p.has_evidence === true;
      const outcome = !isSucceeded ? undefined : hasEvidence ? 'Evidence read'
        : p.has_evidence === false ? 'No evidence available' : 'Availability not recorded';
      return {
        title: isSucceeded ? `${hasEvidence ? 'Read' : 'Checked for'} ${kind} evidence` : '',
        outcome, status, actionKind: 'evidence_read', tone, durationMs,
      };
    }

    // Generic fallback for any other tool
    const label = toolName ? readableLabel(toolName) : readableLabel(event.event_type);
    return {
      title: isSucceeded ? label : '',
      outcome: isSucceeded ? (durText ? `Completed (${durText})` : 'Completed') : undefined,
      status,
      actionKind: toolName || event.event_type,
      tone,
      durationMs,
    };
  }

  // 2. Operation intent requested
  if (
    event.event_type === 'operation.intent_created'
  ) {
    const kind = typeof p.operation_kind === 'string' ? p.operation_kind : '';
    const mapped = kind ? (FAMILIAR_OPERATIONS[kind] ?? readableLabel(kind)) : 'Operation';
    return {
      title: `${mapped} requested`,
      outcome: 'Requested',
      subject: kind || undefined,
      status: 'pending',
      actionKind: 'operation_intent',
      tone: 'neutral',
    };
  }

  // 3. Known lifecycle events
  if (event.event_type in LIFECYCLE_LABELS) {
    const title = LIFECYCLE_LABELS[event.event_type];
    return {
      title,
      outcome: 'Recorded',
      status: 'succeeded',
      actionKind: 'lifecycle',
      tone: event.event_type.includes('exhausted') ? 'warning' : 'neutral',
    };
  }

  // 4. Neutral fallback for unknown event types
  return {
    title: readableLabel(event.event_type),
    outcome: 'Recorded',
    status: 'unknown',
    actionKind: 'unknown',
    tone: 'neutral',
  };
}

export function describeActivity(event: BaseEvent): ActivityDescription {
  const desc = describeBase(event);
  if (!event.event_type.startsWith('tool.') && event.event_type !== 'tool_call.completed' && typeof event.payload?.tool_name !== 'string') return desc;
  const p = payloadOf(event);
  return nonSuccessDescription(desc, p, event, toolLifecycle(p, event.event_type));
}

export type ActivityGroup<T extends BaseEvent> = {
  key: string;
  isGroup: boolean;
  items: T[];
  count: number;
  summaryTitle: string;
  firstEvent: T;
  latestEvent: T;
};

function isSuccessfulReadOnly(event: BaseEvent): boolean {
  if (event.event_type !== 'tool_call.completed') return false;
  const p = payloadOf(event);
  if (toolLifecycle(p, event.event_type).status !== 'succeeded') return false;
  const toolName = String(p.tool_name ?? '');
  return READ_ONLY_TOOLS.has(toolName);
}

function getLineageKey(event: BaseEvent): string | null {
  const p = payloadOf(event);
  const keys = ['agent_execution_id', 'subscription_attempt_id', 'subscription_task_id', 'step_id'] as const;
  const parts: string[] = [];
  for (const key of keys) {
    const value = p[key];
    if (value === null || value === undefined) continue;
    if (typeof value !== 'string' || !value.trim() || value !== value.trim()) return null;
    parts.push(`${key}:${value}`);
  }
  if (!p.agent_execution_id && !p.subscription_attempt_id) return null;
  return parts.join('|');
}

function getTimestampMs(event: BaseEvent): number | null {
  const raw = event.occurred_at ?? event.created_at;
  if (!raw) return null;
  const ms = Date.parse(raw);
  return Number.isFinite(ms) ? ms : null;
}

export function groupRecentActivity<T extends BaseEvent>(
  events: T[],
  maxGapMs = 60_000,
): ActivityGroup<T>[] {
  const groups: ActivityGroup<T>[] = [];
  let currentGroup: T[] = [];

  function flushCurrent() {
    if (currentGroup.length === 0) return;
    const items = [...currentGroup];
    currentGroup = [];
    const latestEvent = items[0];
    const firstEvent = items[items.length - 1];
    const isGroup = items.length > 1;
    const count = items.length;

    let summaryTitle = '';
    if (isGroup) {
      const allReads = items.every(it => it.payload?.tool_name === 'repository.read_file');
      summaryTitle = allReads ? `${count} file reads` : `${count} read actions`;
    } else {
      summaryTitle = describeActivity(latestEvent).title;
    }

    groups.push({
      key: `group-${latestEvent.sequence ?? latestEvent.id ?? groups.length}-${count}`,
      isGroup,
      items,
      count,
      summaryTitle,
      firstEvent,
      latestEvent,
    });
  }

  for (let i = 0; i < events.length; i++) {
    const event = events[i];
    if (currentGroup.length === 0) {
      currentGroup.push(event);
      continue;
    }

    const prev = currentGroup[currentGroup.length - 1];
    const canGroup =
      isSuccessfulReadOnly(event) &&
      isSuccessfulReadOnly(prev) &&
      getLineageKey(event) !== null &&
      getLineageKey(event) === getLineageKey(currentGroup[0]) &&
      Number.isSafeInteger(event.run_version) &&
      event.run_version === currentGroup[0].run_version &&
      getTimestampMs(event) !== null &&
      getTimestampMs(prev) !== null &&
      getTimestampMs(currentGroup[0]) !== null &&
      getTimestampMs(event)! <= getTimestampMs(prev)! &&
      getTimestampMs(currentGroup[0])! - getTimestampMs(event)! <= maxGapMs;

    if (canGroup) {
      currentGroup.push(event);
    } else {
      flushCurrent();
      currentGroup.push(event);
    }
  }

  flushCurrent();
  return groups;
}
