'use client';
import Link from 'next/link';
import { PolicyBrowser } from '@/components/projects/policy-browser';
import { useApi } from '@/hooks/use-api';
import type { components } from '@/lib/api/schema';

const roles = [
  ['planner', 'Planner', 'Proposes a plan and its checks for human approval.'],
  ['developer', 'Developer', 'Implements the approved plan with named tools and bounded remediation.'],
  ['reviewer', 'Reviewer', 'Independently evaluates changes and records findings.'],
] as const;

export default function AgentsPage() {
  const prompts = useApi<components['schemas']['PromptMetadata'][]>('/agent-prompts');
  return <><h1>Agents &amp; models</h1>
    <p>Provider and budget values belong to the selected immutable project policy.</p>
    <PolicyBrowser>{(policy, project) => <>
      <p><Link href={`/projects/${project.id}`}>Edit models and budgets through a new policy version</Link></p>
      {roles.map(([role, label, summary]) => {
        const model = policy.document[`${role}_model`] as components['schemas']['AgentModelPolicy'] | undefined;
        return <section key={role}><h2>{label}{role === 'reviewer' ? ' · Independent' : ''}</h2><p>{summary}</p>
          {model ? <dl>
            <dt>Provider / model</dt><dd>{model.provider} / {model.model}</dd>
            <dt>Input / output token limits</dt><dd>{model.max_input_tokens} / {model.max_output_tokens}</dd>
            <dt>Tool-call limit</dt><dd>{model.max_tool_calls}</dd>
            <dt>Duration limit</dt><dd>{model.max_duration_seconds} seconds</dd>
            <dt>Cost limit</dt><dd>{model.max_cost_minor} minor currency units</dd>
          </dl> : <p>Model policy unavailable for this version.</p>}
        </section>;
      })}
    </>}</PolicyBrowser>
    <section><h2>Current configured prompts</h2>
      <p>These digests describe the server’s current prompt files. Each execution records its own frozen prompt evidence; these values do not reconstruct past executions.</p>
      {prompts.loading && <p role="status">Loading prompt metadata…</p>}
      {prompts.failed && <p role="alert">Prompt metadata unavailable. <button onClick={prompts.refresh}>Retry</button></p>}
      {prompts.value && <ul>{prompts.value.map(prompt => <li key={prompt.role}>{prompt.role} · {prompt.version}<br /><code>{prompt.digest}</code></li>)}</ul>}
    </section>
  </>;
}
