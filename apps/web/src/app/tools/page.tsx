'use client';
import Link from 'next/link';
import { LoadingStatus } from '@/components/ui/loading-status';
import { useApi } from '@/hooks/use-api';
import type { components } from '@/lib/api/schema';
import { Disclosure } from '@/components/ui/disclosure';

type Capability = 'Read' | 'Write' | 'Git' | 'Checks' | 'Review';

const CAPABILITIES: Capability[] = ['Read', 'Write', 'Git', 'Checks', 'Review'];

function categorizeTool(tool: string): Capability | 'Other' {
  if (tool.startsWith('repository.read') || tool.startsWith('repository.list') || tool.startsWith('repository.search')) return 'Read';
  if (tool.startsWith('repository.write') || tool.startsWith('repository.delete') || tool.startsWith('repository.rename') || tool.startsWith('file.')) return 'Write';
  if (tool.startsWith('git.')) return 'Git';
  if (tool.startsWith('build.') || tool.includes('check')) return 'Checks';
  if (tool.startsWith('review') || tool.startsWith('validation-results')) return 'Review';
  return 'Other';
}

export default function ToolsPage() {
  const permissions = useApi<components['schemas']['ListPage_PermissionItem_']>('/tool-permissions?limit=100');

  return (
    <>
      <div className="page-title-row">
        <h1>Tool permissions</h1>
      </div>
      <div className="readable-prose mb-4">
        <p>
          Server-controlled tool capabilities granted to each agent role. Project policy commands
          and runner sandboxing (Docker by default, unsandboxed for trusted hosts) further constrain
          execution.
        </p>
      </div>

      <div className="append-notice mb-6" role="note">
        <div>
          <strong>Local CLI Provider Boundary:</strong> Local CLI providers are operator-trusted and
          may expose native tools beyond Forge&apos;s interface (Antigravity carries an
          approved-tools-unproved warning). This page describes Forge&apos;s interface, not a
          guaranteed ceiling on what a local CLI runtime may execute. Start from{' '}
          <Link href="/settings" className="underline font-semibold">
            Settings
          </Link>{' '}
          to navigate configuration boundaries.
        </div>
      </div>

      {permissions.loading && <LoadingStatus>Loading permissions…</LoadingStatus>}
      {permissions.failed && (
        <p role="alert">
          Permissions unavailable. <button onClick={permissions.refresh}>Retry</button>
        </p>
      )}

      {permissions.value && (
        <>
          <section className="mb-6">
            <h2>Role capability matrix</h2>
            <div className="table-scroll">
              <table className="data-table">
                <thead>
                  <tr>
                    <th>Role</th>
                    {CAPABILITIES.map(cap => (
                      <th key={cap} className="text-center">{cap}</th>
                    ))}
                    <th className="text-right">Total tools</th>
                  </tr>
                </thead>
                <tbody>
                  {permissions.value.items.map(item => {
                    const counts = CAPABILITIES.reduce((acc, cap) => {
                      acc[cap] = item.tools.filter(t => categorizeTool(t) === cap).length;
                      return acc;
                    }, {} as Record<Capability, number>);

                    return (
                      <tr key={item.role}>
                        <td className="font-semibold capitalize">{item.role}</td>
                        {CAPABILITIES.map(cap => (
                          <td key={cap} className="text-center">
                            {counts[cap] > 0 ? (
                              <span className="font-mono text-xs text-[var(--success)] font-semibold">
                                ✓ {counts[cap]}
                              </span>
                            ) : (
                              <span className="text-[var(--muted)]">—</span>
                            )}
                          </td>
                        ))}
                        <td className="text-right font-mono font-semibold">
                          {item.tools.length}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </section>

          <section>
            <h2>Exact tool identifiers &amp; role details</h2>
            <div className="card-stack">
              {permissions.value.items.map(item => (
                <Disclosure
                  key={item.role}
                  defaultOpen={item.role === 'developer'}
                  summary={
                    <div className="summary-line">
                      <span className="summary-title capitalize">
                        {item.role}
                      </span>
                      <span className="summary-meta font-mono">
                        {item.tools.length} tools allowed
                      </span>
                    </div>
                  }
                >
                  <ul className="grid grid-cols-1 md:grid-cols-2 gap-2">
                    {item.tools.map(tool => (
                      <li
                        key={tool}
                        className="flex items-center justify-between p-2 rounded bg-[var(--surface-muted)] border border-[var(--border-subtle)] text-xs"
                      >
                        <code className="font-mono">{tool}</code>
                        <span className="meta capitalize">{categorizeTool(tool)}</span>
                      </li>
                    ))}
                  </ul>
                </Disclosure>
              ))}
            </div>
          </section>
        </>
      )}
    </>
  );
}
