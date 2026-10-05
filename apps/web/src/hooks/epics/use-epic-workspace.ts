'use client';

import { createContext, createElement, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from 'react';
import { useApi } from '@/hooks/use-api';
import { normalizeEpicId, useEpicMutations } from './use-epic-mutations';
import type {
  AcceptedBriefResponse,
  AcceptedGraphResponse,
  BriefContent,
  BriefRequirement,
  BriefRevisionResponse,
  EpicResponse,
  GraphRevisionResponse,
  ItemInput,
} from './types';

const EMPTY_BRIEF: BriefContent = {
  schema_version: 1,
  problem: '',
  outcomes: [],
  scope: [],
  exclusions: [],
  requirements: [],
  decisions: [],
  assumptions: [],
  open_questions: [],
};

function useLocalEpicWorkspace(epicId: string, enabled = true) {
  const epicPath = enabled && epicId ? `/epics/${epicId}` : null;
  const epicApi = useApi<EpicResponse>(epicPath, {
    refreshIntervalMs: 5000,
    keepPreviousOnRefresh: true,
    keepPreviousOnError: true,
    refreshStorageKey: `epic_refresh_${normalizeEpicId(epicId) ?? epicId}`,
  });

  const [receiptEpic, setReceiptEpic] = useState<EpicResponse | null>(null);
  const [savedBriefRevision, setSavedBriefRevision] = useState<BriefRevisionResponse | null>(null);
  const [savedGraphRevision, setSavedGraphRevision] = useState<GraphRevisionResponse | null>(null);
  const epic = receiptEpic && normalizeEpicId(receiptEpic.epic_id) === normalizeEpicId(epicId) && receiptEpic.version > (epicApi.value?.version ?? 0)
    ? receiptEpic : epicApi.value;
  const epicRef = useRef(epic);
  useEffect(() => { epicRef.current = epic; }, [epic]);
  const acceptReceipt = useCallback((receipt: EpicResponse | BriefRevisionResponse | GraphRevisionResponse) => {
    if ('version' in receipt) {
      setReceiptEpic(previous => !previous || receipt.version >= previous.version ? receipt : previous);
    } else {
      const current = epicRef.current;
      if (current && receipt.epic_version >= current.version) {
        setReceiptEpic({
          ...current, version: receipt.epic_version,
          ...('content' in receipt ? { draft: receipt.content } : {}),
        });
      }
    }
  }, []);

  const briefRevisionsApi = useApi<BriefRevisionResponse[]>(
    enabled && epicId ? `/epics/${epicId}/brief-revisions` : null,
    { keepPreviousOnRefresh: true, keepPreviousOnError: true }
  );

  const acceptedBriefApi = useApi<AcceptedBriefResponse>(
    enabled && epic?.accepted_brief_revision_id ? `/epics/${epicId}/accepted-brief` : null,
    { keepPreviousOnRefresh: true }
  );

  const graphRevisionsApi = useApi<GraphRevisionResponse[]>(
    enabled && epicId ? `/epics/${epicId}/graph-revisions` : null,
    { keepPreviousOnRefresh: true, keepPreviousOnError: true }
  );

  const acceptedGraphApi = useApi<AcceptedGraphResponse>(
    enabled && epic?.accepted_graph_revision_id ? `/epics/${epicId}/accepted-graph` : null,
    { keepPreviousOnRefresh: true }
  );

  const mutations = useEpicMutations(epicId);
  const { execute, registerCompletion } = mutations;
  const refreshEpic = epicApi.refresh;
  const refreshBriefRevisions = briefRevisionsApi.refresh;
  const refreshAcceptedBrief = acceptedBriefApi.refresh;
  const refreshGraphRevisions = graphRevisionsApi.refresh;
  const refreshAcceptedGraph = acceptedGraphApi.refresh;
  const lastProjectionVersion = useRef<number | null>(null);
  useEffect(() => {
    const version = epic?.version;
    if (!enabled || version === undefined) return;
    if (lastProjectionVersion.current !== null && lastProjectionVersion.current !== version) {
      refreshBriefRevisions();
      refreshAcceptedBrief();
      refreshGraphRevisions();
      refreshAcceptedGraph();
    }
    lastProjectionVersion.current = version;
  }, [enabled, epic?.version, refreshBriefRevisions, refreshAcceptedBrief, refreshGraphRevisions, refreshAcceptedGraph]);

  const refreshProjections = useCallback(() => {
    refreshEpic();
    refreshBriefRevisions();
    refreshAcceptedBrief();
    refreshGraphRevisions();
    refreshAcceptedGraph();
  }, [refreshEpic, refreshBriefRevisions, refreshAcceptedBrief, refreshGraphRevisions, refreshAcceptedGraph]);

  // User override state for local uncommitted edits
  const [userTitle, setUserTitle] = useState<string | null>(null);
  const [userContent, setUserContent] = useState<BriefContent | null>(null);
  const [userBaseVersion, setUserBaseVersion] = useState<number | null>(null);
  const localDraftRef = useRef({ title: userTitle, content: userContent });
  useEffect(() => { localDraftRef.current = { title: userTitle, content: userContent }; }, [userTitle, userContent]);

  const serverTitle = epic?.title ?? '';
  const serverContent = epic?.draft ?? EMPTY_BRIEF;
  const serverVersion = epic?.version ?? 1;

  const isDirty = userTitle !== null || userContent !== null;
  const draftTitle = userTitle !== null ? userTitle : serverTitle;
  const draftContent = userContent !== null ? userContent : serverContent;
  const baseVersion = isDirty && userBaseVersion !== null ? userBaseVersion : serverVersion;
  const hasServerConflict = isDirty && serverVersion > baseVersion;

  useEffect(() => {
    if (!enabled) return;
    const registrations = [
      registerCompletion('brief-draft', (value, request) => {
        const current = localDraftRef.current;
        const titleStillSubmitted = current.title === null || current.title === request.body.title;
        const contentStillSubmitted = current.content === null || JSON.stringify(current.content) === JSON.stringify(request.body.draft);
        acceptReceipt(value as EpicResponse);
        setUserTitle(previous => previous === request.body.title ? null : previous);
        setUserContent(previous => previous && JSON.stringify(previous) === JSON.stringify(request.body.draft) ? null : previous);
        const nextVersion = (value as EpicResponse).version;
        setUserBaseVersion(titleStillSubmitted && contentStillSubmitted ? null : nextVersion);
        refreshEpic();
      }),
      registerCompletion('brief-revision', (value, request) => {
        const current = localDraftRef.current;
        const contentStillSubmitted = current.content === null || JSON.stringify(current.content) === JSON.stringify(request.body.content);
        acceptReceipt(value as BriefRevisionResponse);
        setSavedBriefRevision(value as BriefRevisionResponse);
        setUserContent(previous => previous && JSON.stringify(previous) === JSON.stringify(request.body.content) ? null : previous);
        setUserBaseVersion(contentStillSubmitted && current.title === null ? null : (value as BriefRevisionResponse).epic_version);
        refreshEpic(); refreshBriefRevisions();
      }),
      registerCompletion('brief-adoption', value => { acceptReceipt(value as EpicResponse); refreshProjections(); }),
      registerCompletion('graph-revision', value => { acceptReceipt(value as GraphRevisionResponse); setSavedGraphRevision(value as GraphRevisionResponse); refreshEpic(); refreshGraphRevisions(); }),
      registerCompletion('graph-adoption', value => { acceptReceipt(value as EpicResponse); refreshProjections(); }),
    ];
    return () => registrations.forEach(unregister => unregister());
  }, [enabled, acceptReceipt, registerCompletion, refreshEpic, refreshBriefRevisions, refreshGraphRevisions, refreshProjections]);

  // Draft update helpers
  const updateDraftTitle = useCallback(
    (title: string) => {
      setUserTitle(title);
      if (!isDirty) setUserBaseVersion(epic?.version ?? 1);
    },
    [epic?.version, isDirty]
  );

  const updateDraftProblem = useCallback(
    (problem: string) => {
      const current = userContent !== null ? userContent : (epic?.draft ?? EMPTY_BRIEF);
      setUserContent({ ...current, problem });
      if (!isDirty) setUserBaseVersion(epic?.version ?? 1);
    },
    [epic?.draft, epic?.version, isDirty, userContent]
  );

  const updateDraftContent = useCallback(
    (updater: (prev: BriefContent) => BriefContent) => {
      const current = userContent !== null ? userContent : (epic?.draft ?? EMPTY_BRIEF);
      setUserContent(updater(current));
      if (!isDirty) setUserBaseVersion(epic?.version ?? 1);
    },
    [epic?.draft, epic?.version, isDirty, userContent]
  );

  const addRequirement = useCallback(
    (text = '', criteria: string[] = ['']) => {
      const current = userContent !== null ? userContent : (epic?.draft ?? EMPTY_BRIEF);
      const newReq: BriefRequirement = {
        requirement_id: crypto.randomUUID(),
        text,
        acceptance_criteria: criteria,
      };
      setUserContent({
        ...current,
        requirements: [...(current.requirements ?? []), newReq],
      });
      if (!isDirty) setUserBaseVersion(epic?.version ?? 1);
      return newReq.requirement_id;
    },
    [epic?.draft, epic?.version, isDirty, userContent]
  );

  const updateRequirement = useCallback(
    (id: string, text: string, criteria?: string[]) => {
      const current = userContent !== null ? userContent : (epic?.draft ?? EMPTY_BRIEF);
      setUserContent({
        ...current,
        requirements: (current.requirements ?? []).map(req => {
          if (req.requirement_id !== id) return req;
          return {
            ...req,
            text,
            acceptance_criteria: criteria !== undefined ? criteria : req.acceptance_criteria,
          };
        }),
      });
      if (!isDirty) setUserBaseVersion(epic?.version ?? 1);
    },
    [epic?.draft, epic?.version, isDirty, userContent]
  );

  const removeRequirement = useCallback(
    (id: string) => {
      const current = userContent !== null ? userContent : (epic?.draft ?? EMPTY_BRIEF);
      setUserContent({
        ...current,
        requirements: (current.requirements ?? []).filter(req => req.requirement_id !== id),
      });
      if (!isDirty) setUserBaseVersion(epic?.version ?? 1);
    },
    [epic?.draft, epic?.version, isDirty, userContent]
  );

  const revertLocalDraft = useCallback(() => {
    setUserTitle(null);
    setUserContent(null);
    setUserBaseVersion(null);
  }, []);

  const acknowledgeConflict = useCallback(() => {
    if (epic) {
      setUserBaseVersion(epic.version);
    }
  }, [epic]);

  // Server Mutations
  const saveDraft = useCallback(async () => {
    const updated = await execute<EpicResponse>('PATCH', `/epics/${epicId}`, {
      schema_version: 1,
      expected_epic_version: baseVersion,
      title: draftTitle,
      draft: draftContent,
    }, { kind: 'brief-draft' });
    return updated;
  }, [baseVersion, draftContent, draftTitle, epicId, execute]);

  const saveBriefRevision = useCallback(async () => {
    if (userTitle !== null) throw new Error('Save the changed title as a draft before saving a revision.');
    const receipt = await execute<BriefRevisionResponse>(
      'POST',
      `/epics/${epicId}/brief-revisions`,
      {
        schema_version: 1,
        expected_epic_version: baseVersion,
        content: draftContent,
      }, { kind: 'brief-revision' }
    );
    return receipt;
  }, [baseVersion, draftContent, epicId, execute, userTitle]);

  const adoptBrief = useCallback(
    async (briefRevisionId: string, briefDigest: string) => {
      const updated = await execute<EpicResponse>(
        'POST',
        `/epics/${epicId}/brief-adoptions`,
        {
          schema_version: 1,
          expected_epic_version: serverVersion,
          brief_revision_id: briefRevisionId,
          brief_digest: briefDigest,
        }, { kind: 'brief-adoption' }
      );
      return updated;
    },
    [serverVersion, epicId, execute]
  );

  const saveGraphRevision = useCallback(
    async (briefRevisionId: string, briefDigest: string, items: ItemInput[], graphBaseVersion = serverVersion) => {
      const receipt = await execute<GraphRevisionResponse>(
        'POST',
        `/epics/${epicId}/graph-revisions`,
        {
          schema_version: 1,
          expected_epic_version: graphBaseVersion,
          brief_revision_id: briefRevisionId,
          brief_digest: briefDigest,
          items,
        }, { kind: 'graph-revision' }
      );
      return receipt;
    },
    [serverVersion, epicId, execute]
  );

  const adoptGraph = useCallback(
    async (graphRevisionId: string, graphDigest: string) => {
      const updated = await execute<EpicResponse>(
        'POST',
        `/epics/${epicId}/graph-adoptions`,
        {
          schema_version: 1,
          expected_epic_version: serverVersion,
          graph_revision_id: graphRevisionId,
          graph_digest: graphDigest,
        }, { kind: 'graph-adoption' }
      );
      return updated;
    },
    [serverVersion, epicId, execute]
  );

  return {
    epic: epic,
    loading: epicApi.loading,
    failed: epicApi.failed,
    refreshEpic: epicApi.refresh,
    refreshProjections,
    serverVersion: epic?.version ?? baseVersion,
    baseVersion,

    // Brief revisions & accepted brief
    briefRevisions: [...(briefRevisionsApi.value ?? []), ...(savedBriefRevision && normalizeEpicId(savedBriefRevision.epic_id) === normalizeEpicId(epicId) && !briefRevisionsApi.value?.some(revision => revision.brief_revision_id === savedBriefRevision.brief_revision_id) ? [savedBriefRevision] : [])].sort((a, b) => a.revision_number - b.revision_number),
    loadingBriefRevisions: briefRevisionsApi.loading,
    failedBriefRevisions: briefRevisionsApi.failed,
    refreshBriefRevisions: briefRevisionsApi.refresh,
    acceptedBrief: epic?.accepted_brief_revision_id && acceptedBriefApi.value?.brief_revision_id === epic.accepted_brief_revision_id && acceptedBriefApi.value?.brief_digest === epic.accepted_brief_digest ? acceptedBriefApi.value : undefined,
    loadingAcceptedBrief: acceptedBriefApi.loading || acceptedBriefApi.refreshing,
    failedAcceptedBrief: acceptedBriefApi.failed,

    // Graph revisions & accepted graph
    graphRevisions: [...(graphRevisionsApi.value ?? []), ...(savedGraphRevision && normalizeEpicId(savedGraphRevision.epic_id) === normalizeEpicId(epicId) && !graphRevisionsApi.value?.some(revision => revision.graph_revision_id === savedGraphRevision.graph_revision_id) ? [savedGraphRevision] : [])].sort((a, b) => a.revision_number - b.revision_number),
    loadingGraphRevisions: graphRevisionsApi.loading,
    failedGraphRevisions: graphRevisionsApi.failed,
    refreshGraphRevisions: graphRevisionsApi.refresh,
    acceptedGraph: epic?.accepted_graph_revision_id && acceptedGraphApi.value?.graph_revision_id === epic.accepted_graph_revision_id && acceptedGraphApi.value?.graph_digest === epic.accepted_graph_digest ? acceptedGraphApi.value : undefined,
    loadingAcceptedGraph: acceptedGraphApi.loading || acceptedGraphApi.refreshing,
    failedAcceptedGraph: acceptedGraphApi.failed,

    // Local draft & dirty state
    draftTitle,
    updateDraftTitle,
    draftContent,
    updateDraftProblem,
    updateDraftContent,
    addRequirement,
    updateRequirement,
    removeRequirement,
    isDirty,
    hasServerConflict,
    revertLocalDraft,
    acknowledgeConflict,

    // Mutation actions & state
    saveDraft,
    saveBriefRevision,
    adoptBrief,
    saveGraphRevision,
    adoptGraph,
    mutations,
  };
}

type EpicWorkspace = ReturnType<typeof useLocalEpicWorkspace>;
const EpicWorkspaceContext = createContext<{ epicId: string; workspace: EpicWorkspace } | null>(null);

export function EpicWorkspaceProvider({ epicId, children }: { epicId: string; children: ReactNode }) {
  const workspace = useLocalEpicWorkspace(epicId);
  return createElement(EpicWorkspaceContext.Provider, { value: { epicId, workspace } }, children);
}

export function useEpicWorkspace(epicId: string): EpicWorkspace {
  const shared = useContext(EpicWorkspaceContext);
  const matching = shared !== null && normalizeEpicId(shared.epicId) === normalizeEpicId(epicId);
  const local = useLocalEpicWorkspace(epicId, !matching);
  return matching ? shared.workspace : local;
}
