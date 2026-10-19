import Link from 'next/link';
import { Disclosure } from '@/components/ui/disclosure';

const SURFACES = [
  {
    name: 'Project policies',
    task: 'Set runner commands, sandboxing & merge rules',
    href: '/policies',
    description:
      'What agents may do in each project: named commands, runner and sandboxing, API models, budgets, and merge rules. Saved as a new policy version from the project page.',
  },
  {
    name: 'Agents & models',
    task: 'Configure API models, reasoning strength & budgets',
    href: '/agents',
    description:
      'Which provider and model each role uses under the selected project policy, including reasoning strength, token, tool-call, duration, and cost limits. Edited through a new policy version on the project page.',
  },
  {
    name: 'Tool permissions',
    task: 'Inspect role capabilities & CLI boundaries',
    href: '/tools',
    description:
      'The named tools Forge offers each role through its controlled interface, and how project command limits constrain them.',
  },
  {
    name: 'Subscription profiles',
    task: 'Manage CLI subscription models & fallbacks',
    href: '/subscription-profiles',
    description:
      'CLI subscription models and reasoning effort, recorded as immutable profile versions. Select a profile per project or override for a single run.',
  },
  {
    name: 'Usage & monitoring policies',
    task: 'Track provider usage, review thresholds & token caps',
    href: '/usage',
    description:
      'Observed provider usage, baselines, and reports. Task-usage monitoring policies (checkpoint mode, review thresholds, and optional hard token/context caps) are edited per project on the project page.',
  },
] as const;

export default function SettingsPage() {
  return (
    <>
      <div className="page-title-row">
        <h1>Settings</h1>
      </div>
      <p className="meta mb-6">
        The controls that decide how Forge runs agents, in one index. Each surface below owns one
        part of that picture — start here when you are not sure where a setting lives.
      </p>

      <div className="card-stack">
        {SURFACES.map(surface => (
          <section key={surface.href} className="panel">
            <div className="summary-line">
              <div className="summary-title">
                <h2>
                  <Link href={surface.href}>{surface.name}</Link>
                </h2>
              </div>
              <span className="summary-meta">{surface.task}</span>
            </div>
            <p className="mt-2 text-sm text-[var(--muted)]">{surface.description}</p>
          </section>
        ))}
      </div>

      <section className="mt-6">
        <Disclosure
          summary={
            <div className="summary-line">
              <span className="summary-title">Runtime boundaries, local CLI providers &amp; editing rules</span>
              <span className="summary-meta">Operator trust &amp; immutability notes</span>
            </div>
          }
        >
          <div className="readable-prose space-y-2">
            <p>
              Local CLI providers are operator-trusted and may expose native tools beyond Forge&apos;s
              interface (Antigravity carries an approved-tools-unproved warning), so treat this as
              Forge&apos;s interface, not a guaranteed ceiling.
            </p>
            <p>
              Policies and profiles are durable: changes create a new policy or append a new version
              rather than rewriting past records.
            </p>
          </div>
        </Disclosure>
      </section>
    </>
  );
}
