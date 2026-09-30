'use client';

import { useEffect, useRef, useState, type FormEvent } from 'react';
import { ApiError, api, mutate } from '@/lib/api/client';
import type { components } from '@/lib/api/schema';
import { Button } from '@/components/ui/button';
import { ProviderBadge, getProviderCue } from '@/components/ui/provider-badge';

type Project = components['schemas']['ProjectResponse'];
type TaskInput = components['schemas']['TaskCreateRequest'];
type IssueInput = { project_id: string; issue_number: number };
type Profile = components['schemas']['ProfileResponse'];
type Attempt = { endpoint: '/tasks' | '/tasks/import-github'; payload: TaskInput | IssueInput; taskKey: string; runKey: string; taskId?: string; profileId?: string; profileVersion?: number; correctionAllowed?: boolean };

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
  const [profiles, setProfiles] = useState<Profile[]>([]);
  const [profileChoice, setProfileChoice] = useState('default');
  const [profilesLoading, setProfilesLoading] = useState(true);
  const [correctionAllowed, setCorrectionAllowed] = useState(false);
  const attempt = useRef<Attempt | null>(null);
  const busy = useRef(false);
  const project = projects.find(item => item.id === projectId);
  const choice = profiles.find(item => `${item.profile_id}:${item.version}` === profileChoice);
  useEffect(() => {
    let active = true;
    void api<Profile[]>('/subscription-profiles').then(values => {
      if (active) setProfiles(values ?? []);
    }).catch(() => {
      if (active) setProfiles([]);
    }).finally(() => { if (active) setProfilesLoading(false); });
    return () => { active = false; };
  }, []);
  const importing = source === 'github' && project?.issue_import_available === true;
  const validSource = importing
    ? /^[1-9][0-9]*$/.test(issueNumber) && Number.isSafeInteger(Number(issueNumber))
    : !!title.trim() && !!body.trim();

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
      ...(choice ? { profileId: choice.profile_id, profileVersion: choice.version } : {}),
    };
    const current = attempt.current;
    if (current.correctionAllowed) {
      setCorrectionAllowed(false);
      current.runKey = crypto.randomUUID();
      current.profileId = choice?.profile_id;
      current.profileVersion = choice?.version;
      current.correctionAllowed = false;
    }
    try {
      if (!current.taskId) {
        const task = await mutate<components['schemas']['TaskResponse']>(current.endpoint, current.payload,
          { idempotencyKey: current.taskKey });
        if (!task?.id) throw new Error('Task response unavailable');
        current.taskId = task.id;
      }
      const run = await mutate<components['schemas']['RunResponse']>('/runs', {
        task_id: current.taskId,
        ...(current.profileId ? { profile_id: current.profileId, profile_version: current.profileVersion } : {}),
      },
        { idempotencyKey: current.runKey });
      if (!run?.id) throw new Error('Run response unavailable');
      onCreated(run.id);
    } catch (failure) {
      if (failure instanceof ApiError && failure.status === 422 && !current.taskId) {
        setFields(failure.fields);
        attempt.current = null;
        setLocked(false);
        setError('Check the task fields and try again.');
      } else if (failure instanceof ApiError && failure.status === 422 && current.taskId) {
        current.correctionAllowed = true;
        setCorrectionAllowed(true);
        setError(current.profileId
          ? 'The run was rejected. Choose another profile or use the project default, then retry.'
          : 'The run was rejected. You can choose a profile override and retry.');
      } else {
        setError('Creation could not be confirmed. Retry to recover the same task and run.');
      }
    } finally {
      busy.current = false;
      setPending(false);
    }
  }

  if (!projects.length) return <p>Register a project before creating a run.</p>;

  return (
    <form onSubmit={submit} aria-label="New run" className="new-run-form">
      <div className="new-run-layout">
        <div className="new-run-primary">
          <section className="panel" aria-labelledby="task-details-heading">
            <h2 id="task-details-heading">Task details</h2>
            <div className="form-field">
              <label htmlFor="run-project">Project</label>
              <select
                id="run-project"
                value={projectId}
                disabled={locked || pending}
                aria-invalid={!!fields.project_id}
                aria-describedby={fields.project_id ? 'project-error' : undefined}
                onChange={event => {
                  setProjectId(event.target.value);
                  setSource('text');
                  setProfileChoice('default');
                }}
              >
                {projects.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}
              </select>
              {fields.project_id && <p id="project-error" className="field-error">{fields.project_id}</p>}
              <span className="field-hint">Target repository and project policy enforcing run constraints.</span>
            </div>

            <div className="form-field">
              <label htmlFor="run-profile">Subscription profile</label>
              <select id="run-profile" value={profileChoice} disabled={pending || profilesLoading || (locked && !correctionAllowed)}
                onChange={event => setProfileChoice(event.target.value)}>
                <option value="default">Project default</option>
                {profiles.map(profile => <option key={`${profile.profile_id}:${profile.version}`} value={`${profile.profile_id}:${profile.version}`}>
                  Version {profile.version} · {profile.profile_id}
                </option>)}
              </select>
              <span className="field-hint">
                {profilesLoading ? 'Loading immutable profile versions…' : choice
                  ? `Override: ${choice.preferences.map(profilePreferenceLabel).join(', ')}`
                  : 'Uses the project default profile.'}
              </span>
            </div>

            {project?.issue_import_available === true && (
              <div className="form-field">
                <label htmlFor="task-source">Task source</label>
                <select
                  id="task-source"
                  value={source}
                  disabled={locked || pending}
                  onChange={event => setSource(event.target.value as 'text' | 'github')}
                >
                  <option value="text">Write a task</option>
                  <option value="github">Import GitHub issue</option>
                </select>
                <span className="field-hint">Choose whether to author a manual task description or import from GitHub.</span>
              </div>
            )}

            {importing ? (
              <div className="github-import-fields">
                <p className="meta">Import from {project.github_repository}. Issue content is untrusted task input.</p>
                <div className="form-field">
                  <label htmlFor="issue-number">GitHub issue number</label>
                  <input
                    id="issue-number"
                    type="number"
                    min="1"
                    max={Number.MAX_SAFE_INTEGER}
                    step="1"
                    required
                    readOnly={locked}
                    value={issueNumber}
                    onChange={event => setIssueNumber(event.target.value)}
                    aria-invalid={!!fields.issue_number}
                    aria-describedby={fields.issue_number ? 'issue-error' : undefined}
                    placeholder="e.g. 42"
                  />
                  {fields.issue_number && <p id="issue-error" className="field-error">{fields.issue_number}</p>}
                  <span className="field-hint">Numerical issue ID within {project.github_repository}.</span>
                </div>
              </div>
            ) : (
              <>
                <div className="form-field">
                  <label htmlFor="task-title">Task title</label>
                  <input
                    id="task-title"
                    required
                    value={title}
                    readOnly={locked}
                    aria-invalid={!!fields.title}
                    aria-describedby={fields.title ? 'title-error' : undefined}
                    onChange={event => setTitle(event.target.value)}
                    placeholder="Brief summary of the desired outcome"
                  />
                  {fields.title && <p id="title-error" className="field-error">{fields.title}</p>}
                  <span className="field-hint">State the bounded outcome or fix clearly.</span>
                </div>
                <div className="form-field">
                  <label htmlFor="task-body">Task description</label>
                  <textarea
                    id="task-body"
                    required
                    rows={8}
                    value={body}
                    readOnly={locked}
                    aria-invalid={!!fields.body}
                    aria-describedby={fields.body ? 'body-error' : undefined}
                    onChange={event => setBody(event.target.value)}
                    placeholder="Provide full context, acceptance criteria, relevant file paths, and verification expectations..."
                  />
                  {fields.body && <p id="body-error" className="field-error">{fields.body}</p>}
                  <span className="field-hint">Complete task specification and constraints for planning and execution.</span>
                </div>
              </>
            )}

            <div className="form-actions">
              {error && <p role="alert" className="field-error">{error}</p>}
              {pending && <p role="status" className="meta">Creating task and run…</p>}
              <Button
                type="submit"
                variant="primary"
                disabled={pending || !project || (!locked && !validSource)}
              >
                {pending ? 'Creating…' : (locked && error) || correctionAllowed ? 'Retry creation' : 'Create run'}
              </Button>
            </div>
          </section>
        </div>

        <aside className="new-run-aside">
          {project && <ProjectRunSummary project={project} />}
        </aside>
      </div>
    </form>
  );
}

