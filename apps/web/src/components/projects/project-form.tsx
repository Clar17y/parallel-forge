'use client';

import { useRef, useState, type FormEvent } from 'react';
import { ApiError } from '@/lib/api/client';
import type { components } from '@/lib/api/schema';
import { CommandEditor } from './command-editor';
import { Field, lines } from './form-fields';
import { defaultModel, ModelPolicy } from './model-policy';

export type ProjectInput = components['schemas']['ProjectCreateRequest'];
export const projectDefaults: ProjectInput = {
  name: '', repository_path: '', github_repository: '', default_branch: 'main',
  runner_mode: 'docker', trusted_project: false, local_remediation_limit: 3, remote_remediation_limit: 3,
  database: { enabled: false, injected_environment_key: 'DATABASE_URL' }, commands: [],
  planner_model: defaultModel, developer_model: defaultModel, reviewer_model: defaultModel,
  allowed_environment_files: [], secret_paths: ['.env', '.env.local'], allowed_merge_methods: ['squash'],
  publication_blocking_severities: ['blocker', 'major'], merge_blocking_severities: ['blocker', 'major'],
};

export function ProjectForm({ onSave, initial, policyOnly = false }: {
  onSave: (request: ProjectInput) => Promise<unknown>; initial?: Partial<ProjectInput>; policyOnly?: boolean;
}) {
  const [value, setValue] = useState<ProjectInput>(() => ({ ...projectDefaults, ...initial }));
  const [pending, setPending] = useState(false);
  const [error, setError] = useState('');
  const [errors, setErrors] = useState<Record<string, string>>({});
  const busy = useRef(false);
  function set<K extends keyof ProjectInput>(key: K, next: ProjectInput[K]) { setValue(current => ({ ...current, [key]: next })); }
  function nested(prefix: string) { return Object.fromEntries(Object.entries(errors).filter(([key]) => key.startsWith(`${prefix}.`)).map(([key, message]) => [key.slice(prefix.length + 1), message])); }
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (busy.current) return;
    if (!value.allowed_merge_methods.length) { setError('Select at least one allowed merge method.'); return; }
    busy.current = true; setPending(true); setError(''); setErrors({});
    try {
      const payload = { ...value, database: value.database?.enabled ? value.database : { enabled: false },
        allowed_environment_files: lines(value.allowed_environment_files.join('\n')),
        secret_paths: lines(value.secret_paths.join('\n')),
        commands: value.commands.map(command => ({ ...command, environment_keys: lines(command.environment_keys.join('\n')) })),
      };
      await onSave(payload as ProjectInput);
    } catch (failure) {
      if (failure instanceof ApiError) setErrors(failure.fields);
      setError(failure instanceof ApiError && failure.status === 409 ? 'The policy changed. Reload its current version before saving.' : 'Changes could not be saved. Check the fields and try again.');
    } finally { busy.current = false; setPending(false); }
  }
  return <form onSubmit={submit}>
    <fieldset disabled={pending}><legend>{policyOnly ? 'New policy version' : 'Register project'}</legend>
      {!policyOnly && <>
        {([['name', 'Project name'], ['repository_path', 'Repository path'], ['github_repository', 'GitHub repository'], ['default_branch', 'Default branch']] as const).map(([key, label]) =>
          <Field key={key} label={label} required value={value[key]} onChange={text => set(key, text)} error={errors[key]} />)}
        <Field label="Instructions path (optional)" value={value.instructions_path ?? ''} onChange={text => set('instructions_path', text || null)} error={errors.instructions_path} />
      </>}
      <label><input type="checkbox" checked={value.trusted_project} onChange={event => setValue(current => ({ ...current, trusted_project: event.target.checked, runner_mode: event.target.checked ? current.runner_mode : 'docker' }))} />I trust this project to run commands on this host without a sandbox.</label>
      <label>Runner <select value={value.runner_mode} onChange={event => set('runner_mode', event.target.value as ProjectInput['runner_mode'])}>
        <option value="docker">Docker</option><option value="trusted_host" disabled={!value.trusted_project}>Trusted host · unsandboxed</option>
      </select></label>
      {errors.runner_mode && <p>{errors.runner_mode}</p>}
      <h2>Named commands</h2><p>Choose an executable and ordered arguments. Only these named commands may run.</p>
      {value.commands.map((command, index) => <fieldset key={index}><legend>Command {index + 1}</legend>
        <CommandEditor value={command} errors={nested(`commands.${index}`)} onChange={next => set('commands', value.commands.map((item, offset) => offset === index ? next : item))} />
        <button type="button" onClick={() => set('commands', value.commands.filter((_, offset) => offset !== index))}>Remove command {index + 1}</button>
      </fieldset>)}
      {errors.commands && <p>{errors.commands}</p>}
      <button type="button" onClick={() => set('commands', [...value.commands, { kind: 'test', name: '', argv: [''], timeout_seconds: 300, required: true, network_enabled: false, environment_keys: [] }])}>Add command</button>
      <h2>Worktree resources</h2>
      <label><input type="checkbox" checked={!!value.database?.enabled} onChange={event => set('database', { ...value.database, enabled: event.target.checked, injected_environment_key: value.database?.injected_environment_key ?? 'DATABASE_URL' })} />Provision a PostgreSQL database per worktree</label>
      {value.database?.enabled && <>
        <Field label="Administrator secret reference" required pattern="secret://[A-Za-z0-9][A-Za-z0-9._/-]*" value={value.database.admin_url_secret_reference ?? ''} onChange={text => set('database', { ...value.database!, admin_url_secret_reference: text })} error={errors['database.admin_url_secret_reference']} />
        <Field label="Injected environment key" required pattern="[A-Za-z_][A-Za-z0-9_]*" value={value.database.injected_environment_key} onChange={text => set('database', { ...value.database!, injected_environment_key: text })} error={errors['database.injected_environment_key']} />
        <p>Enter a server-side secret reference, never the database password or connection string.</p>
      </>}
      <PathList label="Allowed environment files" value={value.allowed_environment_files} onChange={next => set('allowed_environment_files', next)} error={errors.allowed_environment_files} />
      <PathList label="Secret paths" value={value.secret_paths} onChange={next => set('secret_paths', next)} error={errors.secret_paths} />
      <fieldset><legend>Allowed merge methods</legend>{['squash', 'merge', 'rebase'].map(method => <label key={method}>
        <input type="checkbox" checked={value.allowed_merge_methods.includes(method)} onChange={event => set('allowed_merge_methods', event.target.checked ? [...value.allowed_merge_methods, method] : value.allowed_merge_methods.filter(item => item !== method))} />{method}
      </label>)}</fieldset>
      <Field label="Local remediation limit" type="number" min={0} max={20} required value={value.local_remediation_limit} onChange={text => set('local_remediation_limit', Number(text))} error={errors.local_remediation_limit} />
      <Field label="Remote remediation limit" type="number" min={0} max={20} required value={value.remote_remediation_limit} onChange={text => set('remote_remediation_limit', Number(text))} error={errors.remote_remediation_limit} />
      {(['planner', 'developer', 'reviewer'] as const).map(role => <ModelPolicy key={role} role={role[0].toUpperCase() + role.slice(1)} value={value[`${role}_model`] ?? defaultModel} onChange={next => set(`${role}_model`, next)} errors={nested(`${role}_model`)} />)}
    </fieldset>
    {error && <p role="alert">{error}</p>}
    {!!Object.keys(errors).length && <ul aria-label="Validation errors">{Object.entries(errors).map(([path, message]) => <li key={path}>{path}: {message}</li>)}</ul>}
    <button disabled={pending} type="submit">{pending ? 'Saving…' : policyOnly ? 'Create policy version' : 'Register project'}</button>
  </form>;
}

function PathList({ label, value, onChange, error }: { label: string; value: string[]; onChange: (value: string[]) => void; error?: string }) {
  return <Field label={label} multiline value={value.join('\n')} onChange={text => onChange(text.split(/\r?\n/))} error={error} />;
}
