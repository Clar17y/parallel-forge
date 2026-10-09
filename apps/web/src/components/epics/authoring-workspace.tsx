'use client';

import { useCallback, useEffect, useLayoutEffect, useRef, useState, useSyncExternalStore, type FormEvent } from 'react';
import { api, ApiError } from '@/lib/api/client';
import { Button } from '@/components/ui/button';
import { StatusBadge } from '@/components/ui/status-badge';
import { useEpicBrainstorm } from '@/hooks/epics/use-epic-brainstorm';
import { EvidenceDetails } from './evidence-details';
import { AuthoringJobOutcome } from './authoring-job-outcome';
import { BrainstormModelPicker } from './brainstorm-model-picker';
import { toBrainstormRoute, type BrainstormRoute } from './brainstorm-model-choice';
import type { BrainstormProposal, BrainstormTurn, DecompositionProposal } from '@/hooks/epics/types';

const subscribeHydration = () => () => undefined;
const clientHydrated = () => true;
const serverHydrated = () => false;
type Submission = { turnId: string; expectedEpicVersion: number; idempotencyKey: string };
type SavedSend = { conversationId: string; version: number; expectedEpicVersion: number; route?: BrainstormRoute | null; submission?: Submission; retryGeneration?: number };
// A null entry is a completed tombstone when browser storage refuses removal.
const inMemorySavedSends = new Map<string, SavedSend | null>();
const inMemoryPendingRoutes = new Map<string, BrainstormRoute | null>();

function readPendingRoute(key: string): BrainstormRoute | null {
  if (inMemoryPendingRoutes.has(key)) return inMemoryPendingRoutes.get(key) ?? null;
  try { return toBrainstormRoute(JSON.parse(window.sessionStorage.getItem(key) ?? 'null')); } catch { return null; }
}

function savePendingRoute(key: string, route: BrainstormRoute | null) {
  inMemoryPendingRoutes.set(key, route);
  try { window.sessionStorage.setItem(key, JSON.stringify(route)); } catch { /* in-page recovery remains available */ }
}

function removePendingRoute(key: string) {
  inMemoryPendingRoutes.delete(key);
  try { window.sessionStorage.removeItem(key); } catch { /* completed send has no pending route */ }
}

function readSavedSend(key: string, fallbackEpicVersion: number): SavedSend | null {
  if (inMemorySavedSends.has(key)) return inMemorySavedSends.get(key) ?? null;
  try {
    const raw = window.sessionStorage.getItem(key);
    if (!raw) return inMemorySavedSends.get(key) ?? null;
    const parsed: unknown = JSON.parse(raw);
    if (!parsed || typeof parsed !== 'object') return inMemorySavedSends.get(key) ?? null;
    const value = parsed as Partial<SavedSend>;
    if (value.expectedEpicVersion !== undefined &&
      (!Number.isSafeInteger(value.expectedEpicVersion) || (value.expectedEpicVersion ?? -1) < 0)) return null;
    if (value.retryGeneration !== undefined && (!Number.isSafeInteger(value.retryGeneration) || value.retryGeneration < 0)) return null;
    const submission = value.submission;
    const route = value.route === undefined || value.route === null ? value.route : toBrainstormRoute(value.route);
    if (value.route && !route) return null;
    if (submission && (typeof submission.turnId !== 'string' || !submission.turnId ||
      typeof submission.idempotencyKey !== 'string' || !submission.idempotencyKey ||
      !Number.isSafeInteger(submission.expectedEpicVersion) || submission.expectedEpicVersion < 0)) return null;
    return typeof value.conversationId === 'string' && /^[A-Za-z0-9_-]{1,128}$/.test(value.conversationId) &&
      Number.isSafeInteger(value.version) && (value.version ?? 0) >= 2
      ? {
        conversationId: value.conversationId,
        version: value.version!,
        expectedEpicVersion: Number.isSafeInteger(value.expectedEpicVersion) && (value.expectedEpicVersion ?? -1) >= 0
          ? value.expectedEpicVersion!
          : fallbackEpicVersion,
        ...(submission ? { submission } : {}),
        ...(route !== undefined ? { route } : {}),
        ...(value.retryGeneration ? { retryGeneration: value.retryGeneration } : {}),
      }
      : inMemorySavedSends.get(key) ?? null;
  } catch { return inMemorySavedSends.get(key) ?? null; }
}