function profilePreferenceLabel(value: Record<string, unknown>): string {
  const route = value.preferred_route;
  const purpose = typeof value.purpose === 'string' ? value.purpose : 'role';
  if (!route || typeof route !== 'object') return purpose;
  const details = route as Record<string, unknown>;
  const provider = typeof details.provider === 'string' ? details.provider : 'provider unknown';
  const model = typeof details.model === 'string' ? details.model : 'model unknown';
  return `${purpose} ${provider}/${model}`;
}

function ProjectRunSummary({ project }: { project: Project }) {
  const policy = project.policy;
  const commands = policy?.commands as components['schemas']['CommandSpec'][] | undefined;
  const roles = [
    { key: 'planner', name: 'Planner' },
    { key: 'developer', name: 'Developer' },
    { key: 'reviewer', name: 'Reviewer' },
  ] as const;

  return (
    <section aria-label="Run policy summary" className="panel">
      <h2>Run policy</h2>
      <p className="meta">Controlled execution policy for {project.name}</p>
      <dl className="key-values" style={{ marginBottom: '16px' }}>
        <div>
          <dt>Base branch</dt>
          <dd><code>{project.default_branch}</code></dd>
        </div>
        <div>
          <dt>Policy version</dt>
          <dd>{project.policy_version ?? 'Unavailable'}</dd>
        </div>
        <div>
          <dt>Runner</dt>
          <dd>{policy?.runner_mode === 'trusted_host' ? 'Trusted host · unsandboxed' : policy?.runner_mode === 'docker' ? 'Docker' : 'Unavailable'}</dd>
        </div>
        <div>
          <dt>Required checks</dt>
          <dd>{commands?.filter(item => item.required).map(item => item.name).join(', ') || 'None configured'}</dd>
        </div>
      </dl>

      <h3 style={{ marginTop: '16px', marginBottom: '8px', fontSize: '0.8125rem', textTransform: 'uppercase', letterSpacing: '0.06em', color: 'var(--muted)' }}>
        Assigned Agent Roles
      </h3>
      {roles.map(({ key, name }) => {
        const model = policy?.[`${key}_model` as keyof typeof policy] as components['schemas']['AgentModelPolicy'] | undefined;
        const cue = getProviderCue(model?.provider);
        return (
          <section
            key={key}
            aria-label={`${key} policy`}
            className="role-card card-provider"
            data-provider={cue.tone}
          >
            <div className="role-card-header">
              <h4>{name}</h4>
              <ProviderBadge provider={model?.provider} />
            </div>
            {model ? (
              <>
                <div className="role-card-model">{model.model}</div>
                <div className="role-budget-grid">
                  <div className="role-budget-item">
                    <span className="role-budget-label">Tokens</span>
                    <span className="role-budget-value">
                      {model.max_input_tokens?.toLocaleString() ?? '—'} in / {model.max_output_tokens?.toLocaleString() ?? '—'} out
                    </span>
                  </div>
                  <div className="role-budget-item">
                    <span className="role-budget-label">Tool calls</span>
                    <span className="role-budget-value">{model.max_tool_calls ?? '—'}</span>
                  </div>
                  <div className="role-budget-item">
                    <span className="role-budget-label">Max time</span>
                    <span className="role-budget-value">{model.max_duration_seconds ? `${model.max_duration_seconds}s` : '—'}</span>
                  </div>
                  <div className="role-budget-item">
                    <span className="role-budget-label">Cost limit</span>
                    <span className="role-budget-value">{model.max_cost_minor != null ? `${model.max_cost_minor} minor units` : '—'}</span>
                  </div>
                </div>
                <details className="agent-policy-details">
                  <summary>All policy limits</summary>
                  <dl className="key-values">
                    <div><dt>Provider</dt><dd>{model.provider}</dd></div>
                    <div><dt>Model</dt><dd>{model.model}</dd></div>
                    <div><dt>Max input tokens</dt><dd>{model.max_input_tokens ?? 'unavailable'}</dd></div>
                    <div><dt>Max output tokens</dt><dd>{model.max_output_tokens ?? 'unavailable'}</dd></div>
                    <div><dt>Max tool calls</dt><dd>{model.max_tool_calls ?? 'unavailable'}</dd></div>
                    <div><dt>Max duration</dt><dd>{model.max_duration_seconds ? `${model.max_duration_seconds} seconds` : 'unavailable'}</dd></div>
                    <div><dt>Cost limit</dt><dd>{model.max_cost_minor != null ? `${model.max_cost_minor} minor currency units` : 'unavailable'}</dd></div>
                  </dl>
                </details>
              </>
            ) : (
              <p className="meta">Model policy unavailable</p>
            )}
          </section>
        );
      })}
    </section>
  );
}
