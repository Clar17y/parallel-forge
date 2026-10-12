import type { Tone } from '@/components/ui/tone';
import type { AuthoringOutcome, AuthoringDiagnosticEvent } from '@/hooks/epics/types';

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

export function readableToolName(toolName: string | null | undefined): string {
  if (!toolName) return 'tool';
  switch (toolName) {
    case 'list_files':
      return 'List files';
    case 'read_file':
      return 'Read file';
    case 'search':
      return 'Search';
    case 'read_instructions':
      return 'Read instructions';
    default:
      return readableState(toolName);
  }
}

export function readableOperation(operation: string | null | undefined): string {
  if (!operation) return '';
  switch (operation) {
    case 'repository_read':
      return 'Repository read';
    case 'provider_receive':
      return 'Waiting for model';
    case 'process_settlement':
      return 'Process settlement';
    case 'client_initialize':
      return 'Client initialization';
    case 'account_read':
      return 'Account check';
    case 'model_list':
      return 'Model availability check';
    case 'config_read':
      return 'Client configuration check';
    case 'thread_start':
      return 'Conversation setup';
    case 'turn_start':
      return 'Turn setup';
    case 'tool_reply':
      return 'Repository result delivery';
    default:
      return readableState(operation);
  }
}

const clientWaitOperations = new Set([
  'client_initialize', 'account_read', 'model_list', 'config_read',
  'thread_start', 'turn_start', 'tool_reply',
]);

export function readableDiagnosticEventLabel(event: AuthoringDiagnosticEvent): string {
  switch (event.kind) {
    case 'launch':
      return 'Process launched';
    case 'tool_started':
      return `Repository read started (${readableToolName(event.tool_name)})`;
    case 'tool_completed':
      return `Repository read completed (${readableToolName(event.tool_name)})`;
    case 'tool_failed':
      return `Repository read failed (${readableToolName(event.tool_name)})`;
    case 'provider_wait':
      return clientWaitOperations.has(event.operation ?? '')
        ? `Waiting for ${readableOperation(event.operation).toLowerCase()}`
        : 'Waiting for model';
    case 'provider_retry':
      return 'Model request retry';
    case 'provider_error':
      return 'Model error';
    case 'proposal_received':
      return 'Proposal received';
    case 'settlement_started':
      return 'Settlement started';
    case 'process_settled':
      return 'Process settled';
    case 'attempt_stopped':
      return 'Attempt stopped';
    default:
      return readableState(event.kind);
  }
}

export function formatUtcTimestamp(dateString: string): string {
  const d = new Date(dateString);
  if (isNaN(d.getTime())) return dateString;
  return `${d.toISOString().slice(0, 19).replace('T', ' ')} UTC`;
}

export function isSubstantiveEvent(event: AuthoringDiagnosticEvent): boolean {
  return !['settlement_started', 'process_settled', 'attempt_stopped'].includes(event.kind);
}

export function getLastSubstantiveOperation(events?: AuthoringDiagnosticEvent[] | null): AuthoringDiagnosticEvent | null {
  if (!events || events.length === 0) return null;
  for (let i = events.length - 1; i >= 0; i--) {
    if (isSubstantiveEvent(events[i])) {
      return events[i];
    }
  }
  return events[events.length - 1] ?? null;
}

