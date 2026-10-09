'use client';

import { useCallback, useEffect, useState, useSyncExternalStore } from 'react';
import { useApi } from '@/hooks/use-api';
import { useEpicMutations } from './use-epic-mutations';
import type {
  AuthoringReceipt,
  AuthoringOutcome,
  BrainstormThread,
  BrainstormTurn,
} from './types';
import type { BrainstormRoute } from '@/components/epics/brainstorm-model-choice';

function readSelection() {
  if (typeof window === 'undefined') return { conversationId: null, jobId: null };
  const params = new URL(window.location.href).searchParams;
  return { conversationId: params.get('conversation_id'), jobId: params.get('job_id') };
}

const subscribeHydration = () => () => undefined;
const clientHydrated = () => true;
const serverHydrated = () => false;

function writeSelection(conversationId: string | null, jobId: string | null) {
  if (typeof window === 'undefined') return;
  const url = new URL(window.location.href);
  if (conversationId) url.searchParams.set('conversation_id', conversationId);
  else url.searchParams.delete('conversation_id');
  if (jobId) url.searchParams.set('job_id', jobId);
  else url.searchParams.delete('job_id');
  window.history.replaceState(window.history.state, '', url);
}

function conversationIdFromPath(path: string): string | null {
  return path.match(/\/brainstorm-conversations\/([^/?]+)\/(?:turns|jobs)(?:[/?]|$)/)?.[1] ?? null;
}

function jobIdFromPath(path: string): string | null {
  return path.match(/\/brainstorm-jobs\/([^/?]+)/)?.[1] ?? null;
}

