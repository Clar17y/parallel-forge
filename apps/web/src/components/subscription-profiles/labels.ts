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
  api_key: 'API key',
  allowance_only: 'Allowance only',
  paid_opt_in: 'Paid opt-in',
};

export function profileLabel(value: unknown): string {
  const text = String(value ?? '');
  return labels[text] ?? text.replaceAll('_', ' ').replace(/^./, char => char.toUpperCase());
}