export function formatSubstantiveOperation(event: AuthoringDiagnosticEvent): string {
  let label = '';
  if (event.operation === 'repository_read' || event.tool_name || event.kind.startsWith('tool_')) {
    const toolPart = readableToolName(event.tool_name);
    if (event.kind === 'tool_started') {
      label = `repository read (${toolPart})`;
    } else if (event.kind === 'tool_completed') {
      label = `repository read (${toolPart}) completed`;
    } else if (event.kind === 'tool_failed') {
      label = `repository read (${toolPart}) failed`;
    } else {
      label = `repository read (${toolPart})`;
    }
  } else if (event.kind === 'provider_retry') {
    label = 'waiting for model retry';
  } else if (event.kind === 'provider_error') {
    label = 'model error';
  } else if (event.kind === 'provider_wait') {
    label = readableDiagnosticEventLabel(event).toLowerCase();
  } else if (event.kind === 'proposal_received') {
    label = 'proposal received';
  } else if (event.kind === 'launch') {
    label = 'process launch';
  } else {
    label = readableDiagnosticEventLabel(event).toLowerCase();
  }

  const elapsedStr = `${event.elapsed_ms.toLocaleString()} ms`;
  if (event.duration_ms !== null && event.duration_ms !== undefined) {
    return `${label} at ${elapsedStr} (duration: ${event.duration_ms.toLocaleString()} ms)`;
  }
  return `${label} at ${elapsedStr}`;
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

    case 'running': {
      const events = outcome.diagnostics?.events;
      if (events && events.length > 0) {
        const latest = events[events.length - 1];
        if (latest.kind === 'provider_wait' && clientWaitOperations.has(latest.operation ?? '')) {
          return {
            state: 'running',
            title: latest.operation === 'tool_reply' ? 'Returning repository result' : 'Preparing AI client',
            description: `Waiting for ${readableOperation(latest.operation).toLowerCase()}. Recorded at ${latest.elapsed_ms.toLocaleString()} ms into the attempt.`,
            tone: 'info',
            isExecuting: true,
            isTerminal: false,
            isWaiting: true,
            actionRequired: null,
          };
        }
        if (latest.kind === 'tool_started') {
          return {
            state: 'running',
            title: 'Reading repository',
            description: `Repository read in progress (${readableToolName(latest.tool_name)}). Recorded at ${latest.elapsed_ms.toLocaleString()} ms into the attempt.`,
            tone: 'info',
            isExecuting: true,
            isTerminal: false,
            isWaiting: false,
            actionRequired: null,
          };
        }
        if (latest.kind === 'tool_completed' || latest.kind === 'provider_wait') {
          return {
            state: 'running',
            title: 'Waiting for model',
            description: `Waiting for model response. Recorded at ${latest.elapsed_ms.toLocaleString()} ms into the attempt.`,
            tone: 'info',
            isExecuting: true,
            isTerminal: false,
            isWaiting: true,
            actionRequired: null,
          };
        }
        if (latest.kind === 'provider_retry') {
          return {
            state: 'running',
            title: 'Waiting for model retry',
            description: `Model request retry in progress. Recorded at ${latest.elapsed_ms.toLocaleString()} ms into the attempt.`,
            tone: 'warning',
            isExecuting: true,
            isTerminal: false,
            isWaiting: true,
            actionRequired: null,
          };
        }
        if (latest.kind === 'provider_error') {
          return {
            state: 'running',
            title: 'Model provider error',
            description: `Model error encountered${latest.failure ? `: ${readableState(latest.failure)}` : ''}. Recorded at ${latest.elapsed_ms.toLocaleString()} ms into the attempt.`,
            tone: 'warning',
            isExecuting: true,
            isTerminal: false,
            isWaiting: true,
            actionRequired: null,
          };
        }
        if (latest.kind === 'tool_failed') {
          return {
            state: 'running',
            title: 'Repository read failed',
            description: `Repository read (${readableToolName(latest.tool_name)}) failed. Recorded at ${latest.elapsed_ms.toLocaleString()} ms into the attempt.`,
            tone: 'warning',
            isExecuting: true,
            isTerminal: false,
            isWaiting: true,
            actionRequired: null,
          };
        }
        if (latest.kind === 'proposal_received') {
          return {
            state: 'running',
            title: 'Proposal received',
            description: `Proposal received from model. Finalizing output. Recorded at ${latest.elapsed_ms.toLocaleString()} ms into the attempt.`,
            tone: 'info',
            isExecuting: true,
            isTerminal: false,
            isWaiting: false,
            actionRequired: null,
          };
        }
        if (latest.kind === 'process_settled' || latest.kind === 'attempt_stopped') {
          return {
            state: 'running',
            title: 'Finalizing assistant job',
            description: `${latest.kind === 'process_settled' ? 'The assistant process stopped.' : 'The attempt stopped.'} Finalizing the saved job outcome. Recorded at ${latest.elapsed_ms.toLocaleString()} ms into the attempt.`,
            tone: 'info',
            isExecuting: true,
            isTerminal: false,
            isWaiting: true,
            actionRequired: null,
          };
        }
        if (latest.kind === 'settlement_started') {
          return {
            state: 'running',
            title: 'Stopping assistant process',
            description: `Process settlement in progress. Recorded at ${latest.elapsed_ms.toLocaleString()} ms into the attempt.`,
            tone: 'warning',
            isExecuting: true,
            isTerminal: false,
            isWaiting: true,
            actionRequired: null,
          };
        }
      }
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
    }

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

    case 'cancel_requested': {
      const lastSubstantive = getLastSubstantiveOperation(outcome.diagnostics?.events);
      const lastSubstantiveText = lastSubstantive ? ` Last substantive operation: ${formatSubstantiveOperation(lastSubstantive)}.` : '';
      return {
        state: 'cancel_requested',
        title: 'Cancellation in progress',
        description: `Waiting for the assistant process to settle.${lastSubstantiveText}`,
        tone: 'warning',
        isExecuting: false,
        isTerminal: false,
        isWaiting: true,
        actionRequired: 'Awaiting process termination.',
      };
    }

    case 'reconciling': {
      const lastSubstantive = getLastSubstantiveOperation(outcome.diagnostics?.events);
      const lastSubstantiveText = lastSubstantive ? ` Last substantive operation: ${formatSubstantiveOperation(lastSubstantive)}.` : '';
      return {
        state: 'reconciling',
        title: 'Reconciling assistant state',
        description: `Verifying and reconciling assistant process state before continuing.${lastSubstantiveText}`,
        tone: 'warning',
        isExecuting: false,
        isTerminal: false,
        isWaiting: true,
        actionRequired: 'Process settlement and reconciliation pending.',
      };
    }

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

    case 'cancelled': {
      const settled = outcome.process_settled ?? false;
      const lastSubstantive = getLastSubstantiveOperation(outcome.diagnostics?.events);
      const lastSubstantiveText = lastSubstantive ? ` Last substantive operation: ${formatSubstantiveOperation(lastSubstantive)}.` : '';
      return {
        state: 'cancelled',
        title: 'Assistant job cancelled',
        description: outcome.diagnostics
          ? settled
            ? `The assistant job was cancelled and its process stopped.${lastSubstantiveText}`
            : `The assistant job was cancelled. The process is still stopping or awaiting settlement.${lastSubstantiveText}`
          : 'The assistant job was cancelled.',
        tone: 'neutral',
        isExecuting: false,
        isTerminal: true,
        isWaiting: false,
        actionRequired: null,
      };
    }

    case 'failed': {
      const isUnavailable = outcome.failure === 'unavailable';
      const timedOut = outcome.failure === 'timeout';
      const inputConflict = outcome.failure === 'input_conflict';
      const settled = outcome.process_settled ?? false;
      const lastSubstantive = getLastSubstantiveOperation(outcome.diagnostics?.events);
      const lastSubstantiveText = lastSubstantive ? ` Last substantive operation: ${formatSubstantiveOperation(lastSubstantive)}.` : '';
      let description: string;
      if (isUnavailable) {
        description = 'Selected AI model could not be started.';
      } else if (timedOut) {
        description = settled
          ? `The assistant timed out and its process stopped.${lastSubstantiveText}`
          : `The assistant timed out. The process is still stopping or awaiting settlement.${lastSubstantiveText}`;
      } else if (inputConflict) {
        description = 'The prompt or context changed. Refresh the latest context before retrying.';
      } else {
        const failureLabel = readableState(outcome.failure || 'failure');
        description = settled
          ? `The assistant job reported: ${failureLabel}.${lastSubstantiveText}`
          : `The assistant job reported: ${failureLabel}. Process settlement pending.${lastSubstantiveText}`;
      }
      return {
        state: 'failed',
        title: 'Assistant job failed',
        description,
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
