'use client';

import { useCallback, useEffect, useState } from 'react';
import { useApi } from '@/hooks/use-api';
import { useEpicMutations } from './use-epic-mutations';
import type { ActiveChild, ExecutionProgress, ExecutionState } from './types';

function executionIdFromCommandPath(path: string): string | null {
  return path.match(/\/executions\/([^/?]+)\/commands(?:[/?]|$)/)?.[1] ?? null;
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

  const executionPath =
    epicId && executionId ? `/epics/${epicId}/executions/${executionId}` : null;

  const executionApi = useApi<ExecutionProgress>(executionPath, {
    refreshIntervalMs: 3000,
    keepPreviousOnRefresh: true,
    keepPreviousOnError: true,
  });

  const mutations = useEpicMutations(epicId);
  const { execute, registerCompletion } = mutations;
  const refreshExecution = executionApi.refresh;

  const rememberExecution = useCallback((value: unknown) => {
    const id = (value as { execution_id?: string }).execution_id;
    if (!id) return;
    setExecutionId(id);
  }, [setExecutionId]);

  useEffect(() => registerCompletion('execution-start', rememberExecution), [registerCompletion, rememberExecution]);
  useEffect(() => registerCompletion('execution-command', (value, request) => {
    const requestedExecutionId = executionIdFromCommandPath(request.path);
    const receiptExecutionId = (value as { execution_id?: string }).execution_id;
    if (!requestedExecutionId || (receiptExecutionId && receiptExecutionId !== requestedExecutionId)) return;
    setExecutionId(requestedExecutionId);
    // A changed ID triggers useApi for that frozen projection. Refresh directly
    // only when it is already the selected projection.
    if (requestedExecutionId === executionId) refreshExecution();
  }), [executionId, refreshExecution, registerCompletion, setExecutionId]);

  const startExecution = useCallback(
    async (expectedEpicVersion: number) => {
      const res = await execute<{
        schema_version: 1;
        execution_id: string;
        execution_version: number;
        state: ExecutionState;
      }>('POST', `/epics/${epicId}/executions`, {
        schema_version: 1,
        expected_epic_version: expectedEpicVersion,
      }, { kind: 'execution-start' });
      return res;
    },
    [epicId, execute]
  );

  const sendCommand = useCallback(
    async (action: 'pause' | 'resume' | 'cancel', expectedExecutionVersion: number) => {
      if (!executionId) {
        throw new Error('Cannot send command without an active execution ID.');
      }
      const res = await execute<{
        schema_version: 1;
        action: string;
        execution_version: number;
        state: ExecutionState;
      }>('POST', `/epics/${epicId}/executions/${executionId}/commands`, {
        schema_version: 1,
        action,
        expected_execution_version: expectedExecutionVersion,
      }, { kind: 'execution-command' });
      return res;
    },
    [epicId, executionId, execute]
  );

  const execution = executionApi.value ?? null;
  const isPendingDiscovery = !executionId;
  const state: ExecutionState | null = execution?.state ?? null;
  const childRuns: ActiveChild[] = execution?.active_child ? [execution.active_child] : [];

  return {
    executionId,
    setExecutionId,
    execution,
    loading: executionApi.loading,
    failed: executionApi.failed,
    refresh: executionApi.refresh,
    isPendingDiscovery,
    state,
    childRuns,
    startExecution,
    sendCommand,
    mutations,
  };
}
