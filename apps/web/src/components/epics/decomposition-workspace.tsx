'use client';

import { LoadingStatus } from '@/components/ui/loading-status';
import { useEffect, useState, type FormEvent } from 'react';
import { Button } from '@/components/ui/button';
import { Panel } from '@/components/ui/panel';
import { StatusBadge } from '@/components/ui/status-badge';
import { useEpicDecomposition } from '@/hooks/epics/use-epic-decomposition';
import { useEpicWorkspace } from '@/hooks/epics/use-epic-workspace';
import { EvidenceDetails } from './evidence-details';
import { AuthoringJobOutcome } from './authoring-job-outcome';
import { describeAuthoringOutcome } from './authoring-presentation';
import { ActivityStatus } from '@/components/ui/activity-status';
import type {
  BrainstormProposal,
  DecompositionProposal,
} from '@/hooks/epics/types';

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

export function isDecompositionProposal(
  proposal: BrainstormProposal | DecompositionProposal
): proposal is DecompositionProposal {
  return 'items' in proposal && Array.isArray(proposal.items);
}

export function DecompositionProposalPreview({ proposal }: { proposal: DecompositionProposal }) {
  return (
    <div className="p-4 rounded border border-[var(--border)] bg-[var(--surface)] space-y-4 mt-3">
      <div className="flex items-center justify-between border-b border-[var(--border)] pb-2">
        <h3 className="font-semibold">Proposed decomposition</h3>
        <StatusBadge label="Proposal" tone="warning" />
      </div>

      <EvidenceDetails summary="Inspect source brief revision">
        <span>brief_revision_id: {proposal.brief_revision_id}</span>
        <span>brief_digest: {proposal.brief_digest}</span>
      </EvidenceDetails>

      {proposal.problem && (
        <section>
          <h4 className="font-semibold text-xs text-[var(--muted)] uppercase">Problem</h4>
          <p className="text-sm mt-1">{proposal.problem}</p>
        </section>
      )}

      {proposal.summary && (
        <section>
          <h4 className="font-semibold text-xs text-[var(--muted)] uppercase">Summary</h4>
          <p className="text-sm mt-1">{proposal.summary}</p>
        </section>
      )}

      <section>
        <h4 className="font-semibold text-xs text-[var(--muted)] uppercase">
          Proposed work items ({proposal.items.length})
        </h4>
        <div className="mt-2 space-y-3">
          {proposal.items.map((item, index) => (
            <div
              key={item.item_id || index}
              className="p-3 rounded border border-[var(--border)] bg-[var(--surface-muted)] space-y-2"
            >
              <div className="flex items-center justify-between">
                <span className="font-medium text-sm">
                  {item.ordinal}. {item.title}
                </span>
                <StatusBadge
                  label={item.disposition}
                  tone={item.disposition === 'required' ? 'info' : 'neutral'}
                />
              </div>
              {item.outcome && <p className="text-sm text-[var(--muted)]">{item.outcome}</p>}
              {item.acceptance_criteria && item.acceptance_criteria.length > 0 && (
                <div>
                  <h5 className="font-semibold text-xs text-[var(--muted)] uppercase">Acceptance criteria</h5>
                  <ul className="list-disc pl-4 mt-1 text-sm space-y-1">
                    {item.acceptance_criteria.map((criterion, cIdx) => (
                      <li key={`${item.item_id}-ac-${cIdx}`}>{criterion}</li>
                    ))}
                  </ul>
                </div>
              )}
              {item.dependency_item_ids && item.dependency_item_ids.length > 0 && (
                <div>
                  <h5 className="font-semibold text-xs text-[var(--muted)] uppercase">Dependencies</h5>
                  <ul className="list-disc pl-4 mt-1 text-xs text-[var(--muted)] space-y-1">
                    {item.dependency_item_ids.map(depId => (
                      <li key={depId}>{depId}</li>
                    ))}
                  </ul>
                </div>
              )}
              {item.source_requirement_ids && item.source_requirement_ids.length > 0 && (
                <div>
                  <h5 className="font-semibold text-xs text-[var(--muted)] uppercase">Source requirements</h5>
                  <ul className="list-disc pl-4 mt-1 text-xs text-[var(--muted)] space-y-1">
                    {item.source_requirement_ids.map(reqId => (
                      <li key={reqId}>{reqId}</li>
                    ))}
                  </ul>
                </div>
              )}
              <EvidenceDetails summary="Inspect item ID">
                <span>item_id: {item.item_id}</span>
              </EvidenceDetails>
            </div>
          ))}
        </div>
      </section>

      {proposal.assumptions?.length > 0 && (
        <ProposalList title="Assumptions" items={proposal.assumptions} />
      )}
      {proposal.open_questions?.length > 0 && (
        <ProposalList title="Open questions" items={proposal.open_questions} />
      )}
      {!!proposal.evidence?.length && (
        <section>
          <h4 className="font-semibold text-xs text-[var(--muted)] uppercase">Supporting evidence</h4>
          <ul className="list-disc pl-4 mt-1 text-sm space-y-2">
            {proposal.evidence.map((evidence, index) => (
              <li key={`${evidence.path}-${index}`}>
                <span>{evidence.path}: {evidence.excerpt}</span>
                <EvidenceDetails summary="Inspect evidence digest">
                  <span>{evidence.content_digest}</span>
                </EvidenceDetails>
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}

export function DecompositionWorkspace({
  epicId,
  projectId,
  epicVersion,
}: {
  epicId: string;
  projectId: string;
  epicVersion: number;
}) {
  const decomp = useEpicDecomposition(epicId, projectId);
  const workspace = useEpicWorkspace(epicId);
  const [inputText, setInputText] = useState('');
  const [retryOwnerOverride, setRetryOwnerOverride] = useState(false);
  const [retryOverrideNote, setRetryOverrideNote] = useState('');

  const {
    threads,
    activeConversationId,
    setActiveConversationId,
    turns,
    outcome,
    outcomeStale,
    outcomeChecking,
    jobReceiptAccepted,
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
    adoptedGraphRevisionId,
    mutations,
  } = decomp;

  const activeRequestKind = mutations.loading ? mutations.pendingMutation?.kind : null;
  const isSubmitting = !!(activeRequestKind && ['decomp-conversation-start', 'decomp-conversation-turn', 'decomp-job-submit'].includes(activeRequestKind));
  const activityDesc = describeAuthoringOutcome(
    isSubmitting ? null : outcome,
    {
      requestKind: activeRequestKind && activeRequestKind.startsWith('decomp-') ? activeRequestKind : null,
      stale: outcomeStale,
    }
  );

  const currentEpicVersion = workspace.epic?.version ?? epicVersion;
  const activeThread = threads.find(thread => thread.conversation_id === activeConversationId);
  const latestOperatorTurn = [...turns].reverse().find(turn => turn.role === 'operator');
  const registerCompletion = mutations.registerCompletion;
  const refreshProjections = workspace.refreshProjections;
  const mutationPending = mutations.loading || mutations.hasPendingRetry;
  const currentActionKind = mutations.pendingMutation?.kind ?? mutations.actionKind;
  const isDecompositionAction = !!currentActionKind?.startsWith('decomp-');

  useEffect(() => {
    const clearSubmittedPrompt = (_receipt: unknown, request: { body: Record<string, unknown> }) => {
      const submittedText = typeof request.body.text === 'string' ? request.body.text : null;
      if (!submittedText) return;
      setInputText(current => (current.trim() === submittedText.trim() ? '' : current));
    };
    const unregisterStart = registerCompletion('decomp-conversation-start', clearSubmittedPrompt);
    const unregisterTurn = registerCompletion('decomp-conversation-turn', clearSubmittedPrompt);
    return () => {
      unregisterStart();
      unregisterTurn();
    };
  }, [registerCompletion]);

  useEffect(() => {
    const unregisterAdopt = registerCompletion('decomp-proposal-adopt', () => {
      refreshProjections();
    });
    return () => {
      unregisterAdopt();
    };
  }, [registerCompletion, refreshProjections]);

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
      await submitJob(
        activeConversationId,
        latestOperatorTurn.turn_id,
        currentEpicVersion,
        activeThread.conversation_version
      );
    } catch {
      // The mutation owner retains uncertain requests for an exact retry.
    }
  };

  const handleAdopt = async () => {
    if (isUnavailable || !outcome?.proposal || !outcome.proposal_digest || outcome.state !== 'proposed') return;
    try {
      await adoptProposal(outcome.job_id, outcome.proposal_digest, outcome.job_version, currentEpicVersion);
    } catch {
      // The mutation owner retains uncertain requests for an exact retry.
    }
  };

  const handleJobControl = async (action: 'cancel' | 'retry') => {
    if (!outcome || mutationPending || isUnavailable) return;
    try {
      if (action === 'cancel') {
        await cancelJob(outcome.job_id, outcome.job_version);
      } else {
        await retryJob(
          outcome.job_id,
          outcome.job_version,
          retryOwnerOverride,
          retryOverrideNote.trim() || undefined
        );
      }
    } catch {
      // The mutation owner retains uncertain requests for an exact retry.
    }
  };

  return (
    <div className="decomposition-workspace space-y-6">
      {mutations.hasPendingRetry && !mutations.shared && isDecompositionAction && (
        <div role="alert" className="p-4 bg-[var(--warning-soft)] text-[var(--warning)] rounded border border-[var(--border)] space-y-2">
          <p className="font-semibold">The last request may still have completed.</p>
          <p className="text-sm">Retry the saved request to check its result before starting another action.</p>
          <Button
            variant="primary"
            disabled={mutations.loading}
            onClick={() => { void mutations.retryPending().catch(() => undefined); }}
          >
            {mutations.loading ? 'Retrying…' : 'Retry original request'}
          </Button>
        </div>
      )}

      {mutations.error && isDecompositionAction && (
        <div role="alert" className="p-4 bg-[var(--danger-soft)] text-[var(--danger)] rounded border border-[var(--border)] space-y-2">
          <p>{mutations.error}</p>
          <div className="flex flex-wrap gap-2">
            {mutations.conflict && (
              <Button variant="secondary" onClick={() => { mutations.clearError(); refresh(); }}>
                Refresh decomposition
              </Button>
            )}
            <Button variant="quiet" onClick={mutations.clearError}>Dismiss message</Button>
          </div>
        </div>
      )}

      {isUnavailable && (
        <div role="alert" className="p-4 bg-[var(--warning-soft)] text-[var(--warning)] rounded border border-[var(--border)] space-y-2">
          <p className="font-semibold">Decomposition service unavailable</p>
          <p className="text-sm">Saved decomposition conversations and assistant drafting could not be loaded. Manual graph editing remains available.</p>
          <Button variant="secondary" onClick={refresh}>Retry decomposition</Button>
        </div>
      )}

      {loading && <LoadingStatus>Loading decomposition…</LoadingStatus>}
      {(adoptedGraphRevisionId || outcome?.adopted_revision_id) && (
        <p role="status" className="p-3 bg-[var(--success-soft)] text-[var(--success)] rounded text-sm">
          Proposed decomposition adopted as a new graph revision.
        </p>
      )}

      <div className="flex flex-col items-stretch gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h3 className="text-md font-semibold">Decomposition conversations</h3>
          <p className="text-sm text-[var(--muted)]">Discuss work-item breakdown, then review an assistant decomposition proposal before adopting it.</p>
        </div>
        {threads.length > 1 && (
          <label className="text-sm min-w-0">Decomposition conversation
            <select
              aria-label="Decomposition conversation"
              className="mt-1 block w-full min-w-0 px-3 py-1.5 text-sm border border-[var(--control-border)] rounded bg-[var(--surface)] sm:mt-0 sm:inline-block sm:w-auto"
              value={activeConversationId ?? ''}
              onChange={event => setActiveConversationId(event.target.value)}
              disabled={mutationPending}
            >
              {threads.map((thread, index) => (
                <option key={thread.conversation_id} value={thread.conversation_id}>
                  Conversation {index + 1}
                </option>
              ))}
            </select>
          </label>
        )}
        {jobs.length > 1 && (
          <label className="text-sm min-w-0">Decomposition job
            <select
              aria-label="Decomposition job"
              className="mt-1 block w-full min-w-0 px-3 py-1.5 text-sm border border-[var(--control-border)] rounded bg-[var(--surface)] sm:mt-0 sm:inline-block sm:w-auto"
              value={selectedJobId ?? ''}
              disabled={mutationPending || !activeConversationId}
              onChange={event => setSelectedJobId(event.target.value || null)}
            >
              {jobs.map((jobId, index) => (
                <option key={jobId} value={jobId}>Job {index + 1}</option>
              ))}
            </select>
          </label>
        )}
      </div>

      {outcomeChecking && !isSubmitting && activityDesc.state === 'idle' ? (
        <ActivityStatus title={jobReceiptAccepted ? 'Job submitted · checking assistant status' : 'Checking assistant status'}
          description="Waiting for the selected decomposition job’s current status." tone="info" isExecuting />
      ) : activityDesc && (activityDesc.state !== 'idle' || outcome) ? (
        <ActivityStatus
          title={activityDesc.title}
          description={activityDesc.description}
          tone={activityDesc.tone}
          isExecuting={activityDesc.isExecuting}
          isWaiting={activityDesc.isWaiting}
          actionRequired={activityDesc.actionRequired}
        />
      ) : null}

      <div className="space-y-4">
        {turns.length === 0 ? (
          <p className="p-6 bg-[var(--surface-muted)] rounded border border-[var(--border)] text-center text-sm text-[var(--muted)]">
            No decomposition messages yet. Send a prompt to begin.
          </p>
        ) : turns.map(turn => {
          const operator = turn.role === 'operator';
          const matchingOutcome =
            outcome?.proposal?.turn_id === turn.proposal?.turn_id ? outcome : null;
          const isAdopted = !!(
            matchingOutcome?.adopted_revision_id ||
            (matchingOutcome && adoptedGraphRevisionId)
          );
          const canAdopt =
            !isSubmitting && !outcomeStale &&
            matchingOutcome?.state === 'proposed' &&
            !!matchingOutcome.proposal_digest &&
            !isAdopted;
          const decompProposal =
            turn.proposal && isDecompositionProposal(turn.proposal) ? turn.proposal : null;

          return (
            <article
              key={turn.turn_id}
              className={`p-4 rounded border ${
                operator
                  ? 'bg-[var(--surface)] border-[var(--border)] mr-8'
                  : 'bg-[var(--info-soft)] border-[var(--info)] ml-8'
              } space-y-3`}
            >
              <div className="flex items-center justify-between">
                <span className="font-semibold text-xs uppercase tracking-wider text-[var(--muted)]">
                  {operator ? 'Your message' : 'Assistant message'}
                </span>
                <EvidenceDetails summary="Inspect message ID">
                  <span>{turn.turn_id}</span>
                </EvidenceDetails>
              </div>
              <p className="text-sm whitespace-pre-wrap">{turn.text}</p>
              {decompProposal && (
                <>
                  <DecompositionProposalPreview proposal={decompProposal} />
                  {canAdopt && (
                    <Button
                      variant="primary"
                      disabled={mutationPending || isUnavailable}
                      onClick={handleAdopt}
                    >
                      Adopt proposed decomposition
                    </Button>
                  )}
                  {isAdopted && (
                    <p className="text-sm text-[var(--success)]">
                      This decomposition proposal has been adopted.
                    </p>
                  )}
                </>
              )}
            </article>
          );
        })}
      </div>

      {outcome && (
        <>
          <AuthoringJobOutcome outcome={outcome} title="Decomposition assistant job" stale={outcomeStale} />
          {outcome.state === 'cancel_requested' && !outcomeStale && (
            <p role="status">Cancellation requested. Waiting for the process to settle.</p>
          )}
          <div className="flex flex-wrap gap-2">
            {['queued', 'running', 'quota_wait', 'capacity_wait', 'reconciling'].includes(
              outcome.state
            ) && (
              <Button
                variant="secondary"
                disabled={mutationPending || isUnavailable}
                onClick={() => { void handleJobControl('cancel'); }}
              >
                Cancel decomposition job
              </Button>
            )}
            {outcome.state === 'failed' && outcome.process_settled && (
              <div className="space-y-2">
                <Button
                  variant="secondary"
                  disabled={mutationPending || isUnavailable}
                  onClick={() => { void handleJobControl('retry'); }}
                >
                  Retry decomposition job
                </Button>
                <div className="flex flex-col gap-2 pt-1 text-xs">
                  <label className="flex items-center gap-2 cursor-pointer font-medium text-[var(--muted)]">
                    <input
                      type="checkbox"
                      aria-label="Owner override decomposition retry policy"
                      checked={retryOwnerOverride}
                      disabled={mutationPending || isUnavailable}
                      onChange={e => setRetryOwnerOverride(e.target.checked)}
                    />
                    <span>Owner override retry policy</span>
                  </label>
                  {retryOwnerOverride && (
                    <input
                      type="text"
                      aria-label="Decomposition override note"
                      className="px-2 py-1 border border-[var(--control-border)] rounded text-xs bg-[var(--surface)] max-w-sm"
                      placeholder="Optional override note..."
                      value={retryOverrideNote}
                      disabled={mutationPending || isUnavailable}
                      onChange={e => setRetryOverrideNote(e.target.value)}
                    />
                  )}
                </div>
              </div>
            )}
          </div>
        </>
      )}
      {(adoptedGraphRevisionId || outcome?.adopted_revision_id) && (
        <EvidenceDetails summary="Inspect adopted graph revision">
          <span>graph_revision_id: {adoptedGraphRevisionId ?? outcome?.adopted_revision_id}</span>
        </EvidenceDetails>
      )}

      <form onSubmit={handleSendTurn} className="space-y-3">
        <label htmlFor="decomp-turn-text" className="block text-sm font-medium">
          Decomposition message or instruction
        </label>
        <textarea
          id="decomp-turn-text"
          className="w-full px-3 py-2 border border-[var(--control-border)] rounded text-sm bg-[var(--surface)] focus:border-[var(--focus)] focus:outline-none min-h-[80px]"
          value={inputText}
          onChange={event => setInputText(event.target.value)}
          placeholder="Describe how to break down the brief into work items…"
          maxLength={8000}
          required
          disabled={mutationPending || isUnavailable}
        />
        <Button
          type="submit"
          variant="primary"
          busy={!!(activeRequestKind && ['decomp-conversation-start', 'decomp-conversation-turn'].includes(activeRequestKind))}
          disabled={mutationPending || isUnavailable || !inputText.trim()}
        >
          {activeRequestKind && ['decomp-conversation-start', 'decomp-conversation-turn'].includes(activeRequestKind)
            ? 'Sending…'
            : activeConversationId
              ? 'Send decomposition message'
              : 'Start decomposition conversation'}
        </Button>
      </form>

      {latestOperatorTurn &&
        (!outcome || ['cancelled', 'proposed', 'failed'].includes(outcome.state)) && (
          <Panel
            title="Ask the assistant to decompose"
            description="The assistant reads the saved decomposition conversation and current brief context to create a versioned work-item proposal."
          >
            <Button
              variant="secondary"
              busy={activeRequestKind === 'decomp-job-submit'}
              disabled={mutationPending || isUnavailable || !activeThread}
              onClick={() => { void handleSubmitJob(); }}
            >
              {activeRequestKind === 'decomp-job-submit'
                ? 'Submitting decomposition job…'
                : 'Generate proposed decomposition'}
            </Button>
          </Panel>
        )}
    </div>
  );
}
