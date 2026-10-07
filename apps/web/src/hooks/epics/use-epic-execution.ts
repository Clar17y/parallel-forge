'use client';

import { useCallback, useEffect, useState } from 'react';
import { useApi } from '@/hooks/use-api';
import { useEpicMutations } from './use-epic-mutations';
import type {
  EpicChildProjection,
  EpicControlReceipt,
  EpicControlRequest,
  EpicDispatchRequest,
  EpicExecutionDispatch,
  EpicExecutionProjection,
  EpicExecutionSnapshot,
  EpicIntentProjection,
  EpicOwnerActionProjection,
  ExecutionStartRequest,
} from './types';

function executionIdFromCommandPath(path: string): string | null {
  return path.match(/\/executions\/([^/?]+)\/commands(?:[/?]|$)/)?.[1] ?? null;
}

function executionIdFromDispatchPath(path: string): string | null {
  return path.match(/\/executions\/([^/?]+)\/dispatch(?:[/?]|$)/)?.[1] ?? null;
}

export interface StartExecutionOptions {
  expectedEpicVersion: number;
  briefRevisionId?: string | null;
  briefDigest?: string | null;
  graphRevisionId?: string | null;
  graphDigest?: string | null;
  ownerOverride?: boolean;
  overrideNote?: string | null;
}

