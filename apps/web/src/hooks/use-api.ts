'use client';
import { useCallback, useEffect, useRef, useState } from 'react';
import { api } from '@/lib/api/client';

type RefreshOptions = {
  refreshIntervalMs?: number;
  keepPreviousOnRefresh?: boolean;
  refreshStorageKey?: string;
};

export function useApi<T>(path: string | null, {
  refreshIntervalMs,
  keepPreviousOnRefresh = false,
  refreshStorageKey,
}: RefreshOptions = {}) {
  const requestToken = useRef(0);
  const [activeToken, setActiveToken] = useState(0);
  const [result, setResult] = useState<{ key: string; path: string; value?: T; failed: boolean; token: number }>();
  const key = `${path}:${activeToken}`;
  useEffect(() => {
    if (!path) return;
    const controller = new AbortController();
    api<T>(path, { signal: controller.signal, cache: 'no-store' }).then(value => {
      if (!controller.signal.aborted) {
        if (value !== undefined) {
          setResult({ key, path, value, failed: false, token: activeToken });
        } else {
          setResult({ key, path, failed: true, token: activeToken });
        }
      }
    }, () => {
      if (!controller.signal.aborted) setResult({ key, path, failed: true, token: activeToken });
    });
    return () => controller.abort();
  }, [activeToken, path, key]);
  const refreshLocal = useCallback(() => {
    const token = ++requestToken.current;
    setActiveToken(token);
    return token;
  }, []);
  const refresh = useCallback(() => {
    const token = refreshLocal();
    if (refreshStorageKey && typeof window !== 'undefined') {
      try {
        window.localStorage.setItem(refreshStorageKey, `${Date.now()}:${token}`);
      } catch {
        // Polling and this tab's refresh remain available when storage is disabled.
      }
    }
    return token;
  }, [refreshLocal, refreshStorageKey]);
  useEffect(() => {
    if (!refreshStorageKey || typeof window === 'undefined') return;
    const receiveRefresh = (event: StorageEvent) => {
      if (event.key === refreshStorageKey && event.newValue !== null) refreshLocal();
    };
    window.addEventListener('storage', receiveRefresh);
    return () => window.removeEventListener('storage', receiveRefresh);
  }, [refreshLocal, refreshStorageKey]);
  const current = result?.key === key ? result : undefined;
  useEffect(() => {
    if (!path || !current || refreshIntervalMs === undefined) return;
    if (!Number.isFinite(refreshIntervalMs) || refreshIntervalMs < 1000) return;
    // Start the next interval after settlement, so slow requests are not aborted
    // or multiplied by a periodic timer. Cleanup also stops polling on unmount.
    const timer = setTimeout(refreshLocal, refreshIntervalMs);
    return () => clearTimeout(timer);
  }, [path, current, refreshIntervalMs, refreshLocal]);
  const shown = current ?? (keepPreviousOnRefresh && result?.path === path ? result : undefined);
  return {
    value: shown?.value,
    token: shown?.token ?? 0,
    failed: current?.failed ?? false,
    loading: path !== null && !current && shown?.value === undefined,
    refreshing: path !== null && !current,
    refresh,
  };
}