function saveSavedSend(key: string, saved: SavedSend) {
  inMemorySavedSends.set(key, saved);
  try { window.sessionStorage.setItem(key, JSON.stringify(saved)); } catch { /* in-page recovery remains available */ }
}

function removeSavedSend(key: string) {
  inMemorySavedSends.set(key, null);
  try { window.sessionStorage.removeItem(key); } catch { /* in-memory completion still prevents a duplicate */ }
}

function assistanceIdempotencyKey(epicId: string, conversationId: string, turnId: string) {
  return `brainstorm:${epicId}:${conversationId}:${turnId}`;
}

function savedSendForReceipt(
  previous: SavedSend | null,
  conversationId: string,
  version: number,
  epicVersion: number,
  pendingRouteKey: string,
): SavedSend {
  const matches = previous?.conversationId === conversationId && previous.version === version;
  return {
    conversationId,
    version,
    expectedEpicVersion: matches ? previous.expectedEpicVersion : epicVersion,
    // A matching legacy send without a route also means the project default.
    route: matches ? previous.route ?? null : readPendingRoute(pendingRouteKey),
  };
}

export function resetSavedBrainstormSendsForTesting() {
  inMemorySavedSends.clear();
  inMemoryPendingRoutes.clear();
}

function isBrainstormProposal(
  proposal: BrainstormProposal | DecompositionProposal
): proposal is BrainstormProposal {
  return 'requirements' in proposal && Array.isArray(proposal.requirements);
}

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



