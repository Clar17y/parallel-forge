'use client';

import { useEffect, useState, type FormEvent } from 'react';
import { Button } from '@/components/ui/button';
import { Panel } from '@/components/ui/panel';
import { StatusBadge } from '@/components/ui/status-badge';
import { useEpicBrainstorm } from '@/hooks/epics/use-epic-brainstorm';
import { EvidenceDetails } from './evidence-details';
import type { AuthoringOutcome, BrainstormProposal } from '@/hooks/epics/types';

function ProposalList({ title, items }: { title: string; items: string[] }) {
  if (!items.length) return null;
  return (
    <section>
      <h4 className="font-semibold text-xs text-[var(--muted)] uppercase">{title}</h4>
      <ul className="list-disc pl-4 mt-1 text-sm space-y-1">
        {items.map((item, index) => <li key={`${title}-${index}`}>{item}</li>)}
      </ul>
    </section>
  );
}

function ProposalPreview({ proposal }: { proposal: BrainstormProposal }) {
  return (
    <div className="p-4 rounded border border-[var(--border)] bg-[var(--surface)] space-y-4 mt-3">
      <div className="flex items-center justify-between border-b border-[var(--border)] pb-2">
        <h3 className="font-semibold">Proposed brief</h3>
        <StatusBadge label="Proposal" tone="warning" />
      </div>
      <section>
        <h4 className="font-semibold text-xs text-[var(--muted)] uppercase">Problem</h4>
        <p className="text-sm mt-1">{proposal.problem}</p>
      </section>
      <ProposalList title="Expected outcomes" items={proposal.outcomes} />
      <ProposalList title="In scope" items={proposal.scope} />
      <ProposalList title="Excluded" items={proposal.exclusions} />
      <section>
        <h4 className="font-semibold text-xs text-[var(--muted)] uppercase">Requirements and criteria</h4>
        <ul className="list-disc pl-4 mt-1 text-sm space-y-2">
          {proposal.requirements.map((requirement, index) => (
            <li key={`${requirement}-${index}`}>
              <span className="font-medium">{requirement}</span>
              {proposal.requirement_criteria?.[requirement]?.length ? (
                <ul className="list-square pl-5 text-[var(--muted)] mt-1 space-y-1">
                  {proposal.requirement_criteria[requirement].map((criterion, criterionIndex) => (
                    <li key={`${criterion}-${criterionIndex}`}>{criterion}</li>
                  ))}
                </ul>
              ) : null}
            </li>
          ))}
        </ul>
      </section>
      <ProposalList title="Proposed decisions" items={proposal.decisions} />
      <ProposalList title="Assumptions" items={proposal.assumptions} />
      <ProposalList title="Open questions" items={proposal.open_questions} />
      {!!proposal.evidence?.length && (
        <section>
          <h4 className="font-semibold text-xs text-[var(--muted)] uppercase">Supporting evidence</h4>
          <ul className="list-disc pl-4 mt-1 text-sm space-y-2">
            {proposal.evidence.map((evidence, index) => (
              <li key={`${evidence.path}-${index}`}>
                <span>{evidence.path}: {evidence.excerpt}</span>
                <EvidenceDetails summary="Inspect evidence digest"><span>{evidence.content_digest}</span></EvidenceDetails>
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}

function formatState(state: string): string {
  return state.replaceAll('_', ' ');
}

function UsageValue({ label, value }: { label: string; value: number | null | undefined }) {
  return <li>{label}: {value === null || value === undefined ? 'Unknown' : value}</li>;
}

function JobOutcome({ outcome }: { outcome: AuthoringOutcome }) {
  const usage = outcome.usage;
  const unknown = outcome.unknown_usage_fields ?? [];
  const held = outcome.held_reservations;
  const settlementLabel = outcome.process_settled
    ? 'Process settled'
    : outcome.state === 'cancelled'
      ? 'No process settlement reported'
      : 'Process settlement pending';
  const settlementTone = outcome.process_settled || outcome.state === 'cancelled' ? 'neutral' : 'warning';
  return (
    <Panel title="Assistant job" description={`Job state: ${formatState(outcome.state)}`}>
      <div className="space-y-3 text-sm">
        <div className="flex flex-wrap items-center gap-2">
          <StatusBadge label={formatState(outcome.state)} tone={outcome.state === 'failed' ? 'danger' : outcome.state === 'proposed' ? 'success' : 'info'} />
          <StatusBadge label={settlementLabel} tone={settlementTone} />
        </div>
        {outcome.failure && <p role="alert">The job reported: {formatState(outcome.failure)}.</p>}
        <p>Usage: {outcome.usage_known === true ? 'measured' : outcome.usage_known === false ? 'unknown' : 'not yet reported'}.</p>
        {usage && (
          <ul className="list-disc pl-5 space-y-1">
            <UsageValue label="Duration (ms)" value={usage.duration_ms} />
            <UsageValue label="Duration lower bound (ms)" value={usage.duration_lower_bound_ms} />
            <UsageValue label="Tool calls" value={usage.tool_call_count} />
            <UsageValue label="Input tokens" value={usage.input_tokens} />
            <UsageValue label="Output tokens" value={usage.output_tokens} />
            <UsageValue label={`Estimated cost${outcome.currency ? ` (${outcome.currency})` : ''}`} value={usage.estimated_api_cost_minor} />
          </ul>
        )}
        {unknown.length > 0 && <p>Unknown usage: {unknown.map(formatState).join(', ')}.</p>}
        {held && outcome.held_reasons && Object.values(outcome.held_reasons).some(Boolean) && (
          <EvidenceDetails summary="Inspect held usage reservations">
            <span>{JSON.stringify(held)}</span>
            <span>{JSON.stringify(outcome.held_reasons)}</span>
          </EvidenceDetails>
        )}
        <EvidenceDetails summary="Inspect job identity and version">
          <span>job_id: {outcome.job_id}</span>
          <span>job_version: {outcome.job_version}</span>
          {outcome.proposal_digest && <span>proposal_digest: {outcome.proposal_digest}</span>}
        </EvidenceDetails>
      </div>
    </Panel>
  );
}

export function AuthoringWorkspace({
  epicId,
  projectId,
  epicVersion,
}: {
  epicId: string;
  projectId: string;
  epicVersion: number;
}) {
  const brainstorm = useEpicBrainstorm(epicId, projectId);
  const [inputText, setInputText] = useState('');
  const {
    threads,
    activeConversationId,
    setActiveConversationId,
    turns,
    outcome,
    loading,
    isUnavailable,
    refresh,
    startConversation,
    appendTurn,
    submitJob,
    adoptProposal,
    cancelJob,
    retryJob,
    jobs,
    selectedJobId,
    setSelectedJobId,
    adoptedRevisionId,
    mutations,
  } = brainstorm;
  const activeThread = threads.find(thread => thread.conversation_id === activeConversationId);
  const latestOperatorTurn = [...turns].reverse().find(turn => turn.role === 'operator');
  const mutationPending = mutations.loading || mutations.hasPendingRetry;
  const registerCompletion = mutations.registerCompletion;
  const isAuthoringAction = !mutations.actionKind || ['conversation-start', 'conversation-turn', 'job-submit', 'job-cancel', 'job-retry', 'proposal-adopt'].includes(mutations.actionKind);

  useEffect(() => {
    const clearSubmittedPrompt = (_receipt: unknown, request: { body: Record<string, unknown> }) => {
      const submittedText = typeof request.body.text === 'string' ? request.body.text : null;
      if (!submittedText) return;
      setInputText(current => current.trim() === submittedText.trim() ? '' : current);
    };
    const unregisterStart = registerCompletion('conversation-start', clearSubmittedPrompt);
    const unregisterTurn = registerCompletion('conversation-turn', clearSubmittedPrompt);
    return () => { unregisterStart(); unregisterTurn(); };
  }, [registerCompletion]);

  const handleSendTurn = async (event: FormEvent) => {
    event.preventDefault();
    const submittedText = inputText.trim();
    if (!submittedText || mutationPending || isUnavailable) return;
    try {
      if (!activeConversationId) {
        await startConversation(submittedText);
      } else if (activeThread) {
        await appendTurn(activeConversationId, activeThread.conversation_version, submittedText);
      }
    } catch {
      // The mutation owner retains uncertain requests for an exact retry.
    }
  };

  const handleSubmitJob = async () => {
    if (!activeConversationId || !activeThread || !latestOperatorTurn || mutationPending || isUnavailable) return;
    try {
      await submitJob(activeConversationId, latestOperatorTurn.turn_id, epicVersion, activeThread.conversation_version);
    } catch {
      // The mutation owner retains uncertain requests for an exact retry.
    }
  };

  const handleAdopt = async () => {
    if (isUnavailable || !outcome?.proposal || !outcome.proposal_digest || outcome.state !== 'proposed') return;
    try {
      await adoptProposal(outcome.job_id, outcome.proposal_digest, outcome.job_version, epicVersion);
    } catch {
      // The mutation owner retains uncertain requests for an exact retry.
    }
  };

  const handleJobControl = async (action: 'cancel' | 'retry') => {
    if (!outcome || mutationPending || isUnavailable) return;
    try {
      if (action === 'cancel') await cancelJob(outcome.job_id, outcome.job_version);
      else await retryJob(outcome.job_id, outcome.job_version);
    } catch {
      // The mutation owner retains uncertain requests for an exact retry.
    }
  };

  return (
    <div className="authoring-workspace space-y-6">
      {mutations.hasPendingRetry && !mutations.shared && (
        <div role="alert" className="p-4 bg-[var(--warning-soft)] text-[var(--warning)] rounded border border-[var(--border)] space-y-2">
          <p className="font-semibold">The last request may still have completed.</p>
          <p className="text-sm">Retry the saved request to check its result before starting another action.</p>
          <Button variant="primary" disabled={mutations.loading} onClick={() => { void mutations.retryPending().catch(() => undefined); }}>
            {mutations.loading ? 'Retrying…' : 'Retry original request'}
          </Button>
        </div>
      )}

      {mutations.error && isAuthoringAction && (
        <div role="alert" className="p-4 bg-[var(--danger-soft)] text-[var(--danger)] rounded border border-[var(--border)] space-y-2">
          <p>{mutations.error}</p>
          <div className="flex flex-wrap gap-2">
            {mutations.conflict && <Button variant="secondary" onClick={() => { mutations.clearError(); refresh(); }}>Refresh authoring</Button>}
            <Button variant="quiet" onClick={mutations.clearError}>Dismiss message</Button>
          </div>
        </div>
      )}

      {isUnavailable && (
        <div role="alert" className="p-4 bg-[var(--warning-soft)] text-[var(--warning)] rounded border border-[var(--border)] space-y-2">
          <p className="font-semibold">Authoring service unavailable</p>
          <p className="text-sm">Saved conversations and assistant drafting could not be loaded. Manual brief and work item editing remain available.</p>
          <Button variant="secondary" onClick={refresh}>Retry authoring</Button>
        </div>
      )}

      {loading && <p role="status">Loading authoring…</p>}
      {adoptedRevisionId && <p role="status" className="p-3 bg-[var(--success-soft)] text-[var(--success)] rounded text-sm">Proposed brief adopted as a new revision.</p>}

      <div className="flex flex-col items-stretch gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h2 className="text-lg font-semibold">Authoring conversations</h2>
          <p className="text-sm text-[var(--muted)]">Discuss requirements, then review an assistant proposal before adopting it.</p>
        </div>
        {threads.length > 1 && (
          <label className="text-sm min-w-0">Conversation
            <select
              aria-label="Conversation"
              className="mt-1 block w-full min-w-0 px-3 py-1.5 text-sm border border-[var(--control-border)] rounded bg-[var(--surface)] sm:mt-0 sm:inline-block sm:w-auto"
              value={activeConversationId ?? ''}
              onChange={event => setActiveConversationId(event.target.value)}
              disabled={mutationPending}
            >
              {threads.map((thread, index) => <option key={thread.conversation_id} value={thread.conversation_id}>Conversation {index + 1}</option>)}
            </select>
          </label>
        )}
        {jobs.length > 1 && (
          <label className="text-sm min-w-0">Assistant job
            <select
              aria-label="Assistant job"
              className="mt-1 block w-full min-w-0 px-3 py-1.5 text-sm border border-[var(--control-border)] rounded bg-[var(--surface)] sm:mt-0 sm:inline-block sm:w-auto"
              value={selectedJobId ?? ''}
              disabled={mutationPending || !activeConversationId}
              onChange={event => setSelectedJobId(event.target.value || null)}
            >
              {jobs.map((jobId, index) => <option key={jobId} value={jobId}>Job {index + 1}</option>)}
            </select>
          </label>
        )}
      </div>

      <div className="space-y-4">
        {turns.length === 0 ? (
          <p className="p-6 bg-[var(--surface-muted)] rounded border border-[var(--border)] text-center text-sm text-[var(--muted)]">No messages yet. Send a prompt to begin.</p>
        ) : turns.map(turn => {
          const operator = turn.role === 'operator';
          const matchingOutcome = outcome?.proposal?.turn_id === turn.proposal?.turn_id ? outcome : null;
          const canAdopt = matchingOutcome?.state === 'proposed' && !!matchingOutcome.proposal_digest && !matchingOutcome.adopted_revision_id;
          return (
            <article key={turn.turn_id} className={`p-4 rounded border ${operator ? 'bg-[var(--surface)] border-[var(--border)] mr-8' : 'bg-[var(--info-soft)] border-[var(--info)] ml-8'} space-y-3`}>
              <div className="flex items-center justify-between">
                <span className="font-semibold text-xs uppercase tracking-wider text-[var(--muted)]">{operator ? 'Your message' : 'Assistant message'}</span>
                <EvidenceDetails summary="Inspect message ID"><span>{turn.turn_id}</span></EvidenceDetails>
              </div>
              <p className="text-sm whitespace-pre-wrap">{turn.text}</p>
              {turn.proposal && (
                <>
                  <ProposalPreview proposal={turn.proposal} />
                  {canAdopt && (
                    <Button variant="primary" disabled={mutationPending || isUnavailable} onClick={handleAdopt}>Adopt proposed brief</Button>
                  )}
                  {matchingOutcome?.adopted_revision_id && <p className="text-sm text-[var(--success)]">This proposal has been adopted.</p>}
                </>
              )}
            </article>
          );
        })}
      </div>

      {outcome && (
        <>
          <JobOutcome outcome={outcome} />
          {outcome.state === 'cancel_requested' && <p role="status">Cancellation requested. Waiting for the process to settle.</p>}
          <div className="flex flex-wrap gap-2">
            {['queued', 'running', 'quota_wait', 'capacity_wait', 'reconciling'].includes(outcome.state) && (
              <Button variant="secondary" disabled={mutationPending || isUnavailable} onClick={() => { void handleJobControl('cancel'); }}>Cancel assistant job</Button>
            )}
            {outcome.state === 'failed' && outcome.process_settled && (
              <Button variant="secondary" disabled={mutationPending || isUnavailable} onClick={() => { void handleJobControl('retry'); }}>Retry assistant job</Button>
            )}
          </div>
        </>
      )}
      {adoptedRevisionId && <EvidenceDetails summary="Inspect adopted revision"><span>brief_revision_id: {adoptedRevisionId}</span></EvidenceDetails>}

      <form onSubmit={handleSendTurn} className="space-y-3">
        <label htmlFor="turn-text" className="block text-sm font-medium">Send message or instruction</label>
        <textarea
          id="turn-text"
          className="w-full px-3 py-2 border border-[var(--control-border)] rounded text-sm bg-[var(--surface)] focus:border-[var(--focus)] focus:outline-none min-h-[80px]"
          value={inputText}
          onChange={event => setInputText(event.target.value)}
          placeholder="Ask for clarification or refine the requirements…"
          maxLength={8000}
          required
          disabled={mutationPending || isUnavailable}
        />
        <Button type="submit" variant="primary" disabled={mutationPending || isUnavailable || !inputText.trim()}>
          {mutations.loading ? 'Sending…' : activeConversationId ? 'Send message' : 'Start conversation'}
        </Button>
      </form>

      {latestOperatorTurn && (!outcome || ['cancelled', 'proposed', 'failed'].includes(outcome.state)) && (
        <Panel title="Ask the assistant to draft" description="The assistant reads the saved conversation and current brief context to create a versioned proposal.">
          <Button variant="secondary" disabled={mutationPending || isUnavailable || !activeThread} onClick={() => { void handleSubmitJob(); }}>
            {mutations.pendingMutation?.kind === 'job-submit' ? 'Draft request pending' : 'Generate proposed brief'}
          </Button>
        </Panel>
      )}

      <Panel title="Graph decomposition" description="There is no available graph decomposition request contract yet.">
        <p className="text-sm text-[var(--muted)]">Use the Graph Editor to create and review work items manually.</p>
      </Panel>
    </div>
  );
}
