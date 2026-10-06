'use client';

import { useCallback, useEffect } from 'react';
import { useApi } from '@/hooks/use-api';
import { api } from '@/lib/api/client';
import { useEpicMutations } from './use-epic-mutations';
import type {
  EpicBudgetEdit,
  EpicBudgetPermitReceipt,
  EpicBudgetPermitRequest,
  EpicBudgetProjection,
  EpicBudgetReceipt,
  TaskBudget,
} from './types';

export interface EditBudgetOptions {
  ceiling: TaskBudget;
  expectedVersion: number;
  disabledDimensions?: string[];
  note?: string | null;
}

export interface PermitAdmissionOptions {
  runId: string;
  expectedVersion: number;
  note?: string | null;
}

export function useEpicBudget(epicId: string) {
  const budgetPath = epicId ? `/epics/${epicId}/budget` : null;
  const budgetApi = useApi<EpicBudgetProjection>(budgetPath, {
    refreshIntervalMs: 5000,
    keepPreviousOnRefresh: true,
    keepPreviousOnError: true,
  });

  const mutations = useEpicMutations(epicId);
  const { execute, registerCompletion } = mutations;
  const refreshBudget = budgetApi.refresh;

  const readLatestBudget = useCallback(async () => {
    if (!budgetPath) throw new Error('Epic budget is unavailable');
    const latest = await api<EpicBudgetProjection>(budgetPath, { cache: 'no-store' });
    if (!latest) throw new Error('Epic budget is unavailable');
    return latest;
  }, [budgetPath]);

  useEffect(() => {
    const unregisterEdit = registerCompletion('budget-edit', () => {
      refreshBudget();
    });
    const unregisterPermit = registerCompletion('budget-permit', () => {
      refreshBudget();
    });
    return () => {
      unregisterEdit();
      unregisterPermit();
    };
  }, [refreshBudget, registerCompletion]);

  const editBudget = useCallback(
    async (options: EditBudgetOptions) => {
      const body: EpicBudgetEdit = {
        ceiling: options.ceiling,
        expected_version: options.expectedVersion,
        disabled_dimensions: options.disabledDimensions ?? [],
        ...(options.note !== undefined ? { note: options.note } : {}),
      };
      const receipt = await execute<EpicBudgetReceipt>(
        'PUT',
        `/epics/${epicId}/budget`,
        body as unknown as Record<string, unknown>,
        { kind: 'budget-edit' }
      );
      return receipt;
    },
    [epicId, execute]
  );

  const permitBudgetAdmission = useCallback(
    async (options: PermitAdmissionOptions) => {
      const body: EpicBudgetPermitRequest = {
        run_id: options.runId,
        expected_version: options.expectedVersion,
        ...(options.note !== undefined ? { note: options.note } : {}),
      };
      const receipt = await execute<EpicBudgetPermitReceipt>(
        'POST',
        `/epics/${epicId}/budget/admissions`,
        body as unknown as Record<string, unknown>,
        { kind: 'budget-permit' }
      );
      return receipt;
    },
    [epicId, execute]
  );

  const budget = budgetApi.value ?? null;

  const isUnlimited = useCallback(
    (dimension: string): boolean => {
      return budget?.disabled_dimensions?.includes(dimension) ?? false;
    },
    [budget]
  );

  const getKnown = useCallback(
    (dimension: string): number => {
      return budget?.known?.[dimension] ?? 0;
    },
    [budget]
  );

  const getHeld = useCallback(
    (dimension: string): number => {
      return budget?.held?.[dimension] ?? 0;
    },
    [budget]
  );

  return {
    budget,
    loading: budgetApi.loading,
    failed: budgetApi.failed,
    refresh: budgetApi.refresh,
    readLatestBudget,
    isUnlimited,
    getKnown,
    getHeld,
    isUnknown: budget?.unknown ?? false,
    currency: budget?.currency ?? null,
    warnings: budget?.warnings ?? [],
    permits: budget?.permits ?? [],
    ownerActions: budget?.owner_actions ?? [],
    editBudget,
    permitBudgetAdmission,
    mutations,
  };
}
