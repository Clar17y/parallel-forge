import type { components } from '@/lib/api/schema';

export type ReasoningEffort = components['schemas']['ReasoningEffort'];
export type Route = components['schemas']['RouteInput'];
export type Preference = components['schemas']['PreferenceInput'];

export type SubscriptionModelOption = components['schemas']['SubscriptionModelOption'];
export type SubscriptionModelCatalogView = components['schemas']['SubscriptionModelCatalogView'];
export type SubscriptionModelCatalogPage = components['schemas']['SubscriptionModelCatalogPage'];

export const createRoute = (
  provider: string,
  client: string,
  model: string,
  effort: ReasoningEffort = 'low'
): Route => ({
  provider,
  client,
  model,
  effort,
  auth_mode: 'subscription',
  billing_mode: 'allowance_only',
});

export interface RoleSeedConfig {
  purpose: components['schemas']['SpecialistPurpose'];
  preferred: {
    provider: string;
    client: string;
    model: string;
    effort: ReasoningEffort;
    family: string;
  };
  fallbacks: Array<{
    provider: string;
    client: string;
    model: string;
    effort: ReasoningEffort;
    family: string;
  }>;
}

export const offlineRoleSeeds: RoleSeedConfig[] = [
  {
    purpose: 'primary',
    preferred: { provider: 'openai', client: 'codex_app_server', model: 'gpt-6-astra', effort: 'low', family: 'astra' },
    fallbacks: [],
  },
  {
    purpose: 'routine_implementation',
    preferred: { provider: 'google', client: 'gemini_cli', model: 'gemini-3.8-flash', effort: 'medium', family: 'flash' },
    fallbacks: [
      { provider: 'openai', client: 'codex_app_server', model: 'gpt-6-luna', effort: 'medium', family: 'luna' },
    ],
  },
  {
    purpose: 'complex_implementation',
    preferred: { provider: 'openai', client: 'codex_app_server', model: 'gpt-6-sol', effort: 'medium', family: 'sol' },
    fallbacks: [],
  },
  {
    purpose: 'independent_review',
    preferred: { provider: 'anthropic', client: 'claude_code', model: 'claude-opus-5-5', effort: 'medium', family: 'opus' },
    fallbacks: [
      { provider: 'openai', client: 'codex_app_server', model: 'gpt-6-astra', effort: 'low', family: 'astra' },
    ],
  },
  {
    purpose: 'planning',
    preferred: { provider: 'openai', client: 'codex_app_server', model: 'gpt-6-sol', effort: 'medium', family: 'sol' },
    fallbacks: [],
  },
  {
    purpose: 'exploration',
    preferred: { provider: 'openai', client: 'codex_app_server', model: 'gpt-6-luna', effort: 'medium', family: 'luna' },
    fallbacks: [],
  },
  {
    purpose: 'security',
    preferred: { provider: 'openai', client: 'codex_app_server', model: 'gpt-6-sol', effort: 'high', family: 'sol' },
    fallbacks: [],
  },
  {
    purpose: 'integration',
    preferred: { provider: 'openai', client: 'codex_app_server', model: 'gpt-6-sol', effort: 'medium', family: 'sol' },
    fallbacks: [],
  },
  {
    purpose: 'verification',
    preferred: { provider: 'openai', client: 'codex_app_server', model: 'gpt-6-luna', effort: 'medium', family: 'luna' },
    fallbacks: [],
  },
];

/**
 * Extracts numeric version segments from a model ID.
 * Avoids lexical version comparison bugs (e.g. gpt-10-sol vs gpt-7-sol).
 */
export function extractNumericVersion(modelId: string): number[] {
  const matches = modelId.match(/\d+(?:[.-]\d+)*/g);
  if (!matches || matches.length === 0) return [0];
  return matches[0].split(/[.-]/).map(part => parseInt(part, 10) || 0);
}

/**
 * Compares two numeric version arrays.
 * Returns > 0 if a > b, < 0 if a < b, 0 if equal.
 */
