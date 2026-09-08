'use client';

import { useRef, useState, type FormEvent } from 'react';
import { ApiError, mutate } from '@/lib/api/client';
import type { components } from '@/lib/api/schema';

type Project = components['schemas']['ProjectResponse'];
type TaskInput = components['schemas']['TaskCreateRequest'];
type IssueInput = { project_id: string; issue_number: number };
type Attempt = { endpoint: '/tasks' | '/tasks/import-github'; payload: TaskInput | IssueInput; taskKey: string; runKey: string; taskId?: string };

export function NewRunForm({ projects, onCreated }: {
  projects: Project[];
  onCreated: (runId: string) => void;
}) {
  const [projectId, setProjectId] = useState(projects[0]?.id ?? '');
  const [title, setTitle] = useState('');
  const [body, setBody] = useState('');
  const [source, setSource] = useState<'text' | 'github'>('text');
  const [issueNumber, setIssueNumber] = useState('');
  const [pending, setPending] = useState(false);
  const [locked, setLocked] = useState(false);
  const [error, setError] = useState('');
  const [fields, setFields] = useState<Record<string, string>>({});
  const attempt = useRef<Attempt | null>(null);
  const busy = useRef(false);
  const project = projects.find(item => item.id === projectId);
  const importing = source === 'github' && project?.issue_import_available === true;
  const validSource = importing ? /^[1-9][0-9]*$/.test(issueNumber) && Number.isSafeInteger(Number(issueNumber)) : !!title.trim() && !!body.trim();

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (busy.current || !project || (!attempt.current && !validSource)) return;
    busy.current = true;
    setPending(true);
    setLocked(true);
    setError('');
    setFields({});
    attempt.current ??= {
      endpoint: importing ? '/tasks/import-github' : '/tasks',
      payload: importing ? { project_id: projectId, issue_number: Number(issueNumber) } : { project_id: projectId, title, body },
      taskKey: crypto.randomUUID(), runKey: crypto.randomUUID(),
    };
    const current = attempt.current;
    try {
      if (!current.taskId) {
        const task = await mutate<components['schemas']['TaskResponse']>(current.endpoint, current.payload,
          { idempotencyKey: current.taskKey });
        if (!task?.id) throw new Error('Task response unavailable');
        current.taskId = task.id;
      }
      const run = await mutate<components['schemas']['RunResponse']>('/runs', { task_id: current.taskId },
        { idempotencyKey: current.runKey });
      if (!run?.id) throw new Error('Run response unavailable');
      onCreated(run.id);
    } catch (failure) {
      if (failure instanceof ApiError && failure.status === 422 && !current.taskId) {
        setFields(failure.fields);
        attempt.current = null;
        setLocked(false);
        setError('Check the task fields and try again.');
      } else {
        setError('Creation could not be confirmed. Retry to recover the same task and run.');
      }
    } finally {
      busy.current = false;
      setPending(false);
    }
  }

  if (!projects.length) return <p>Register a project before creating a run.</p>;
  return <form onSubmit={submit} aria-label="New run">
    <label htmlFor="run-project">Project</label>
    <select id="run-project" value={projectId} disabled={locked || pending}
      aria-invalid={!!fields.project_id} aria-describedby={fields.project_id ? 'project-error' : undefined}
      onChange={event => { setProjectId(event.target.value); setSource('text'); }}>
      {projects.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}
    </select>
    {fields.project_id && <p id="project-error">{fields.project_id}</p>}
    {project && <ProjectRunSummary project={project} />}
    {project?.issue_import_available === true && <label>Task source<select value={source} disabled={locked || pending}
      onChange={event => setSource(event.target.value as 'text' | 'github')}>
      <option value="text">Write a task</option><option value="github">Import GitHub issue</option>
    </select></label>}
    {importing ? <>
      <p>Import from {project.github_repository}. Issue content is untrusted task input.</p>
      <label htmlFor="issue-number">GitHub issue number</label>
      <input id="issue-number" type="number" min="1" max={Number.MAX_SAFE_INTEGER} step="1" required readOnly={locked}
        value={issueNumber} onChange={event => setIssueNumber(event.target.value)}
        aria-invalid={!!fields.issue_number} aria-describedby={fields.issue_number ? 'issue-error' : undefined} />
      {fields.issue_number && <p id="issue-error">{fields.issue_number}</p>}
    </> : <>
    <label htmlFor="task-title">Task title</label>
    <input id="task-title" required value={title} readOnly={locked}
      aria-invalid={!!fields.title} aria-describedby={fields.title ? 'title-error' : undefined}
      onChange={event => setTitle(event.target.value)} />
    {fields.title && <p id="title-error">{fields.title}</p>}
    <label htmlFor="task-body">Task description</label>
    <textarea id="task-body" required rows={8} value={body} readOnly={locked}
      aria-invalid={!!fields.body} aria-describedby={fields.body ? 'body-error' : undefined}
      onChange={event => setBody(event.target.value)} />
    {fields.body && <p id="body-error">{fields.body}</p>}
    </>}
    {error && <p role="alert">{error}</p>}
    {pending && <p role="status">Creating task and run…</p>}
    <button type="submit" disabled={pending || !project || (!locked && !validSource)}>
      {pending ? 'Creating…' : locked && error ? 'Retry creation' : 'Create run'}
    </button>
  </form>;
}

function ProjectRunSummary({ project }: { project: Project }) {
  const policy = project.policy;
  const commands = policy?.commands as components['schemas']['CommandSpec'][] | undefined;
  return <section aria-label="Run policy summary">
    <h2>Run policy</h2>
    <dl>
      <dt>Base branch</dt><dd>{project.default_branch}</dd>
      <dt>Policy version</dt><dd>{project.policy_version ?? 'Unavailable'}</dd>
      <dt>Runner</dt><dd>{policy?.runner_mode === 'trusted_host' ? 'Trusted host · unsandboxed' : policy?.runner_mode === 'docker' ? 'Docker' : 'Unavailable'}</dd>
      <dt>Required checks</dt><dd>{commands?.filter(item => item.required).map(item => item.name).join(', ') || 'None configured'}</dd>
    </dl>
    {(['planner', 'developer', 'reviewer'] as const).map(role => {
      const model = policy?.[`${role}_model`] as components['schemas']['AgentModelPolicy'] | undefined;
      return <section key={role} aria-label={`${role} policy`}>
        <h3>{role[0].toUpperCase() + role.slice(1)}</h3>
        {model ? <p>{model.provider} / {model.model} · Input {model.max_input_tokens ?? 'unavailable'} tokens · Output {model.max_output_tokens ?? 'unavailable'} tokens · {model.max_tool_calls ?? 'unavailable'} tools · {model.max_duration_seconds ?? 'unavailable'} seconds · Cost limit {model.max_cost_minor ?? 'unavailable'} minor currency units</p> : <p>Model policy unavailable</p>}
      </section>;
    })}
  </section>;
}
