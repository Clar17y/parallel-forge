import type { Tone } from '@/components/ui/tone';
import type { AuthoringOutcome } from '@/hooks/epics/types';

export interface AuthoringActivityDescription {
  state: string;
  title: string;
  description: string;
  tone: Tone;
  isExecuting: boolean;
  isTerminal: boolean;
  isWaiting: boolean;
  actionRequired?: string | null;
}

export interface AuthoringOutcomePresentationOptions {
  stale?: boolean;
  isNewSubmission?: boolean;
  requestKind?: string | null;
}

export function readableState(state: string): string {
  const words = state.replaceAll('_', ' ').replaceAll('-', ' ').trim().toLowerCase();
  return words ? words[0].toUpperCase() + words.slice(1) : 'Unknown';
}

export function describeAuthoringOutcome(
  outcome: Partial<AuthoringOutcome> | null | undefined,
  options?: AuthoringOutcomePresentationOptions
): AuthoringActivityDescription {
  const requestTitles: Record<string, string> = {
    'conversation-start': 'Saving conversation',
    'conversation-turn': 'Saving message',
    'job-submit': 'Submitting assistant job',
    'job-cancel': 'Requesting cancellation',
    'job-retry': 'Requesting assistant retry',
    'proposal-adopt': 'Adopting proposal',
    'decomp-conversation-start': 'Saving decomposition conversation',
    'decomp-conversation-turn': 'Saving decomposition message',
    'decomp-job-submit': 'Submitting decomposition job',
    'decomp-job-cancel': 'Requesting decomposition cancellation',
    'decomp-job-retry': 'Requesting decomposition retry',
    'decomp-proposal-adopt': 'Adopting decomposition proposal',
  };
  if (options?.requestKind && requestTitles[options.requestKind]) {
    return {
      state: 'requesting', title: requestTitles[options.requestKind],
      description: 'Waiting for the server to confirm this request.', tone: 'info',
      isExecuting: true, isTerminal: false, isWaiting: false, actionRequired: null,
    };
  }
  if (options?.isNewSubmission) {
    return {
      state: 'submitting',
      title: 'Preparing assistant request',
      description: 'Checking the saved conversation before submitting the assistant job.',
      tone: 'info',
      isExecuting: true,
      isTerminal: false,
      isWaiting: false,
      actionRequired: null,
    };
  }

  if (!outcome) {
    return {
      state: 'idle',
      title: 'Ready for prompt',
      description: 'Discuss requirements, then review an assistant proposal before adopting it.',
      tone: 'neutral',
      isExecuting: false,
      isTerminal: false,
      isWaiting: false,
      actionRequired: null,
    };
  }

  const rawState = outcome.state ?? 'unknown';

  if (options?.stale) {
    return {
      state: 'stale',
      title: `Last known state: ${readableState(rawState)} (Connection unavailable)`,
      description: 'Connection unavailable. Could not refresh current assistant status; retaining last confirmed state.',
      tone: 'warning',
      isExecuting: false, // Never assert live execution on stale/failed read
      isTerminal: ['proposed', 'failed', 'cancelled'].includes(rawState),
      isWaiting: false,
      actionRequired: 'Retry authoring or refresh to reconnect.',
    };
  }

  switch (rawState) {
    case 'queued':
      return {
        state: 'queued',
        title: 'Assistant job queued',
        description: 'The assistant is queued and waiting to begin drafting your proposal.',
        tone: 'info',
        isExecuting: false,
        isTerminal: false,
        isWaiting: true,
        actionRequired: null,
      };

    case 'running':
      return {
        state: 'running',
        title: 'Assistant drafting proposal',
        description: 'The assistant is actively drafting a proposal based on your conversation.',
        tone: 'info',
        isExecuting: true,
        isTerminal: false,
        isWaiting: false,
        actionRequired: null,
      };

    case 'quota_wait':
      return {
        state: 'quota_wait',
        title: 'Waiting for model quota',
        description: 'Assistant paused: provider quota limit reached. The job will resume automatically when quota resets.',
        tone: 'warning',
        isExecuting: false,
        isTerminal: false,
        isWaiting: true,
        actionRequired: 'You can wait for quota to reset, cancel the job, or add context while you wait.',
      };

    case 'capacity_wait':
      return {
        state: 'capacity_wait',
        title: 'Waiting for execution capacity',
        description: 'Assistant paused: system execution capacity reached. The job will resume automatically when capacity is freed.',
        tone: 'warning',
        isExecuting: false,
        isTerminal: false,
        isWaiting: true,
        actionRequired: 'You can wait for system capacity, cancel the job, or add context while you wait.',
      };

    case 'cancel_requested':
      return {
        state: 'cancel_requested',
        title: 'Cancellation in progress',
        description: 'Waiting for the assistant process to settle.',
        tone: 'warning',
        isExecuting: false,
        isTerminal: false,
        isWaiting: true,
        actionRequired: 'Awaiting process termination.',
      };

    case 'reconciling':
      return {
        state: 'reconciling',
        title: 'Reconciling assistant state',
        description: 'Verifying and reconciling assistant process state before continuing.',
        tone: 'warning',
        isExecuting: false,
        isTerminal: false,
        isWaiting: true,
        actionRequired: 'Process settlement and reconciliation pending.',
      };

    case 'proposed':
      return {
        state: 'proposed',
        title: 'Proposal ready for review',
        description: 'The assistant generated a proposal. Review the proposed draft below before adopting.',
        tone: 'success',
        isExecuting: false,
        isTerminal: true,
        isWaiting: false,
        actionRequired: null,
      };

    case 'cancelled':
      return {
        state: 'cancelled',
        title: 'Assistant job cancelled',
        description: 'The assistant job was cancelled.',
        tone: 'neutral',
        isExecuting: false,
        isTerminal: true,
        isWaiting: false,
        actionRequired: null,
      };

    case 'failed': {
      const isUnavailable = outcome.failure === 'unavailable';
      const timedOut = outcome.failure === 'timeout';
      const inputConflict = outcome.failure === 'input_conflict';
      const settled = outcome.process_settled ?? false;
      return {
        state: 'failed',
        title: 'Assistant job failed',
        description: isUnavailable ? 'Selected AI model could not be started.'
          : timedOut ? settled
            ? 'The assistant timed out and its process stopped.'
            : 'The assistant timed out. The process is still stopping or awaiting settlement.'
          : inputConflict ? 'The prompt or context changed. Refresh the latest context before retrying.'
          : `The assistant job reported: ${readableState(outcome.failure || 'failure')}.`,
        tone: 'danger',
        isExecuting: false,
        isTerminal: true,
        isWaiting: false,
        actionRequired: isUnavailable
          ? 'Check the local client setup and this project’s AI settings, then retry the assistant job.'
          : settled
            ? 'Review the failure details and retry the assistant job when ready.'
            : 'Process settlement pending before retry is available.',
      };
    }

    default:
      return {
        state: rawState,
        title: `Job ${readableState(rawState)}`,
        description: 'Inspect current outcome details below.',
        tone: 'neutral',
        isExecuting: false,
        isTerminal: false,
        isWaiting: false,
        actionRequired: null,
      };
  }
}
