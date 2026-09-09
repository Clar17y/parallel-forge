'use client';
import { useCallback, useEffect, useState } from 'react';
import { api } from '@/lib/api/client';

export function useApi<T>(path: string | null) {
  const [revision, setRevision] = useState(0);
  const [result, setResult] = useState<{ key: string; value?: T; failed: boolean }>();
  const key = `${path}:${revision}`;
  useEffect(() => {
    if (!path) return;
    const controller = new AbortController();
    api<T>(path, { signal: controller.signal, cache: 'no-store' }).then(value => {
      if (!controller.signal.aborted) setResult({ key, value, failed: value === undefined });
    }, () => { if (!controller.signal.aborted) setResult({ key, failed: true }); });
    return () => controller.abort();
  }, [path, key]);
  const refresh = useCallback(() => setRevision(value => value + 1), []);
  const current = result?.key === key ? result : undefined;
  return { value: current?.value, failed: current?.failed ?? false, loading: path !== null && !current, refresh };
}
