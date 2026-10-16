'use client';

import { createContext, useContext, useState, useCallback, type ReactNode } from 'react';
import { useApi } from '@/hooks/use-api';
import type { components } from '@/lib/api/schema';

export type SubscriptionTaskPage = components['schemas']['SubscriptionTaskPage'];

export type SubscriptionTasksState = {
  runId: string;
  offset: number;
  pageSize: number;
  selected: string | null;
  setSelected: (id: string | null) => void;
  movePage: (nextOffset: number) => void;
  tasks: ReturnType<typeof useApi<SubscriptionTaskPage>>;
  refresh: () => number;
};

export const SubscriptionTasksContext = createContext<SubscriptionTasksState | null>(null);

export function useSubscriptionTasksContext(): SubscriptionTasksState | null {
  return useContext(SubscriptionTasksContext);
}

export type UseSubscriptionTasksOptions = {
  pageSize?: number;
  refreshIntervalMs?: number;
};

export function useSubscriptionTasks(
  runId: string,
  {
    pageSize = 25,
    refreshIntervalMs = 5000,
  }: UseSubscriptionTasksOptions = {},
): SubscriptionTasksState {
  const [offset, setOffset] = useState(0);
  const [selected, setSelected] = useState<string | null>(null);
  const [currentRunId, setCurrentRunId] = useState(runId);

  if (runId !== currentRunId) {
    setCurrentRunId(runId);
    setOffset(0);
    setSelected(null);
  }

  const tasks = useApi<SubscriptionTaskPage>(
    runId ? `/runs/${runId}/subscription-tasks?offset=${offset}&limit=${pageSize}` : null,
    { refreshIntervalMs, keepPreviousOnRefresh: true },
  );

  const movePage = useCallback((nextOffset: number) => {
    setSelected(null);
    setOffset(nextOffset);
  }, []);

  return {
    runId,
    offset,
    pageSize,
    selected,
    setSelected,
    movePage,
    tasks,
    refresh: tasks.refresh,
  };
}

export function SubscriptionTasksProvider({
  value,
  children,
}: {
  value: SubscriptionTasksState;
  children: ReactNode;
}) {
  return (
    <SubscriptionTasksContext.Provider value={value}>
      {children}
    </SubscriptionTasksContext.Provider>
  );
}