export function AuthoringWorkspace({
  epicId,
  projectId,
  epicVersion,
  onReviewBrief,
}: {
  epicId: string;
  projectId: string;
  epicVersion: number;
  onReviewBrief?: () => void;
}) {
  const brainstorm = useEpicBrainstorm(epicId, projectId);
  const [inputText, setInputText] = useState('');
  const [routeChoice, setRouteChoice] = useState<BrainstormRoute | null>(null);
  const [savedMessageState, setSavedMessage] = useState<SavedSend | false | null>(null);
  const [sendError, setSendError] = useState<string | null>(null);
  const [assistancePending, setAssistancePending] = useState(false);
  const sendLock = useRef(false);
  const sendIntent = useRef(false);
  const lifecycle = useRef({ active: false, subject: '' });
  const subject = `${epicId}:${projectId}`;
  const savedSendKey = `epic_saved_brainstorm_send_${epicId}`;
  const pendingRouteKey = `epic_pending_brainstorm_route_${epicId}`;
  const currentEpicVersion = useRef(epicVersion);
  const hydrated = useSyncExternalStore(subscribeHydration, clientHydrated, serverHydrated);
  const restoredSavedMessage = hydrated && savedMessageState === null ? readSavedSend(savedSendKey, epicVersion) : null;
  const savedMessage = savedMessageState === false ? null : savedMessageState ?? restoredSavedMessage;
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
  const mutationPending = mutations.loading || mutations.hasPendingRetry;
  const registerCompletion = mutations.registerCompletion;
  const currentActionKind = mutations.pendingMutation?.kind ?? mutations.actionKind;
  const isAuthoringAction =
    !currentActionKind ||
    (!currentActionKind.startsWith('decomp-') &&
      ['conversation-start', 'conversation-turn', 'job-submit', 'job-cancel', 'job-retry', 'proposal-adopt'].includes(
      currentActionKind
      ));

  useLayoutEffect(() => {
    currentEpicVersion.current = epicVersion;
    lifecycle.current = { active: true, subject };
    return () => { lifecycle.current.active = false; };
  }, [epicVersion, subject]);

  const isCurrentInstance = useCallback(() => lifecycle.current.active && lifecycle.current.subject === subject, [subject]);

  const rejectSavedSubmission = useCallback((saved: SavedSend) => {
    const current = readSavedSend(savedSendKey, currentEpicVersion.current);
    if (!current?.submission || current.conversationId !== saved.conversationId || current.version !== saved.version ||
      current.submission.idempotencyKey !== saved.submission?.idempotencyKey) return;
    const recovered: SavedSend = { ...current, submission: undefined, retryGeneration: (current.retryGeneration ?? 0) + 1 };
    saveSavedSend(savedSendKey, recovered);
    if (isCurrentInstance()) setSavedMessage(recovered);
  }, [isCurrentInstance, savedSendKey]);

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

  const requestAssistance = useCallback(async (saved: SavedSend, ownsLock = false) => {
    if (!isCurrentInstance()) return;
    if (!ownsLock) {
      if (sendLock.current) return;
      sendLock.current = true;
    }
    setAssistancePending(true);
    setSavedMessage(saved);
    saveSavedSend(savedSendKey, saved);
    setSendError(null);
    try {
      const savedTurns = await api<BrainstormTurn[]>(
        `/epics/${epicId}/brainstorm-conversations/${saved.conversationId}/turns?project_id=${encodeURIComponent(projectId)}`,
      );
      if (!isCurrentInstance()) return;
      const prompt = savedTurns?.[saved.version - 2];
      if (!prompt || prompt.role !== 'operator' || prompt.conversation_id !== saved.conversationId) {
        throw new Error('The saved message could not be confirmed yet.');
      }
      if (saved.submission && saved.submission.turnId !== prompt.turn_id) {
        throw new Error('The saved request no longer matches its confirmed message.');
      }
      const submission = saved.submission ?? {
        turnId: prompt.turn_id,
        expectedEpicVersion: currentEpicVersion.current,
        idempotencyKey: `${assistanceIdempotencyKey(epicId, saved.conversationId, prompt.turn_id)}${saved.retryGeneration ? `:${saved.retryGeneration}` : ''}`,
      };
      // Freeze identity and body before the first request can reach the server.
      const frozen = { ...saved, submission };
      saveSavedSend(savedSendKey, frozen);
      setSavedMessage(frozen);
      try {
        await submitJob(saved.conversationId, submission.turnId, submission.expectedEpicVersion, saved.version,
          submission.idempotencyKey, saved.route ?? undefined);
      } catch (error) {
        if (error instanceof ApiError && error.status === 409) rejectSavedSubmission(frozen);
        throw error;
      }
      if (!isCurrentInstance()) return;
      removeSavedSend(savedSendKey);
      removePendingRoute(pendingRouteKey);
      setSavedMessage(false);
    } catch {
      if (isCurrentInstance()) setSendError('Your message is saved. We could not start the assistant yet; retry assistance when the conversation is available.');
    } finally {
      sendLock.current = false;
      if (isCurrentInstance()) setAssistancePending(false);
    }
  }, [epicId, isCurrentInstance, pendingRouteKey, projectId, rejectSavedSubmission, savedSendKey, submitJob]);

  useEffect(() => {
    if (mutations.conflict && mutations.actionKind === 'job-submit' && !mutations.hasPendingRetry) {
      const saved = readSavedSend(savedSendKey, epicVersion);
      if (saved?.submission) rejectSavedSubmission(saved);
    }
  }, [epicVersion, mutations.actionKind, mutations.conflict, mutations.hasPendingRetry, rejectSavedSubmission, savedSendKey]);

  useEffect(() => {
    const resumeSavedSend = (receipt: unknown, request: { kind?: string; path: string }) => {
      if (request.kind === 'conversation-start') {
        if (sendIntent.current) return;
        const receiptValue = receipt as { conversation_id: string; version: number };
        const previous = readSavedSend(savedSendKey, epicVersion);
        void requestAssistance(savedSendForReceipt(previous, receiptValue.conversation_id,
          receiptValue.version, epicVersion, pendingRouteKey));
      } else if (request.kind === 'conversation-turn') {
        if (sendIntent.current) return;
        const receiptValue = receipt as { version: number };
        const conversationId = request.path.match(/brainstorm-conversations\/([^/]+)\/turns/)?.[1];
        if (conversationId) {
          const previous = readSavedSend(savedSendKey, epicVersion);
          void requestAssistance(savedSendForReceipt(previous, conversationId,
            receiptValue.version, epicVersion, pendingRouteKey));
        }
      }
    };
    const unregisterStart = registerCompletion('conversation-start', resumeSavedSend);
    const unregisterTurn = registerCompletion('conversation-turn', resumeSavedSend);
    const unregisterJob = registerCompletion('job-submit', () => {
      removeSavedSend(savedSendKey);
      removePendingRoute(pendingRouteKey);
      setSavedMessage(false);
      setSendError(null);
      sendLock.current = false;
      setAssistancePending(false);
    });
    return () => { unregisterStart(); unregisterTurn(); unregisterJob(); };
  }, [epicVersion, pendingRouteKey, registerCompletion, requestAssistance, savedSendKey]);

  const handleSendTurn = async (event: FormEvent) => {
    event.preventDefault();
    const submittedText = inputText.trim();
    if (!submittedText || mutationPending || assistancePending || sendLock.current || isUnavailable) return;
    sendLock.current = true;
    sendIntent.current = true;
    const frozenRoute = routeChoice;
    savePendingRoute(pendingRouteKey, frozenRoute);
    setAssistancePending(true);
    let continuationStarted = false;
    try {
      let conversationId: string;
      let version: number;
      if (!activeConversationId) {
        const receipt = await startConversation(submittedText);
        conversationId = receipt.conversation_id;
        version = receipt.version;
      } else if (activeThread) {
        const receipt = await appendTurn(activeConversationId, activeThread.conversation_version, submittedText);
        conversationId = activeConversationId;
        version = receipt.version;
      } else return;
      continuationStarted = true;
      const saved = { conversationId, version, expectedEpicVersion: epicVersion, route: frozenRoute };
      if (!isCurrentInstance()) saveSavedSend(savedSendKey, saved);
      else await requestAssistance(saved, true);
    } catch {
      // The mutation owner retains uncertain requests for an exact retry.
    } finally {
      sendIntent.current = false;
      if (!continuationStarted) {
        sendLock.current = false;
        setAssistancePending(false);
      }
    }
  };

  const recoverableSavedMessage = savedMessage;

  const handleAdopt = async () => {
    if (isUnavailable || !outcome?.proposal || !outcome.proposal_digest || outcome.state !== 'proposed') return;
    try {
      await adoptProposal(outcome.job_id, outcome.proposal_digest, outcome.job_version, epicVersion);
    } catch {
      // The mutation owner retains uncertain requests for an exact retry.
    }
  };

  const [retryOwnerOverride, setRetryOwnerOverride] = useState(false);
  const [retryOverrideNote, setRetryOverrideNote] = useState('');

  const handleJobControl = async (action: 'cancel' | 'retry') => {
    if (!outcome || mutationPending || isUnavailable) return;
    try {
      if (action === 'cancel') await cancelJob(outcome.job_id, outcome.job_version);
      else await retryJob(outcome.job_id, outcome.job_version, retryOwnerOverride || undefined, retryOverrideNote.trim() || undefined);
    } catch {
      // The mutation owner retains uncertain requests for an exact retry.
    }
  };

  return (
    <div className="authoring-workspace space-y-6">
      {mutations.hasPendingRetry && !mutations.shared && isAuthoringAction && (
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
              disabled={mutationPending || assistancePending}
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
              {turn.proposal && isBrainstormProposal(turn.proposal) && (
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
          <AuthoringJobOutcome outcome={outcome} />
          {outcome.state === 'failed' && outcome.failure === 'unavailable' && (
            <p role="status">The selected AI model could not start. Check the local client setup and this project&apos;s AI settings, then retry the assistant job.</p>
          )}
          {outcome.state === 'cancel_requested' && <p role="status">Cancellation requested. Waiting for the process to settle.</p>}
          <div className="flex flex-wrap gap-2">
            {['queued', 'running', 'quota_wait', 'capacity_wait', 'reconciling'].includes(outcome.state) && (
              <Button variant="secondary" disabled={mutationPending || isUnavailable} onClick={() => { void handleJobControl('cancel'); }}>Cancel assistant job</Button>
            )}
            {outcome.state === 'failed' && outcome.process_settled && (
              <div className="space-y-2">
                <Button variant="secondary" disabled={mutationPending || isUnavailable} onClick={() => { void handleJobControl('retry'); }}>Retry assistant job</Button>
                <div className="flex flex-col gap-2 pt-1 text-xs">
                  <label className="flex items-center gap-2 cursor-pointer font-medium text-[var(--muted)]">
                    <input
                      type="checkbox"
                      checked={retryOwnerOverride}
                      disabled={mutationPending || isUnavailable}
                      onChange={e => setRetryOwnerOverride(e.target.checked)}
                    />
                    <span>Owner override retry policy</span>
                  </label>
                  {retryOwnerOverride && (
                    <input
                      type="text"
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
      {adoptedRevisionId && <div className="flex items-center gap-3">
        <EvidenceDetails summary="Inspect adopted revision"><span>brief_revision_id: {adoptedRevisionId}</span></EvidenceDetails>
        {onReviewBrief && <Button variant="quiet" onClick={onReviewBrief}>Review or edit the adopted brief</Button>}
      </div>}

      <form onSubmit={handleSendTurn} className="space-y-3">
        <label htmlFor="turn-text" className="block text-sm font-medium">What are you trying to accomplish?</label>
        <p className="text-sm text-[var(--muted)]">A rough idea is enough. The assistant will ask questions and suggest a draft for you to review.</p>
        <BrainstormModelPicker projectId={projectId} choice={routeChoice} onChange={setRouteChoice} />
        <textarea
          id="turn-text"
          className="w-full px-3 py-2 border border-[var(--control-border)] rounded text-sm bg-[var(--surface)] focus:border-[var(--focus)] focus:outline-none min-h-[80px]"
          value={inputText}
          onChange={event => setInputText(event.target.value)}
          placeholder={outcome?.proposal && 'open_questions' in outcome.proposal && outcome.proposal.open_questions.length
            ? `Help answer this question: ${outcome.proposal.open_questions[0]}`
            : 'For example: I need a simpler way to track field repairs…'}
          maxLength={8000}
          required
          disabled={mutationPending || assistancePending || isUnavailable}
        />
        <Button type="submit" variant="primary" disabled={mutationPending || assistancePending || isUnavailable || !inputText.trim()}>
          {mutations.loading || assistancePending ? 'Sending…' : activeConversationId ? 'Send message' : 'Start conversation'}
        </Button>
      </form>

      {(sendError || recoverableSavedMessage) && <div role={sendError ? 'alert' : 'status'} className="space-y-2">
        <p>{sendError ?? 'Your message is saved. Start assistant help when you are ready.'}</p>
        <Button variant="secondary" disabled={mutationPending || assistancePending || loading || isUnavailable || !recoverableSavedMessage} onClick={() => recoverableSavedMessage && void requestAssistance(recoverableSavedMessage)}>
          {assistancePending ? 'Starting assistant…' : 'Retry assistant help'}
        </Button>
      </div>}

      {outcome && ['queued', 'running', 'quota_wait', 'capacity_wait', 'reconciling'].includes(outcome.state) && <p role="status">The assistant is working on your idea. You can add context while you wait.</p>}

    </div>
  );
}
