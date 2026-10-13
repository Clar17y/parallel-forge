import Link from 'next/link';

const SURFACES = [
  {
    name: 'Project policies',
    href: '/policies',
    description:
      'What agents may do in each project: named commands, runner and sandboxing, API models, budgets, and merge rules. Policies are immutable — changes are saved as a new policy version from the project page.',
  },
  {
    name: 'Agents & models',
    href: '/agents',
    description:
      'Which provider and model each role — planner, developer, reviewer — uses under the selected project policy, including reasoning strength and token, tool-call, duration, and cost limits. Edited through a new policy version on the project page.',
  },
  {
    name: 'Tool permissions',
    href: '/tools',
    description:
      'The fixed tool set each role receives, assigned by the Forge server. Project command and runner limits further constrain every invocation, so this page explains the ceiling rather than editing it.',
  },
  {
    name: 'Subscription profiles',
    href: '/subscription-profiles',
    description:
      'CLI subscription models and reasoning effort, recorded as immutable profile versions. Select a profile per project, or override it for a single run when you create it.',
  },
] as const;

export default function SettingsPage() {
  return <>
    <h1>Settings</h1>
    <p className="meta">
      Every control that decides how Forge runs agents, in one index. Each surface below owns one
      part of that picture — start here when you are not sure where a setting lives.
    </p>
    <div className="space-y-4">
      {SURFACES.map(surface => <section key={surface.href} className="panel">
        <h2><Link href={surface.href}>{surface.name}</Link></h2>
        <p>{surface.description}</p>
      </section>)}
    </div>
  </>;
}
