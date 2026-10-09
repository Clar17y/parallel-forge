'use client';

import { useApi } from '@/hooks/use-api';
import type { components } from '@/lib/api/schema';
import { profileLabel } from '@/components/subscription-profiles/labels';
import { routeKey, toBrainstormRoute, type BrainstormRoute } from './brainstorm-model-choice';

type Profile = components['schemas']['ProfileResponse'];
type Runtime = components['schemas']['SubscriptionRuntimeStatusPage'];

export function BrainstormModelPicker({ projectId, choice, onChange, disabled = false }: {
  projectId: string;
  choice: BrainstormRoute | null;
  onChange: (route: BrainstormRoute | null) => void;
  disabled?: boolean;
}) {
  const profile = useApi<Profile | null>(projectId ? `/projects/${projectId}/subscription-profile` : null);
  const runtime = useApi<Runtime>('/subscription-runtime?offset=0&limit=100');
  const preferred = profile.value?.preferences?.find(item => item.purpose === 'exploration')?.preferred_route;
  const defaultRoute = toBrainstormRoute(preferred);
  const configured = runtime.value?.workers?.filter(worker => worker.state === 'current')
    .flatMap(worker => worker.routes?.filter(route => route.configured !== false && route.effective_reason !== 'stale_worker') ?? [])
    .map(toBrainstormRoute).filter((route): route is BrainstormRoute => route !== null) ?? [];
  const routes = [...(defaultRoute ? [defaultRoute] : []), ...configured];
  const unique = [...new Map(routes.map(route => [routeKey(route), route])).values()];
  const current = choice ?? defaultRoute;
  const modelKey = current ? JSON.stringify([current.provider, current.client, current.model]) : '';
  const variants = unique.filter(route => JSON.stringify([route.provider, route.client, route.model]) === modelKey);
  const efforts = [...new Set([...(current ? [current.effort] : []), ...variants.map(route => route.effort)])];
  const models = [...new Map(unique.map(route => [JSON.stringify([route.provider, route.client, route.model]), route])).entries()];

  return <div className="flex flex-wrap gap-3 text-sm" aria-label="Assistant model choice">
    <label className="flex flex-col gap-1">Model
      <select aria-label="Model" className="px-2 py-1 border rounded bg-[var(--surface)]" disabled={disabled || !defaultRoute}
        value={choice ? modelKey : 'default'} onChange={event => {
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
