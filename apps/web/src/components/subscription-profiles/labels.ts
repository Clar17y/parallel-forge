import type { components } from '@/lib/api/schema';

const labels: Record<string, string> = {
  primary: 'Primary',
  routine_implementation: 'Implementer',
  complex_implementation: 'Complex implementer',
  independent_review: 'Reviewer',
  planning: 'Planner',
  exploration: 'Explorer',
  security: 'Security reviewer',
  integration: 'Integrator',
  verification: 'Verifier',
  codex_app_server: 'Codex',
  claude_code: 'Claude Code',
  gemini_cli: 'Gemini CLI',
  antigravity_cli: 'Antigravity',
  muse_code: 'Muse Code',
  meta: 'Meta',
  api_key: 'API key',
  allowance_only: 'Allowance only',
  paid_opt_in: 'Paid opt-in',
};

export function profileLabel(value: unknown): string {
  const text = String(value ?? '');
  return labels[text] ?? text.replaceAll('_', ' ').replace(/^./, char => char.toUpperCase());
}

export function deriveProfileSummaryLabel(
  preferences: Array<{ purpose?: string; preferred_route?: { model?: string } }> = []
): string {
  const primary = preferences.find(p => p.purpose === 'primary')?.preferred_route?.model;
  if (primary) return String(primary);
  const models = Array.from(new Set(preferences.map(p => p.preferred_route?.model).filter(Boolean)));
  if (models.length === 1) return String(models[0]);
  if (models.length > 1) return `${models[0]} + ${models.length - 1} more`;
  return 'Standard route';
}

export function profileVersionLabel(profile: Pick<components['schemas']['ProfileResponse'], 'profile_id' | 'version' | 'preferences'>): string {
  const identity = profile.profile_id.length > 12 ? `${profile.profile_id.slice(0, 8)}…` : profile.profile_id;
  return `${deriveProfileSummaryLabel(profile.preferences)} · v${profile.version} · ${identity}`;
}
