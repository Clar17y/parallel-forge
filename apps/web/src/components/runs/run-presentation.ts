import type { components } from '@/lib/api/schema';

// Presentation only. These functions must never manufacture available commands,
// approve a gate, or substitute for RunControls' evidence-binding checks.
type Projection = components['schemas']['RunProjection'];
type State = components['schemas']['RunState'];
import type { Tone } from '../ui/tone';
export type { Tone } from '../ui/tone';
export type CheckDisplay = { label: string; tone: Tone };
export type RunDescription = { title: string; description: string; tone: Tone };

export function readableLabel(value: string): string {
  const words = value.replaceAll('_', ' ').replaceAll('.', ' ').trim().toLowerCase();
  return words ? words[0].toUpperCase() + words.slice(1) : 'Unknown';
}

const descriptions: Record<State, RunDescription> = {
  CREATED: { title: 'Run created', description: 'Planning is next. Implementation requires a separate plan approval.', tone: 'neutral' },
  PLANNING: { title: 'Preparing a plan', description: 'Forge is in the planning phase. Review the plan before authorising implementation.', tone: 'info' },
  AWAITING_PLAN_APPROVAL: { title: 'Plan approval needed', description: 'Review the proposed scope and evidence before authorising implementation.', tone: 'warning' },
  PREPARING_WORKTREE: { title: 'Preparing the workspace', description: 'Forge is preparing isolated resources for the approved plan.', tone: 'info' },
  IMPLEMENTING: { title: 'Implementation phase', description: 'Work is governed by the approved plan. Task scheduling and provider waits are shown in Tasks.', tone: 'info' },
  VALIDATING: { title: 'Validating the candidate', description: 'Inspect recorded check results below. Publishing a pull request requires a separate approval.', tone: 'info' },
  REVIEWING: { title: 'Review phase', description: 'Inspect independent review evidence and any findings in Review.', tone: 'info' },
  REMEDIATING: { title: 'Repairing local findings', description: 'Remediation is bounded by the retained policy and remaining attempts. Review the latest checks and activity.', tone: 'warning' },
  AWAITING_PR_APPROVAL: { title: 'PR publication approval needed', description: 'Review the exact candidate, validation and proposed PR before approving publication.', tone: 'warning' },
  PUBLISHING_PR: { title: 'Publishing the approved candidate', description: 'Forge is in the PR publication phase. Publication does not authorise merging.', tone: 'info' },
  MONITORING_PR: { title: 'Monitoring the pull request', description: 'Observed GitHub results are shown below. Merging still requires exact human approval.', tone: 'info' },
  AWAITING_HUMAN_INTERVENTION: { title: 'Human attention required', description: 'Review the recorded reason in Activity and any PR outcome before taking further action.', tone: 'danger' },
  AWAITING_MERGE_APPROVAL: { title: 'Merge approval needed', description: 'Review the exact current evidence. Only the server-provided approval action can authorise merging.', tone: 'warning' },
  MERGING: { title: 'Merge operation in progress', description: 'Wait for a recorded outcome. Queue admission is not confirmation of a completed merge.', tone: 'info' },
  PAUSED: { title: 'Run paused', description: 'Resume only through the available run controls. Retained policy, budgets and recovery holds still apply.', tone: 'neutral' },
  COMPLETED: { title: 'Run completed', description: 'Inspect the recorded outcome and retained evidence. A completed merge is shown only when its commit is recorded.', tone: 'success' },
  FAILED: { title: 'Run failed', description: 'Inspect the recorded failure in Activity. Resources and evidence are retained until explicit teardown.', tone: 'danger' },
  CANCELLED: { title: 'Run cancelled', description: 'Resources and evidence remain available until explicit teardown.', tone: 'neutral' },
};

type EventItem = components['schemas']['EventItem'];

export type RemediationOrigin = 'remote' | 'local' | 'unrecorded';

