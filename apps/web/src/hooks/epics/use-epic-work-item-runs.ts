'use client';

import { useCallback } from 'react';
import { useEpicMutations } from './use-epic-mutations';
import type { EpicAttemptResponse, EpicLaunchRequest } from './types';

export function useEpicWorkItemRuns(epicId: string) {
  const mutations = useEpicMutations(epicId);
  const { execute } = mutations;

  const launch = useCallback((request: EpicLaunchRequest) => execute<EpicAttemptResponse>(
    'POST',
    `/epics/${epicId}/work-item-runs`,
    request as unknown as Record<string, unknown>,
    { kind: 'work-item-launch' },
  ), [epicId, execute]);

  return { launch, mutations };
}
