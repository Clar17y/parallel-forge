import { offlineRoleSeeds, type ReasoningEffort, type Route, type SubscriptionModelCatalogView } from '../subscription-profiles/models';

export interface RoutePreset {
  provider: string;
  client: string;
  model: string;
  label: string;
  efforts: ReasoningEffort[];
  defaultEffort: ReasoningEffort;
  source: 'catalog' | 'suggestion';
}

const allEfforts: ReasoningEffort[] = ['none', 'low', 'medium', 'high', 'maximum'];
const effortLabel = (effort: ReasoningEffort) => effort === 'maximum' ? 'Max' : effort[0].toUpperCase() + effort.slice(1);

function friendlyModel(id: string, label?: string) {
  const known: Record<string, string> = {
    'gpt-6.1-sol': 'Sol 6.1', 'gpt-6-sol': 'Sol 6', 'gpt-6-luna': 'Luna 6',
    'gpt-6-astra': 'Astra 6', 'claude-opus-5-5': 'Claude Opus 5.5',
    'gemini-3.8-flash': 'Google Gemini 3.8 Flash', 'gemini-3.5-flash': 'Google Gemini 3.5 Flash',
    'gemini-2.5-flash': 'Google Gemini 2.5 Flash', 'gemini-2.5-pro': 'Google Gemini 2.5 Pro',
  };
  if (known[id]) return known[id];
  return label && label !== id ? label : id.replace(/[-_]+/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
}

const modelIdentity = (route: Pick<Route, 'provider' | 'client' | 'model'>) => JSON.stringify([route.provider, route.client, route.model]);
export const routePresetIdentity = modelIdentity;

export function routePresets(catalogs: SubscriptionModelCatalogView[] = []): RoutePreset[] {
  const presets: RoutePreset[] = [];
  const add = (route: { provider: string; client: string; model: string; effort: ReasoningEffort }, label: string) => {
    const identity = modelIdentity(route);
    const current = presets.find(preset => modelIdentity(preset) === identity);
    if (current) {
      if (current.source === 'suggestion') {
        current.efforts = [...new Set([...current.efforts, route.effort])];
      }
      if (!current.efforts.includes(current.defaultEffort)) current.defaultEffort = current.efforts[0] ?? 'low';
      return;
    }
    presets.push({ ...route, label, efforts: [route.effort], defaultEffort: route.effort, source: 'suggestion' });
  };
  for (const seed of offlineRoleSeeds) {
    for (const route of [seed.preferred, ...seed.fallbacks]) add(route, friendlyModel(route.model));
  }
  add({ provider: 'openai', client: 'codex_app_server', model: 'gpt-6.1-sol', effort: 'maximum' }, 'Sol 6.1');
  add({ provider: 'google', client: 'gemini_cli', model: 'gemini-3.8-flash', effort: 'medium' }, 'Google Gemini 3.8 Flash');
  for (const catalog of catalogs) {
    for (const model of catalog.models) {
      const route = { provider: catalog.provider, client: catalog.client, model: model.id, effort: model.efforts[0] ?? 'low' as ReasoningEffort };
      const preset = presets.find(item => modelIdentity(item) === modelIdentity(route));
      const efforts = model.efforts.length ? model.efforts : ['low' as ReasoningEffort];
      if (preset) {
        preset.source = 'catalog';
        preset.label = friendlyModel(model.id, model.label);
        preset.efforts = [...efforts];
        if (!preset.efforts.includes(preset.defaultEffort)) preset.defaultEffort = preset.efforts[0];
      } else presets.push({ ...route, label: friendlyModel(model.id, model.label), efforts: [...efforts], defaultEffort: route.effort, source: 'catalog' });
    }
  }
  const counts = new Map<string, number>();
  for (const preset of presets) counts.set(preset.label, (counts.get(preset.label) ?? 0) + 1);
  const clientLabels = presets.map(preset => counts.get(preset.label)! > 1
    ? { ...preset, label: `${preset.label} · ${preset.client.replace(/[_-]+/g, ' ')}` }
    : preset).sort((a, b) => a.label.localeCompare(b.label));
  const disambiguatedCounts = new Map<string, number>();
  for (const preset of clientLabels) disambiguatedCounts.set(preset.label, (disambiguatedCounts.get(preset.label) ?? 0) + 1);
  return clientLabels.map(preset => disambiguatedCounts.get(preset.label)! > 1
    ? { ...preset, label: `${preset.label} · ${friendlyProvider(preset.provider)}` }
    : preset).sort((a, b) => a.label.localeCompare(b.label));
}

function friendlyProvider(provider: string) {
  const names: Record<string, string> = { openai: 'OpenAI', google: 'Google', anthropic: 'Anthropic' };
  return names[provider.toLowerCase()] ?? provider.replace(/[-_]+/g, ' ');
}

export function withCurrentRoute(presets: RoutePreset[], route: Route): RoutePreset[] {
  if (!route.provider || !route.client || !route.model) return presets;
  const identity = modelIdentity(route);
  if (presets.some(preset => modelIdentity(preset) === identity)) return presets;
  return [...presets, { provider: route.provider, client: route.client, model: route.model, label: `${friendlyModel(route.model)} · Saved custom`, efforts: [route.effort], defaultEffort: route.effort, source: 'suggestion' }];
}

export function reasoningOptions(preset: RoutePreset | undefined, current: ReasoningEffort, customOverride = false): ReasoningEffort[] {
  return [...new Set([...(customOverride ? allEfforts : preset?.efforts ?? allEfforts), current])];
}

export { effortLabel };