// Remote repair is admitted only by a recorded PR observation, and the event that
// opens the current remediation names that context. Local remediation transitions
// (review findings, evidence drift, requested changes) never carry it.
function remoteObservationContext(event: EventItem | undefined): boolean {
  const payload = event?.payload;
  if (!payload || typeof payload !== 'object') return false;
  const rec = payload as Record<string, unknown>;
  const digest = rec.observation_digest;
  return rec.disposition === 'remediate'
    || (typeof digest === 'string' && /^[0-9a-f]{64}$/.test(digest));
}

export function remediationOrigin(projection: Projection): RemediationOrigin {
  const events = Array.isArray(projection?.latest_events) ? projection.latest_events : [];
  // A resume event records restored_state and no target, so a paused and
  // resumed repair keeps the transition that actually opened it.
  const transition = [...events]
    .sort((a, b) => (b.sequence ?? 0) - (a.sequence ?? 0))
    .find(event => {
      const payload = event?.payload;
      return typeof payload === 'object' && payload !== null && (payload as Record<string, unknown>).target === 'REMEDIATING';
    });
  if (!transition) return 'unrecorded';
  const isRemote = events.some(
    event => event?.run_version === transition.run_version && remoteObservationContext(event),
  );
  return isRemote ? 'remote' : 'local';
}

export function describeRunState(projection: Projection): RunDescription {
  if (projection.recovery_hold) return {
    title: 'Recovery needs attention', tone: 'danger',
    description: 'An earlier operation has an unresolved outcome. Resume and resource teardown are held until its evidence is reconciled. Review the activity before taking further action.',
  };
  if (projection.run.state === 'REMEDIATING') {
    const origin = remediationOrigin(projection);
    if (origin === 'remote') {
      return {
        title: 'Repairing observed PR findings',
        description: 'Remediation is triggered by a recorded pull request observation. A repaired candidate still requires fresh validation and review.',
        tone: 'warning',
      };
    }
    if (origin === 'unrecorded') {
      return {
        title: 'Repairing recorded findings',
        description: 'The retained event window no longer shows which evidence opened this repair. Review the latest checks, the GitHub observation and activity before relying on either remediation budget.',
        tone: 'warning',
      };
    }
    return descriptions.REMEDIATING;
  }
  return descriptions[projection.run.state] ?? { title: readableLabel(projection.run.state), description: 'Inspect the recorded state and available controls.', tone: 'neutral' };
}

function checkHead(head: string | null, candidate: string | null): CheckDisplay | null {
  if (!candidate) return { label: 'No candidate', tone: 'neutral' };
  if (!head) return { label: 'Head unknown', tone: 'neutral' };
  if (head !== candidate) return { label: 'Different head', tone: 'neutral' };
  return null;
}

const successfulStatuses = ['succeeded', 'passed', 'completed'];

export function localCheckDisplay(check: components['schemas']['CheckItem'], candidate: string | null): CheckDisplay {
  const head = checkHead(check.head_sha, candidate);
  if (head) return head;
  const status = check.status.toLowerCase();
  if (successfulStatuses.includes(status) && check.exit_code === 0) return { label: 'Passed', tone: 'success' };
  if (['failed', 'error', 'timed_out'].includes(status) || (typeof check.exit_code === 'number' && check.exit_code !== 0)) return { label: 'Failed', tone: 'danger' };
  if (['running', 'in_progress'].includes(status)) return { label: 'Running', tone: 'info' };
  if (['pending', 'queued'].includes(status)) return { label: 'Queued', tone: 'neutral' };
  // Do not turn an unknown provider result or a missing exit code into success.
  return { label: successfulStatuses.includes(status) ? 'Outcome incomplete' : readableLabel(check.status), tone: 'neutral' };
}