export function useEpicBrainstorm(epicId: string, projectId: string) {
  const threadsPath = epicId && projectId
    ? `/epics/${epicId}/brainstorm-conversations?project_id=${encodeURIComponent(projectId)}`
    : null;
  const threadsApi = useApi<BrainstormThread[]>(threadsPath, { refreshIntervalMs: 5000, keepPreviousOnRefresh: true });
  const refreshThreads = threadsApi.refresh;
  const [activeConversationState, setActiveConversationState] = useState<string | null>(null);
  const [selectedJobState, setSelectedJobState] = useState<{ id: string; conversationId: string | null } | null>(null);
  const [adoptedRevisionId, setAdoptedRevisionId] = useState<string | null>(null);

  // Browser URL selection becomes available after the server markup hydrates.
  const hydrated = useSyncExternalStore(subscribeHydration, clientHydrated, serverHydrated);
  const urlSelection = hydrated ? readSelection() : { conversationId: null, jobId: null };
  const urlJobThread = urlSelection.jobId
    ? threadsApi.value?.find(thread => thread.job_ids.includes(urlSelection.jobId!))
    : undefined;
  const selectedConversationId = urlSelection.conversationId
    ? (urlSelection.jobId && urlJobThread
      ? urlJobThread.conversation_id
      : urlSelection.conversationId)
    : (urlSelection.jobId
      ? urlJobThread?.conversation_id ?? null
      : activeConversationState ?? threadsApi.value?.[0]?.conversation_id ?? null);
  const selectedJobId = urlSelection.jobId
    ? (urlJobThread?.conversation_id === selectedConversationId
      || (selectedJobState?.id === urlSelection.jobId && selectedJobState.conversationId === selectedConversationId)
      ? urlSelection.jobId
      : null)
    : selectedJobState?.conversationId === selectedConversationId
      ? selectedJobState.id
      : null;

  const turnsPath = epicId && projectId && selectedConversationId
    ? `/epics/${epicId}/brainstorm-conversations/${selectedConversationId}/turns?project_id=${encodeURIComponent(projectId)}`
    : null;
  const turnsApi = useApi<BrainstormTurn[]>(turnsPath, { refreshIntervalMs: 3000, keepPreviousOnRefresh: true });
  const refreshTurns = turnsApi.refresh;
  const activeThread = threadsApi.value?.find(thread => thread.conversation_id === selectedConversationId);
  const jobId = selectedJobId ?? (!urlSelection.jobId ? activeThread?.job_ids.at(-1) ?? null : null);
  const outcomePath = jobId
    ? `/epics/${epicId}/brainstorm-jobs/${jobId}?project_id=${encodeURIComponent(projectId)}`
    : null;
  const outcomeApi = useApi<AuthoringOutcome>(outcomePath, {
    refreshIntervalMs: 3000,
    keepPreviousOnRefresh: true,
    keepPreviousOnError: true,
  });
  const refreshOutcome = outcomeApi.refresh;

  const mutations = useEpicMutations(epicId);
  const { execute, registerCompletion, loading: mutationLoading, hasPendingRetry } = mutations;

  const selectConversation = useCallback((conversationId: string) => {
    if (mutationLoading || hasPendingRetry) return;
    setActiveConversationState(conversationId);
    setSelectedJobState(null);
    setAdoptedRevisionId(null);
    writeSelection(conversationId, null);
  }, [mutationLoading, hasPendingRetry]);

  const selectCompletedJob = useCallback((jobIdValue: string, conversationId: string | null) => {
    setActiveConversationState(conversationId);
    setSelectedJobState({ id: jobIdValue, conversationId });
    setAdoptedRevisionId(null);
    writeSelection(conversationId, jobIdValue);
  }, []);

  const setSelectedJobId = useCallback((jobIdValue: string | null) => {
    if (mutationLoading || hasPendingRetry) return;
    if (!jobIdValue) {
      setSelectedJobState(null);
      writeSelection(selectedConversationId, null);
      return;
    }
    if (!selectedConversationId) return;
    setSelectedJobState({ id: jobIdValue, conversationId: selectedConversationId });
    setAdoptedRevisionId(null);
    writeSelection(selectedConversationId, jobIdValue);
  }, [hasPendingRetry, mutationLoading, selectedConversationId]);

  const resolveJobConversation = useCallback((jobIdValue: string) => (
    threadsApi.value?.find(thread => thread.job_ids.includes(jobIdValue))?.conversation_id ?? null
  ), [threadsApi.value]);

  const isUnavailable = threadsApi.failed
    || (selectedConversationId !== null && turnsApi.failed)
    || (!!jobId && outcomeApi.failed);

  const refresh = useCallback(() => {
    refreshThreads();
    refreshTurns();
    refreshOutcome();
  }, [refreshOutcome, refreshThreads, refreshTurns]);

  useEffect(() => {
    if (urlSelection.jobId && urlJobThread && urlSelection.conversationId !== urlJobThread.conversation_id) {
      writeSelection(urlJobThread.conversation_id, urlSelection.jobId);
    }
  }, [urlJobThread, urlSelection.conversationId, urlSelection.jobId]);

  useEffect(() => {
    const onJobSettled = (value: unknown, request: { path: string }) => {
      const receipt = value as AuthoringReceipt;
      const requestJobId = jobIdFromPath(request.path) ?? receipt.job_id;
      const conversationId = resolveJobConversation(requestJobId);
      selectCompletedJob(receipt.job_id, conversationId);
      refreshOutcome();
      refreshThreads();
    };

    const registrations = [
      registerCompletion('conversation-start', value => {
        const conversationId = (value as { conversation_id: string }).conversation_id;
        setActiveConversationState(conversationId);
        setSelectedJobState(null);
        writeSelection(conversationId, null);
        refreshThreads();
      }),
      registerCompletion('conversation-turn', (_value, request) => {
        const conversationId = conversationIdFromPath(request.path);
        if (conversationId) {
          setActiveConversationState(conversationId);
          writeSelection(conversationId, null);
        }
        refreshTurns();
        refreshThreads();
      }),
      registerCompletion('job-submit', (value, request) => {
        const conversationId = conversationIdFromPath(request.path);
        const receipt = value as AuthoringReceipt;
        selectCompletedJob(receipt.job_id, conversationId);
        refreshThreads();
      }),
      registerCompletion('job-cancel', onJobSettled),
      registerCompletion('job-retry', onJobSettled),
      registerCompletion('proposal-adopt', (value, request) => {
        const requestJobId = jobIdFromPath(request.path);
        const conversationId = requestJobId ? resolveJobConversation(requestJobId) : null;
        if (requestJobId) selectCompletedJob(requestJobId, conversationId);
        setAdoptedRevisionId((value as { brief_revision_id: string }).brief_revision_id);
        refreshOutcome();
        refreshThreads();
      }),
    ];
    return () => registrations.forEach(unregister => unregister());
  }, [
    registerCompletion,
    refreshOutcome,
    refreshThreads,
    refreshTurns,
    resolveJobConversation,
    selectCompletedJob,
  ]);

  const startConversation = useCallback((text: string) => execute<{ conversation_id: string; version: number }>(
    'POST', `/epics/${epicId}/brainstorm-conversations`,
    { schema_version: 1, project_id: projectId, text }, { kind: 'conversation-start' },
  ), [epicId, execute, projectId]);

  const appendTurn = useCallback((conversationId: string, expectedConvVersion: number, text: string, idempotencyKey?: string) => execute<{ version: number }>(
    'POST', `/epics/${epicId}/brainstorm-conversations/${conversationId}/turns`,
    {
      schema_version: 1,
      project_id: projectId,
      expected_conversation_version: expectedConvVersion,
      text,
      pending: false,
    }, { kind: 'conversation-turn', idempotencyKey },
  ), [epicId, execute, projectId]);

  const submitJob = useCallback((conversationId: string, promptTurnId: string, expectedEpicVersion: number, expectedConvVersion: number, idempotencyKey?: string, requestedRoute?: BrainstormRoute) => execute<AuthoringReceipt>(
    'POST', `/epics/${epicId}/brainstorm-conversations/${conversationId}/jobs`,
    {
      schema_version: 1,
      project_id: projectId,
      prompt_turn_id: promptTurnId,
      expected_epic_version: expectedEpicVersion,
      expected_conversation_version: expectedConvVersion,
      ...(requestedRoute ? { requested_route: requestedRoute } : {}),
    }, { kind: 'job-submit', idempotencyKey },
  ), [epicId, execute, projectId]);

  const adoptProposal = useCallback((jobIdValue: string, proposalDigest: string, expectedJobVersion: number, expectedEpicVersion: number) => execute<{ brief_revision_id: string }>(
    'POST', `/epics/${epicId}/brainstorm-jobs/${jobIdValue}/adopt`,
    {
      schema_version: 1,
      project_id: projectId,
      expected_job_version: expectedJobVersion,
      expected_epic_version: expectedEpicVersion,
      proposal_digest: proposalDigest,
    }, { kind: 'proposal-adopt' },
  ), [epicId, execute, projectId]);

  const cancelJob = useCallback((jobIdValue: string, expectedJobVersion: number) => execute<AuthoringReceipt>(
    'POST', `/epics/${epicId}/brainstorm-jobs/${jobIdValue}/cancel`,
    { schema_version: 1, project_id: projectId, expected_job_version: expectedJobVersion }, { kind: 'job-cancel' },
  ), [epicId, execute, projectId]);

  const retryJob = useCallback((jobIdValue: string, expectedJobVersion: number, ownerOverride?: boolean, overrideNote?: string | null) => execute<AuthoringReceipt>(
    'POST', `/epics/${epicId}/brainstorm-jobs/${jobIdValue}/retry`,
    {
      schema_version: 1,
      project_id: projectId,
      expected_job_version: expectedJobVersion,
      ...(ownerOverride !== undefined ? { owner_override: ownerOverride } : {}),
      ...(overrideNote ? { override_note: overrideNote } : {}),
    }, { kind: 'job-retry' },
  ), [epicId, execute, projectId]);

  return {
    threads: threadsApi.value ?? [],
    activeConversationId: selectedConversationId,
    setActiveConversationId: selectConversation,
    turns: turnsApi.value ?? [],
    outcome: !outcomeApi.failed && outcomeApi.value?.job_id === jobId ? outcomeApi.value : null,
    selectedJobId: jobId,
    setSelectedJobId,
    jobs: [...new Set([...(activeThread?.job_ids ?? []), ...(selectedJobId ? [selectedJobId] : [])])],
    adoptedRevisionId,
    loading: threadsApi.loading || turnsApi.loading || outcomeApi.loading,
    isUnavailable,
    refresh,
    startConversation,
    appendTurn,
    submitJob,
    adoptProposal,
    cancelJob,
    retryJob,
    mutations,
  };
}
