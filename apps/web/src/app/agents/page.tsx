'use client';
import { LoadingStatus } from '@/components/ui/loading-status';
import Link from 'next/link';
import { PolicyBrowser } from '@/components/projects/policy-browser';
import { useApi } from '@/hooks/use-api';
import type { components } from '@/lib/api/schema';
import { Disclosure } from '@/components/ui/disclosure';
import { ProviderBadge } from '@/components/ui/provider-badge';
import { formatDurationSeconds } from '@/lib/format';

const roles = [
  ['planner', 'Planner', 'Proposes a plan and its checks for human approval.'],
  ['developer', 'Developer', 'Implements the approved plan with named tools and bounded remediation.'],
  ['reviewer', 'Reviewer', 'Independently evaluates changes and records findings.'],
] as const;

export default function AgentsPage() {
  const prompts = useApi<components['schemas']['PromptMetadata'][]>('/agent-prompts');
  return (
    <>
      <div className="page-title-row">
        <h1>Agents &amp; models</h1>
      </div>
      <div className="readable-prose mb-4">
        <p>
          Configured API models, reasoning effort and per-task budget limits for this project policy.
          To configure CLI agent models, credentials, or personal fallbacks, see{' '}
          <Link href="/subscription-profiles">Subscription profiles</Link>.
        </p>
      </div>
      <PolicyBrowser>
        {(policy, project) => (
          <>
            <p className="mb-4">
              <Link href={`/projects/${project.id}`}>
                Edit models, reasoning and budgets through a new policy version
              </Link>
            </p>
            {roles.map(([role, label, summary]) => {
              const model = policy.document[`${role}_model`] as components['schemas']['AgentModelPolicy'] | undefined;
              return (
                <Disclosure
                  key={role}
                  className="mb-3"
                  summary={
                    <div className="summary-line">
                      <div className="summary-title">
                        <span>{label}</span>
                        {role === 'reviewer' && <span className="meta">· Independent</span>}
                        {model && <ProviderBadge provider={model.provider} />}
                        {model && <code>{model.model}</code>}
                      </div>
                      <div className="summary-meta">
                        <span>Reasoning: {model?.reasoning_effort ?? 'default'}</span>
                      </div>
                    </div>
                  }
                >
                  <p className="muted mb-3">{summary}</p>
                  {model ? (
                    <div className="detail-grid">
                      <div>
                        <dt>Provider / model</dt>
                        <dd>{model.provider} / {model.model}</dd>
                      </div>
                      <div>
                        <dt>Reasoning strength</dt>
                        <dd>{model.reasoning_effort ?? 'Automatic (provider default)'}</dd>
                      </div>
                      <div>
                        <dt>Input token limit</dt>
                        <dd>{model.max_input_tokens != null ? `${model.max_input_tokens.toLocaleString()} tokens` : 'Unbounded'}</dd>
                      </div>
                      <div>
                        <dt>Output token limit</dt>
                        <dd>{model.max_output_tokens != null ? `${model.max_output_tokens.toLocaleString()} tokens` : 'Unbounded'}</dd>
                      </div>
                      <div>
                        <dt>Tool-call limit</dt>
                        <dd>{model.max_tool_calls != null ? `${model.max_tool_calls.toLocaleString()} calls` : 'Uncapped'}</dd>
                      </div>
                      <div>
                        <dt>Duration limit</dt>
                        <dd>{formatDurationSeconds(model.max_duration_seconds)}</dd>
                      </div>
                      <div>
                        <dt>Cost limit</dt>
                        <dd>{model.max_cost_minor != null ? `${model.max_cost_minor.toLocaleString()} minor currency units` : 'Uncapped'}</dd>
                      </div>
                    </div>
                  ) : (
                    <p>Model policy unavailable for this version.</p>
                  )}
                </Disclosure>
              );
            })}
          </>
        )}
      </PolicyBrowser>
      <section className="mt-6">
        <Disclosure
          summary={
            <div className="summary-line">
              <div className="summary-title">
                <span>Configured prompt digests &amp; provenance</span>
              </div>
              <div className="summary-meta">
                <span>{prompts.value ? `${prompts.value.length} roles` : 'Server configuration'}</span>
              </div>
            </div>
          }
        >
          <div className="readable-prose mb-3">
            <p className="meta">
              These digests describe the server’s current prompt files. Each execution records its own frozen prompt evidence; these values do not reconstruct past executions.
            </p>
          </div>
          {prompts.loading && <LoadingStatus>Loading prompt metadata…</LoadingStatus>}
          {prompts.failed && (
            <p role="alert">
              Prompt metadata unavailable. <button onClick={prompts.refresh}>Retry</button>
            </p>
          )}
          {prompts.value && (
            <ul className="divide-y divide-[var(--border-subtle)]">
              {prompts.value.map(prompt => (
                <li key={prompt.role} className="py-2">
                  <div className="flex items-center justify-between gap-2">
                    <span className="font-semibold capitalize">{prompt.role}</span>
                    <span className="meta">{prompt.version}</span>
                  </div>
                  <code className="text-xs break-all block mt-1">{prompt.digest}</code>
                </li>
              ))}
            </ul>
          )}
        </Disclosure>
      </section>
    </>
  );
}
