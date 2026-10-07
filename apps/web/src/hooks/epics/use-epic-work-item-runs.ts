'use client';

import { useCallback, useEffect } from 'react';
import { useApi } from '@/hooks/use-api';
import { useEpicMutations } from './use-epic-mutations';
import type { EpicAttemptResponse, EpicLaunchRequest } from './types';

export function useEpicWorkItemRuns(epicId: string) {
  const path = epicId ? `/epics/${epicId}/work-item-runs` : null;
  const attemptsApi = useApi<EpicAttemptResponse[]>(path, { keepPreviousOnRefresh: true });
  const mutations = useEpicMutations(epicId);
  const { execute, registerCompletion } = mutations;
  const refresh = attemptsApi.refresh;

  useEffect(() => registerCompletion('work-item-launch', refresh), [registerCompletion, refresh]);

  const launch = useCallback((request: EpicLaunchRequest) => execute<EpicAttemptResponse>(
    'POST',
    `/epics/${epicId}/work-item-runs`,
    request as unknown as Record<string, unknown>,
    { kind: 'work-item-launch' },
  ), [epicId, execute]);

  return { attempts: attemptsApi.value ?? [], loading: attemptsApi.loading, failed: attemptsApi.failed, refresh, launch, mutations };
}