export function useEpicExecution(epicId: string, initialExecutionId?: string | null) {
  const [executionId, setExecutionIdState] = useState<string | null>(() => {
    if (initialExecutionId) return initialExecutionId;
    if (typeof window === 'undefined') return null;
    return new URL(window.location.href).searchParams.get('execution_id');
  });
  const [observedRouteId, setObservedRouteId] = useState(initialExecutionId);
  if (observedRouteId !== initialExecutionId) {
    setObservedRouteId(initialExecutionId);
    setExecutionIdState(initialExecutionId ?? null);
  }

  const setExecutionId = useCallback((id: string | null) => {
    setExecutionIdState(id);
    if (typeof window !== 'undefined') {
      const url = new URL(window.location.href);
      if (id) url.searchParams.set('execution_id', id);
      else url.searchParams.delete('execution_id');
      window.history.replaceState(window.history.state, '', url);
    }
  }, []);

  // Execution discovery
  const executionsPath = epicId ? `/epics/${epicId}/executions` : null;
  const executionsApi = useApi<EpicExecutionProjection[]>(executionsPath, {
    refreshIntervalMs: 5000,
    keepPreviousOnRefresh: true,
  });

  const discoveredId = (!initialExecutionId && !executionId && executionsApi.value && executionsApi.value.length > 0)
    ? (executionsApi.value[executionsApi.value.length - 1]?.execution?.execution_id ?? null)
    : null;
  const activeExecutionId = executionId ?? discoveredId;

  const executionPath =
    epicId && activeExecutionId ? `/epics/${epicId}/executions/${activeExecutionId}` : null;

  const executionApi = useApi<EpicExecutionProjection>(executionPath, {
    refreshIntervalMs: 3000,
    keepPreviousOnRefresh: true,
    keepPreviousOnError: true,
  });

  const mutations = useEpicMutations(epicId);
  const { execute, registerCompletion } = mutations;
  const refreshExecution = executionApi.refresh;
  const refreshExecutions = executionsApi.refresh;

  const refreshAll = useCallback(() => {
    refreshExecutions();
    refreshExecution();
  }, [refreshExecution, refreshExecutions]);

  const rememberExecution = useCallback((value: unknown) => {
    const id = (value as { execution_id?: string }).execution_id;
    if (!id) return;
    setExecutionId(id);
    refreshExecutions();
  }, [refreshExecutions, setExecutionId]);

  useEffect(() => registerCompletion('execution-start', rememberExecution), [registerCompletion, rememberExecution]);
  useEffect(() => registerCompletion('execution-command', (value, request) => {
    const requestedExecutionId = executionIdFromCommandPath(request.path);
    const receiptExecutionId = (value as { execution_id?: string }).execution_id;
    if (!requestedExecutionId || (receiptExecutionId && receiptExecutionId !== requestedExecutionId)) return;
    setExecutionId(requestedExecutionId);
    if (requestedExecutionId === executionId) refreshExecution();
    refreshExecutions();
  }), [executionId, refreshExecution, refreshExecutions, registerCompletion, setExecutionId]);
  useEffect(() => registerCompletion('execution-dispatch', (_value, request) => {
    const requestedExecutionId = executionIdFromDispatchPath(request.path);
    if (!requestedExecutionId) return;
    setExecutionId(requestedExecutionId);
    if (requestedExecutionId === executionId) refreshExecution();
  }), [executionId, refreshExecution, registerCompletion, setExecutionId]);

  const startExecution = useCallback(
    async (options: StartExecutionOptions | number) => {
      const opts = typeof options === 'number' ? { expectedEpicVersion: options } : options;
      const body: ExecutionStartRequest = {
        schema_version: 1,
        expected_epic_version: opts.expectedEpicVersion,
        owner_override: opts.ownerOverride ?? false,
        ...(opts.briefRevisionId ? { brief_revision_id: opts.briefRevisionId } : {}),
        ...(opts.briefDigest ? { brief_digest: opts.briefDigest } : {}),
        ...(opts.graphRevisionId ? { graph_revision_id: opts.graphRevisionId } : {}),
        ...(opts.graphDigest ? { graph_digest: opts.graphDigest } : {}),
        ...(opts.overrideNote !== undefined ? { override_note: opts.overrideNote } : {}),
      };
      const res = await execute<EpicExecutionSnapshot>(
        'POST',
        `/epics/${epicId}/executions`,
        body as unknown as Record<string, unknown>,
        { kind: 'execution-start' }
      );
      if (res && typeof res === 'object' && 'execution_id' in res) {
        setExecutionId(res.execution_id);
      }
      return res;
    },
    [epicId, execute, setExecutionId]
  );

  const sendCommand = useCallback(
    async (action: 'pause' | 'resume' | 'cancel', expectedExecutionVersion: number) => {
      if (!activeExecutionId) {
        throw new Error('Cannot send command without an active execution ID.');
      }
      const body: EpicControlRequest = {
        schema_version: 1,
        action,
        expected_execution_version: expectedExecutionVersion,
      };
      const res = await execute<EpicControlReceipt>(
        'POST',
        `/epics/${epicId}/executions/${activeExecutionId}/commands`,
        body as unknown as Record<string, unknown>,
        { kind: 'execution-command' }
      );
      return res;
    },
    [epicId, activeExecutionId, execute]
  );

  const setSequentialDispatch = useCallback(
    async (enabled: boolean, expectedDispatchVersion: number, profileId?: string | null, profileVersion?: number | null) => {
      if (!activeExecutionId) throw new Error('Cannot configure dispatch without an execution ID.');
      const body: EpicDispatchRequest = {
        schema_version: 1,
        expected_dispatch_version: expectedDispatchVersion,
        enabled,
        ...(profileId !== undefined ? { profile_id: profileId } : {}),
        ...(profileVersion !== undefined ? { profile_version: profileVersion } : {}),
      };
      return execute<EpicExecutionDispatch>(
        'PUT',
        `/epics/${epicId}/executions/${activeExecutionId}/dispatch`,
        body as unknown as Record<string, unknown>,
        { kind: 'execution-dispatch' },
      );
    },
    [activeExecutionId, epicId, execute],
  );

  const execution = executionApi.value ?? null;
  const isPendingDiscovery = !activeExecutionId;

  // Exact generated EpicExecutionProjection fields - no Record casts, no guessed fallbacks
  const controlState: string | null = execution?.control_state ?? null;
  const state: string | null = controlState;
  const controlVersion: number | null = execution?.control_version ?? null;
  const blockerCode: string | null = execution?.blocker_code ?? null;
  const children: EpicChildProjection[] = execution?.children ?? [];
  const intents: EpicIntentProjection[] = execution?.intents ?? [];
  const ownerActions: EpicOwnerActionProjection[] = execution?.owner_actions ?? [];
  const snapshot: EpicExecutionSnapshot | null = execution?.execution ?? null;

  return {
    executionId: activeExecutionId,
    setExecutionId,
    executions: executionsApi.value ?? [],
    executionsLoading: executionsApi.loading,
    executionsFailed: executionsApi.failed,
    execution,
    snapshot,
    loading: executionApi.loading || (!executionId && executionsApi.loading),
    failed: executionApi.failed,
    refresh: refreshAll,
    isPendingDiscovery,
    controlState,
    controlVersion,
    state,
    children,
    intents,
    ownerActions,
    blockerCode,
    startExecution,
    sendCommand,
    setSequentialDispatch,
    mutations,
  };
}