export function remoteCheckDisplay(check: components['schemas']['RemoteCheckItem'], candidate: string | null, observedHead: string | null): CheckDisplay {
  const observation = checkHead(observedHead, candidate);
  if (observation) return observation;
  const head = checkHead(check.head_sha, candidate);
  if (head) return head;
  if (check.status === 'completed' && check.conclusion === 'success') return { label: 'Passed', tone: 'success' };
  if (['failure', 'timed_out', 'startup_failure'].includes(check.conclusion ?? '')) return { label: 'Failed', tone: 'danger' };
  if (check.conclusion === 'action_required') return { label: 'Action required', tone: 'warning' };
  if (check.status === 'in_progress') return { label: 'Running', tone: 'info' };
  if (['queued', 'pending', 'waiting', 'requested'].includes(check.status)) return { label: 'Queued', tone: 'neutral' };
  return { label: check.conclusion ? readableLabel(check.conclusion) : 'Outcome pending', tone: 'neutral' };
}

const phase: Partial<Record<State, string>> = {
  CREATED: 'plan', PLANNING: 'plan', AWAITING_PLAN_APPROVAL: 'plan',
  PREPARING_WORKTREE: 'build', IMPLEMENTING: 'build', VALIDATING: 'build', REMEDIATING: 'build',
  REVIEWING: 'review', AWAITING_PR_APPROVAL: 'publish', PUBLISHING_PR: 'publish',
  MONITORING_PR: 'ci', AWAITING_MERGE_APPROVAL: 'merge', MERGING: 'merge',
};
export type WorkflowStep = { key: string; label: string; detail: string; current: boolean; recorded: boolean };

export function workflowSteps(p: Projection): WorkflowStep[] {
  const suspended = p.run.state === 'PAUSED';
  const effectiveState = suspended ? p.run.suspended_state ?? p.run.state : p.run.state;
  const current = phase[effectiveState];
  const candidate = p.candidate.commit;
  const reviewed = !!candidate && p.review.head_sha === candidate && !!p.review.evidence_digest && p.review.evidence_digest === p.candidate.review_evidence_digest;
  const published = !!candidate && p.pull_request?.head_sha === candidate;
  const observed = !!candidate && p.remote_observation?.head_sha === candidate && p.remote_observation.checks.length > 0;
  const origin = effectiveState === 'REMEDIATING' ? remediationOrigin(p) : null;
  const buildLabel = origin === 'remote' || origin === 'unrecorded' ? 'Repair & revalidate' : 'Build & validate';
  const currentDetail = p.recovery_hold
    ? 'Recovery held'
    : suspended
      ? 'Paused here'
      : origin === 'remote'
        ? 'Remote repair'
        : origin === 'unrecorded'
          ? 'Repair in progress'
          : 'Current phase';
  const records: Array<[string, string, boolean, string]> = [
    ['plan', 'Plan', !!p.plan.approval_id, 'Approval recorded'],
    // A commit alone does not prove that implementation or its checks passed.
    ['build', buildLabel, false, candidate ? 'Candidate recorded' : 'No candidate'],
    ['review', 'Review', reviewed, 'Review recorded'],
    ['publish', 'Publish PR', published, 'PR recorded'],
    // This is observation presence, deliberately not a required-check verdict.
    ['ci', 'GitHub checks', observed, 'Observation recorded'],
    ['merge', 'Merge', !!p.pull_request?.merge_sha, 'Merge recorded'],
  ];
  return records.map(([key, label, recorded, detail]) => ({
    key, label, recorded, current: key === current,
    detail: key === current
      ? currentDetail
      : recorded || key === 'build' ? detail : 'Not recorded',
  }));
}

export function nextGateMessage(p: Projection): string {
  if (p.recovery_hold) return 'Reconcile the unresolved operation before resuming work or removing resources.';
  if (!p.next_gate) return 'No approval gate is currently reported. Available controls remain authoritative.';
  const label = { plan: 'Plan approval', pr: 'PR publication approval', merge: 'Merge approval' }[p.next_gate];
  return p.available_commands.some(command => command.name === `approve_${p.next_gate}`)
    ? `${label}: Review the exact current evidence using the approval control above.`
    : `${label} is not available yet. Review checks and activity; Forge will expose the action when authorised.`;
}
