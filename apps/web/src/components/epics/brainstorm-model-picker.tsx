'use client';

import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { useApi } from '@/hooks/use-api';
import type { components } from '@/lib/api/schema';
import { profileLabel } from '@/components/subscription-profiles/labels';
import { availableBrainstormRoutes, routeKey, runtimeFreshMs, toBrainstormRoute, type BrainstormRoute } from './brainstorm-model-choice';

type Profile = components['schemas']['ProfileResponse'];
type Runtime = components['schemas']['SubscriptionRuntimeStatusPage'];

export function BrainstormModelPicker({ projectId, choice, onChange, disabled = false }: {
  projectId: string;
  choice: BrainstormRoute | null;
  onChange: (route: BrainstormRoute | null, reason?: 'unavailable') => void;
  disabled?: boolean;
}) {
  const profile = useApi<Profile | null>(projectId ? `/projects/${projectId}/subscription-profile` : null);
  const [pollMs, setPollMs] = useState(15_000);
  const runtime = useApi<Runtime>('/subscription-runtime?offset=0&limit=100', {
    refreshIntervalMs: pollMs,
    keepPreviousOnRefresh: true,
    keepPreviousOnError: true,
  });
  const [snapshot, setSnapshot] = useState<{ token: number; observedAt: string; receivedAt: number } | null>(null);
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!runtime.value || snapshot?.token === runtime.token) return;
    const observedAt = runtime.value.observed_at;
    const freshMs = runtimeFreshMs(runtime.value);
    const timer = setTimeout(() => {
      const receivedAt = Date.now();
      setSnapshot(previous => ({
        token: runtime.token,
        observedAt,
        receivedAt: previous && previous.observedAt === observedAt
          ? previous.receivedAt : receivedAt,
      }));
      setNow(receivedAt);
      setPollMs(Math.max(1000, Math.min(15000, freshMs / 2)));
    }, 0);
    return () => clearTimeout(timer);
  }, [runtime.token, runtime.value, snapshot?.token]);
  const receivedAt = snapshot && snapshot.observedAt === runtime.value?.observed_at ? snapshot.receivedAt : now;
  const availability = availableBrainstormRoutes(runtime.value, receivedAt, now);
  useEffect(() => {
    if (availability.expiresAt === null) return;
    const timer = setTimeout(() => setNow(Date.now()), Math.max(1, availability.expiresAt - now + 1));
    return () => clearTimeout(timer);
  }, [availability.expiresAt, now]);
  const preferred = profile.value?.preferences?.find(item => item.purpose === 'exploration')?.preferred_route;
  const defaultRoute = toBrainstormRoute(preferred);
  const configured = availability.routes;
  const selectedProject = useRef(projectId);
  const validChoice = choice && configured.some(route => routeKey(route) === routeKey(choice)) ? choice : null;
  useLayoutEffect(() => {
    const changedProject = selectedProject.current !== projectId;
    selectedProject.current = projectId;
    if (choice && (changedProject || !validChoice)) onChange(null, 'unavailable');
  }, [choice, onChange, projectId, validChoice]);
  const unique = [...new Map(configured.map(route => [routeKey(route), route])).values()];
  const current = validChoice ?? defaultRoute;
  const modelKey = current ? JSON.stringify([current.provider, current.client, current.model]) : '';
  const defaultModelKey = defaultRoute ? JSON.stringify([defaultRoute.provider, defaultRoute.client, defaultRoute.model]) : '';
  const variants = unique.filter(route => JSON.stringify([route.provider, route.client, route.model]) === modelKey);
  const efforts = [...new Set([
    ...(current ? [current.effort] : []),
    ...(defaultRoute && modelKey === defaultModelKey ? [defaultRoute.effort] : []),
    ...variants.map(route => route.effort),
  ])];
  const models = [...new Map(unique.map(route => [JSON.stringify([route.provider, route.client, route.model]), route])).entries()];

  return <div className="flex flex-wrap gap-3 text-sm" aria-label="Assistant model choice">
    <label className="flex flex-col gap-1">Model
      <select aria-label="Model" className="px-2 py-1 border rounded bg-[var(--surface)]" disabled={disabled || !defaultRoute}
        value={validChoice ? modelKey : 'default'} onChange={event => {
          if (event.target.value === 'default') { onChange(null); return; }
          const route = models.find(([key]) => key === event.target.value)?.[1];
          if (route) onChange(route);
        }}>
        <option value="default">{defaultRoute ? `Project default · ${defaultRoute.model}` : 'Project default (configure in AI settings)'}</option>
        {models.map(([key, route]) => <option key={key} value={key}>{route.model} · {profileLabel(route.client)}</option>)}
      </select>
    </label>
    <label className="flex flex-col gap-1">Effort
      <select aria-label="Effort" className="px-2 py-1 border rounded bg-[var(--surface)]" disabled={disabled || !current}
        value={current?.effort ?? ''} onChange={event => {
          if (defaultRoute && modelKey === defaultModelKey && event.target.value === defaultRoute.effort) {
            onChange(null);
            return;
          }
          const route = variants.find(item => item.effort === event.target.value);
          if (route) onChange(route);
        }}>
        {!current && <option value="">Project default</option>}
        {efforts.map(effort => <option key={effort} value={effort}>{effort}</option>)}
      </select>
    </label>
    {!defaultRoute && <p className="self-end text-[var(--muted)]">Choose an Exploration route in <a href="/subscription-profiles">AI settings</a>.</p>}
  </div>;
}