export function compareNumericVersions(a: number[], b: number[]): number {
  const length = Math.max(a.length, b.length);
  for (let i = 0; i < length; i++) {
    const valA = a[i] ?? 0;
    const valB = b[i] ?? 0;
    if (valA !== valB) return valA - valB;
  }
  return 0;
}

export function isModelFamilyMatch(modelId: string, family: string): boolean {
  const pattern = family === 'opus'
    ? /^claude-opus-\d+(?:[.-]\d+)*$/i
    : family === 'flash'
      ? /^gemini-\d+(?:[.-]\d+)*-flash$/i
      : ['astra', 'sol', 'luna'].includes(family)
        ? new RegExp(`^gpt-\\d+(?:[.-]\\d+)*-${family}$`, 'i')
        : undefined;
  return pattern?.test(modelId) ?? false;
}

const observedTime = (catalog: SubscriptionModelCatalogView): number => {
  const time = Date.parse(catalog.observed_at ?? '');
  return Number.isFinite(time) ? time : -Infinity;
};

export function selectCatalogForRoute(
  catalogs: SubscriptionModelCatalogView[] | undefined,
  provider: string,
  client: string
): SubscriptionModelCatalogView | undefined {
  return catalogs
    ?.filter(c => c.provider.toLowerCase() === provider.trim().toLowerCase() &&
      c.client.toLowerCase() === client.trim().toLowerCase())
    .sort((a, b) => {
      const rank = (c: SubscriptionModelCatalogView) =>
        Number(c.status === 'available' && !c.stale) * 4 + Number(c.source === 'provider') * 2 +
        Number(observedTime(c) !== -Infinity);
      return rank(b) - rank(a) || observedTime(b) - observedTime(a);
    })[0];
}

/**
 * Derives the best model choice from available catalogs for a specified route target.
 * Retains semantic role families and effort compatibility while selecting the newest
 * numeric family version.
 */
export function deriveModelFromCatalogs(
  target: { provider: string; client: string; family: string; effort: ReasoningEffort; seedModel: string },
  catalogs?: SubscriptionModelCatalogView[]
): { model: string; source: 'catalog' | 'seed' } {
  if (!catalogs || catalogs.length === 0) {
    return { model: target.seedModel, source: 'seed' };
  }

  const matchingCatalog = selectCatalogForRoute(catalogs, target.provider, target.client);

  if (!matchingCatalog || matchingCatalog.source !== 'provider' ||
      matchingCatalog.status !== 'available' || matchingCatalog.stale ||
      observedTime(matchingCatalog) === -Infinity || matchingCatalog.models.length === 0) {
    return { model: target.seedModel, source: 'seed' };
  }

  // Filter to models matching semantic family and effort compatibility
  const candidates = matchingCatalog.models.filter(option => {
    if (!isModelFamilyMatch(option.id, target.family)) return false;
    // Check effort compatibility: if model options advertise efforts, must include target effort
    return option.efforts.includes(target.effort);
  });

  if (candidates.length === 0) {
    return { model: target.seedModel, source: 'seed' };
  }

  // Sort candidates by numeric version descending (avoiding lexical comparison)
  const sorted = [...candidates].sort((a, b) => {
    const verA = extractNumericVersion(a.id);
    const verB = extractNumericVersion(b.id);
    return compareNumericVersions(verB, verA);
  });

  const bestCandidate = sorted[0];
  return { model: bestCandidate.id, source: 'catalog' };
}

/**
 * Generates role preferences with newest supported family models from current provider
 * catalogs when feasible, falling back cleanly to offline seeds when unavailable.
 */
export function defaultRolePreferences(catalogs?: SubscriptionModelCatalogView[]): Preference[] {
  const routeForSeed = (seed: RoleSeedConfig['preferred']): Route => {
    const derived = deriveModelFromCatalogs(
      {
        provider: seed.provider,
        client: seed.client,
        family: seed.family,
        effort: seed.effort,
        seedModel: seed.model,
      },
      catalogs
    );
    return createRoute(seed.provider, seed.client, derived.model, seed.effort);
  };
  return offlineRoleSeeds.map(role => ({
    purpose: role.purpose,
    preferred_route: routeForSeed(role.preferred),
    fallback_routes: role.fallbacks.map(routeForSeed),
  }));
}
