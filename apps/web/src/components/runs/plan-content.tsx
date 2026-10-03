import type { ReactNode } from 'react';

export const PLAN_SECTIONS = [
  ['assumptions', 'Assumptions'],
  ['affected_components', 'Affected components'],
  ['steps', 'Steps'],
  ['required_checks', 'Required checks'],
  ['risks', 'Risks'],
  ['security_considerations', 'Security considerations'],
  ['dependency_changes', 'Dependency changes'],
] as const;

export type PlanSectionKey = (typeof PLAN_SECTIONS)[number][0];

export type Plan = {
  summary: string;
  owned_paths?: string[];
} & Record<PlanSectionKey, string[]>;

export function parsePlan(text: string): Plan | null {
  try {
    const value = JSON.parse(text);
    if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
    if (typeof value.summary !== 'string' || !value.summary.trim() || value.summary.length > 10000) return null;
    for (const [key] of PLAN_SECTIONS) {
      if (
        !Array.isArray(value[key]) ||
        value[key].length > 100 ||
        !value[key].every((item: unknown) => typeof item === 'string' && item.length <= 5000)
      ) {
        return null;
      }
    }
    if (
      'owned_paths' in value &&
      (!Array.isArray(value.owned_paths) ||
        value.owned_paths.length > 64 ||
        !value.owned_paths.every((item: unknown) => typeof item === 'string' && item.length > 0 && item.length <= 5000))
    ) {
      return null;
    }
    return value as Plan;
  } catch {
    return null;
  }
}

export function PlanContent({ plan }: { plan: Plan }): ReactNode {
  return (
    <>
      <p>{plan.summary}</p>
      {plan.owned_paths !== undefined && (
        <section>
          <h3>Writable paths</h3>
          {plan.owned_paths.length ? (
            <ol>
              {plan.owned_paths.map((path, index) => (
                <li key={index}>{path}</li>
              ))}
            </ol>
          ) : (
            <p>No writable paths.</p>
          )}
        </section>
      )}
      {PLAN_SECTIONS.map(([key, label]) => (
        <section key={key}>
          <h3>{label}</h3>
          {plan[key].length ? (
            <ol>
              {plan[key].map((item, index) => (
                <li key={index}>{item}</li>
              ))}
            </ol>
          ) : (
            <p>None recorded</p>
          )}
        </section>
      ))}
    </>
  );
}
