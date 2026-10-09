'use client';

import { useEffect, useRef, useState, type FormEvent } from 'react';
import { ApiError } from '@/lib/api/client';
import type { components } from '@/lib/api/schema';
import { CommandEditor } from './command-editor';
import { Field, lines } from './form-fields';
import { JevSettings } from './jev-settings';
import { defaultModel, ModelPolicy } from './model-policy';
import styles from './project-policy.module.css';

export type ProjectInput = components['schemas']['ProjectCreateRequest'];
type Command = components['schemas']['CommandSpec'];

interface ManagedCommand {
  id: string;
  command: Command;
  isOpen: boolean;
}

function revealEnclosingDetails(target: HTMLElement) {
  for (let parent = target.parentElement; parent; parent = parent.parentElement) {
    if (parent instanceof HTMLDetailsElement) parent.open = true;
  }
}

export const projectDefaults: ProjectInput = {
  name: '', repository_path: '', github_repository: '', default_branch: 'main',
  runner_mode: 'docker', trusted_project: false, local_remediation_limit: 3, remote_remediation_limit: 3,
  database: { enabled: false, injected_environment_key: 'DATABASE_URL' }, commands: [],
  planner_model: defaultModel, developer_model: defaultModel, reviewer_model: defaultModel,
  allowed_environment_files: [], secret_paths: ['.env', '.env.local'], allowed_merge_methods: ['squash'],
  publication_blocking_severities: ['blocker', 'major'], merge_blocking_severities: ['blocker', 'major'],
};

const starterExamples: Command[] = [
  { kind: 'test', name: 'npm test', argv: ['npm', 'test'], timeout_seconds: 300, required: true, network_enabled: false, environment_keys: [] },
  { kind: 'lint', name: 'npm run lint', argv: ['npm', 'run', 'lint'], timeout_seconds: 300, required: true, network_enabled: false, environment_keys: [] },
  { kind: 'build', name: 'npm run build', argv: ['npm', 'run', 'build'], timeout_seconds: 300, required: true, network_enabled: false, environment_keys: [] },
  { kind: 'test', name: 'pytest', argv: ['python', '-m', 'pytest', '-q'], timeout_seconds: 300, required: true, network_enabled: false, environment_keys: [] },
];

export function ProjectForm({ onSave, initial, policyOnly = false }: {
  onSave: (request: ProjectInput) => Promise<unknown>; initial?: Partial<ProjectInput>; policyOnly?: boolean;
}) {
  const [value, setValue] = useState<ProjectInput>(() => ({ ...projectDefaults, ...initial }));
  const [managedCommands, setManagedCommands] = useState<ManagedCommand[]>(() =>
    (initial?.commands ?? projectDefaults.commands).map((cmd, index) => ({
      id: `cmd-${index}-${crypto.randomUUID()}`,
      command: { ...cmd },
      isOpen: false,
    }))
  );

  const [resourcesOpen, setResourcesOpen] = useState(false);
  const [deliveryOpen, setDeliveryOpen] = useState(false);
  const [jevOpen, setJevOpen] = useState(false);
  const [modelsOpen, setModelsOpen] = useState(false);

  const [pending, setPending] = useState(false);
  const [error, setError] = useState('');
  const [errors, setErrors] = useState<Record<string, string>>({});
  const busy = useRef(false);
  const form = useRef<HTMLFormElement>(null);

  useEffect(() => {
    form.current?.querySelectorAll<HTMLElement>('[aria-invalid="true"]').forEach(revealEnclosingDetails);
  }, [errors]);

  function set<K extends keyof ProjectInput>(key: K, next: ProjectInput[K]) {
    setValue(current => ({ ...current, [key]: next }));
  }

  function nested(prefix: string) {
    return Object.fromEntries(
      Object.entries(errors)
        .filter(([key]) => key.startsWith(`${prefix}.`))
        .map(([key, message]) => [key.slice(prefix.length + 1), message])
    );
  }

  function addStarter(starter: Command) {
    setManagedCommands(current => [
      ...current,
      {
        id: `starter-${crypto.randomUUID()}`,
        command: { ...starter },
        isOpen: true,
      },
    ]);
  }

  function addCustomCommand() {
    setManagedCommands(current => [
      ...current,
      {
        id: `custom-${crypto.randomUUID()}`,
        command: { kind: 'test', name: '', argv: [''], timeout_seconds: 300, required: true, network_enabled: false, environment_keys: [] },
        isOpen: true,
      },
    ]);
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (busy.current) return;
    if (!value.allowed_merge_methods.length) {
      setDeliveryOpen(true);
      setError('Select at least one allowed merge method.');
      return;
    }

    // Auto-reveal any commands with missing required fields
    setManagedCommands(current => current.map(item => {
      const isMissingRequired = !item.command.name?.trim() || !item.command.argv?.[0]?.trim();
      return isMissingRequired ? { ...item, isOpen: true } : item;
    }));

    busy.current = true;
    setPending(true);
    setError('');
    setErrors({});

    try {
      const currentCommands = managedCommands.map(item => item.command);
      const payload = {
        ...value,
        commands: currentCommands.map(command => ({
          ...command,
          environment_keys: lines(command.environment_keys.join('\n')),
        })),
        database: value.database?.enabled ? value.database : { enabled: false },
        allowed_environment_files: lines(value.allowed_environment_files.join('\n')),
        secret_paths: lines(value.secret_paths.join('\n')),
      };
      await onSave(payload as ProjectInput);
    } catch (failure) {
      if (failure instanceof ApiError) {
        const fieldErrors = failure.fields ?? {};
        setErrors(fieldErrors);
        const errKeys = Object.keys(fieldErrors);

        // Auto-reveal affected command disclosures
        setManagedCommands(current => current.map((item, idx) => {
          const hasErr = errKeys.some(k => k.startsWith(`commands.${idx}.`) || k === `commands.${idx}` || k === 'commands');
          return hasErr ? { ...item, isOpen: true } : item;
        }));

        // Auto-reveal affected section disclosures
        if (errKeys.some(k => ['database', 'allowed_environment_files', 'secret_paths'].some(prefix => k === prefix || k.startsWith(`${prefix}.`)))) {
          setResourcesOpen(true);
        }
        if (errKeys.some(k => k === 'allowed_merge_methods' || k === 'local_remediation_limit' || k === 'remote_remediation_limit')) {
          setDeliveryOpen(true);
        }
        if (errKeys.some(k => k.startsWith('jev.') || k === 'jev')) {
          setJevOpen(true);
        }
        if (errKeys.some(k => ['planner_model', 'developer_model', 'reviewer_model'].some(prefix => k === prefix || k.startsWith(`${prefix}.`)))) {
          setModelsOpen(true);
        }
      }
      setError(failure instanceof ApiError && failure.status === 409 ? 'The policy changed. Reload its current version before saving.' : 'Changes could not be saved. Check the fields and try again.');
    } finally {
      busy.current = false;
      setPending(false);
    }
  }

  let sectionCounter = 1;
  const identitySectionNum = !policyOnly ? sectionCounter++ : 0;
  const runnerSectionNum = sectionCounter++;
  const commandsSectionNum = sectionCounter++;
  const resourcesSectionNum = sectionCounter++;
  const deliverySectionNum = sectionCounter++;
  const jevSectionNum = sectionCounter++;
  const modelsSectionNum = sectionCounter++;

  return (
    <form
      ref={form}
      onSubmit={submit}
      onInvalid={event => revealEnclosingDetails(event.target as HTMLElement)}
    >
      <fieldset disabled={pending}>
        <legend>{policyOnly ? 'New policy version' : 'Register project'}</legend>

        {/* Section 1: Repository & identity (excluded in policy-only mode) */}
        {!policyOnly && (
          <section className={styles.setupSection}>
            <div className={styles.setupSectionHeader}>
              <h2 className={styles.setupSectionTitle}>
                {identitySectionNum}. Repository &amp; identity
              </h2>
            </div>
            <p className={styles.sectionGuidance}>
              Configure your local git repository path, GitHub connection, and project details.
            </p>
            <Field
              label="Project name"
              required
              value={value.name}
              onChange={text => set('name', text)}
              error={errors.name}
              hint="e.g. My Parallel Project"
            />
            <Field
              label="Repository path"
              required
              value={value.repository_path}
              onChange={text => set('repository_path', text)}
              error={errors.repository_path}
              hint="Path to existing local Git repository (e.g. C:/code/my-project or /home/user/code/my-project)"
            />
            <Field
              label="GitHub repository"
              required
              value={value.github_repository}
              onChange={text => set('github_repository', text)}
              error={errors.github_repository}
              hint="e.g. owner/repo"
            />
            <Field
              label="Default branch"
              required
              value={value.default_branch}
              onChange={text => set('default_branch', text)}
              error={errors.default_branch}
              hint="e.g. main"
            />
            <Field
              label="Instructions path (optional)"
              value={value.instructions_path ?? ''}
              onChange={text => set('instructions_path', text || null)}
              error={errors.instructions_path}
              hint="Optional markdown instructions file (e.g. AGENTS.md)"
            />
            <p className="meta" style={{ marginTop: '8px' }}>
              Next step after registration: select a subscription profile to configure CLI models and reasoning effort.
            </p>
          </section>
        )}

        {/* Section 2: Runner & sandboxing */}
        <section className={styles.setupSection}>
          <div className={styles.setupSectionHeader}>
            <h2 className={styles.setupSectionTitle}>
              {runnerSectionNum}. Runner &amp; sandboxing
            </h2>
          </div>
          <p className={styles.sectionGuidance}>
            Docker container sandboxing is default and recommended. Trusted-host mode runs unsandboxed directly on this machine.
          </p>
          <label>
            <input
              type="checkbox"
              checked={value.trusted_project}
              onChange={event => setValue(current => ({
                ...current,
                trusted_project: event.target.checked,
                runner_mode: event.target.checked ? current.runner_mode : 'docker'
              }))}
            />
            I trust this project to run commands on this host without a sandbox.
          </label>
          <div style={{ marginTop: '8px' }}>
            <label>
              Runner{' '}
              <select
                value={value.runner_mode}
                onChange={event => set('runner_mode', event.target.value as ProjectInput['runner_mode'])}
              >
                <option value="docker">Docker</option>
                <option value="trusted_host" disabled={!value.trusted_project}>
                  Trusted host · unsandboxed
                </option>
              </select>
            </label>
          </div>
          {errors.runner_mode && <p className="field-error">{errors.runner_mode}</p>}
        </section>

        {/* Section 3: Named commands */}
        <section className={styles.setupSection}>
          <div className={styles.setupSectionHeader}>
            <h2 className={styles.setupSectionTitle}>
              {commandsSectionNum}. Named commands
            </h2>
          </div>
          <p className={styles.sectionGuidance}>
            Choose an executable and ordered arguments. Only these named commands may run.
          </p>

          {/* Starter command examples */}
          <div style={{ margin: '0.75rem 0', display: 'flex', flexWrap: 'wrap', gap: '0.5rem', alignItems: 'center' }}>
            <span style={{ fontSize: '0.875rem', fontWeight: 500 }}>Starter examples:</span>
            {starterExamples.map(example => {
              const isDuplicate = managedCommands.some(
                c => c.command.name.toLowerCase() === example.name.toLowerCase()
              );
              return (
                <button
                  key={example.name}
                  type="button"
                  disabled={isDuplicate}
                  onClick={() => addStarter(example)}
                >
                  {`Add "${example.name}"${isDuplicate ? ' (already added)' : ''}`}
                </button>
              );
            })}
          </div>

          {/* Commands disclosures list */}
          <div className={styles.commandsList}>
            {managedCommands.map((item, index) => {
              const cmd = item.command;
              const argv = Array.isArray(cmd.argv) ? cmd.argv : [];
              return (
                <details
                  key={item.id}
                  data-testid={`command-editor-disclosure-${index}`}
                  open={item.isOpen}
                  onToggle={event => {
                    const open = event.currentTarget.open;
                    setManagedCommands(current =>
                      current.map(c => c.id === item.id ? { ...c, isOpen: open } : c)
                    );
                  }}
                  className={styles.setupSection}
                  style={{ marginBottom: '0.75rem' }}
                >
                  <summary className={styles.commandSummary} style={{ padding: '0.5rem 0', cursor: 'pointer' }}>
                    <strong>Command {index + 1}: {cmd.name || '(unnamed)'}</strong>
                    <span className={`${styles.badge} ${styles.badgeKind}`}>
                      [{cmd.kind || 'unspecified'}]
                    </span>
                    <code className={styles.argvPreview}>
                      {argv.join(' ') || '(no argv)'}
                    </code>
                    <span className={`${styles.badge} ${cmd.required ? styles.badgeRequired : styles.badgeOptional}`}>
                      {cmd.required ? 'Required' : 'Optional'}
                    </span>
                    <span className={`${styles.badge} ${cmd.network_enabled ? styles.badgeNetwork : styles.badgeNoNetwork}`}>
                      {cmd.network_enabled ? 'Network allowed' : 'No network'}
                    </span>
                    <span className={styles.badge}>
                      {cmd.timeout_seconds}s
                    </span>
                  </summary>

                  <div style={{ marginTop: '0.75rem', paddingTop: '0.75rem', borderTop: '1px solid var(--border)' }}>
                    <CommandEditor
                      value={cmd}
                      errors={nested(`commands.${index}`)}
                      onChange={next => {
                        setManagedCommands(current =>
                          current.map(c => c.id === item.id ? { ...c, command: next } : c)
                        );
                      }}
                    />
                    <div style={{ marginTop: '0.75rem' }}>
                      <button
                        type="button"
                        onClick={() => {
                          setManagedCommands(current => current.filter(c => c.id !== item.id));
                        }}
                      >
                        Remove command {index + 1}
                      </button>
                    </div>
                  </div>
                </details>
              );
            })}
          </div>

          {errors.commands && <p className="field-error">{errors.commands}</p>}

          <div style={{ marginTop: '0.75rem' }}>
            <button type="button" onClick={addCustomCommand}>
              Add command
            </button>
          </div>
        </section>

        {/* Section 4: Worktree resources (grouped disclosure) */}
        <section className={styles.setupSection}>
          <details
            data-testid="section-disclosure-worktree-resources"
            open={resourcesOpen}
            onToggle={event => setResourcesOpen(event.currentTarget.open)}
          >
            <summary className={styles.setupSectionTitle} style={{ cursor: 'pointer' }}>
              {resourcesSectionNum}. Worktree resources ·{' '}
              <span style={{ fontSize: '0.9rem', fontWeight: 'normal', color: 'var(--muted-foreground, #666)' }}>
                {value.database?.enabled ? 'PostgreSQL enabled' : 'Database disabled'} ·{' '}
                {value.allowed_environment_files.length} env files ·{' '}
                {value.secret_paths.length} secret paths
              </span>
            </summary>

            <div style={{ marginTop: '1rem' }}>
              <label>
                <input
                  type="checkbox"
                  checked={!!value.database?.enabled}
                  onChange={event => set('database', {
                    ...value.database,
                    enabled: event.target.checked,
                    injected_environment_key: value.database?.injected_environment_key ?? 'DATABASE_URL',
                  })}
                />
                Provision a PostgreSQL database per worktree
              </label>

              {value.database?.enabled && (
                <>
                  <Field
                    label="Administrator secret reference"
                    required
                    pattern="secret://[A-Za-z0-9][A-Za-z0-9._/-]*"
                    value={value.database.admin_url_secret_reference ?? ''}
                    onChange={text => set('database', { ...value.database!, admin_url_secret_reference: text })}
                    error={errors['database.admin_url_secret_reference']}
                  />
                  <Field
                    label="Injected environment key"
                    required
                    pattern="[A-Za-z_][A-Za-z0-9_]*"
                    value={value.database.injected_environment_key}
                    onChange={text => set('database', { ...value.database!, injected_environment_key: text })}
                    error={errors['database.injected_environment_key']}
                  />
                  <p className="meta">
                    Enter a server-side secret reference, never the database password or connection string.
                  </p>
                </>
              )}

              <PathList
                label="Allowed environment files"
                value={value.allowed_environment_files}
                onChange={next => set('allowed_environment_files', next)}
                error={errors.allowed_environment_files}
              />
              <PathList
                label="Secret paths"
                value={value.secret_paths}
                onChange={next => set('secret_paths', next)}
                error={errors.secret_paths}
              />
            </div>
          </details>
        </section>

        {/* Section 5: Delivery & remediation (grouped disclosure) */}
        <section className={styles.setupSection}>
          <details
            data-testid="section-disclosure-delivery-remediation"
            open={deliveryOpen}
            onToggle={event => setDeliveryOpen(event.currentTarget.open)}
          >
            <summary className={styles.setupSectionTitle} style={{ cursor: 'pointer' }}>
              {deliverySectionNum}. Delivery &amp; remediation ·{' '}
              <span style={{ fontSize: '0.9rem', fontWeight: 'normal', color: 'var(--muted-foreground, #666)' }}>
                {value.allowed_merge_methods.join(', ') || 'no merge methods'} · limits: {value.local_remediation_limit} local / {value.remote_remediation_limit} remote
              </span>
            </summary>

            <div style={{ marginTop: '1rem' }}>
              <fieldset>
                <legend>Allowed merge methods</legend>
                {['squash', 'merge', 'rebase'].map(method => (
                  <label key={method} style={{ marginRight: '1rem' }}>
                    <input
                      type="checkbox"
                      checked={value.allowed_merge_methods.includes(method)}
                      onChange={event => set(
                        'allowed_merge_methods',
                        event.target.checked
                          ? [...value.allowed_merge_methods, method]
                          : value.allowed_merge_methods.filter(item => item !== method)
                      )}
                    />
                    {method}
                  </label>
                ))}
              </fieldset>

              <Field
                label="Local remediation limit"
                type="number"
                min={0}
                max={20}
                required
                value={value.local_remediation_limit}
                onChange={text => set('local_remediation_limit', Number(text))}
                error={errors.local_remediation_limit}
              />
              <Field
                label="Remote remediation limit"
                type="number"
                min={0}
                max={20}
                required
                value={value.remote_remediation_limit}
                onChange={text => set('remote_remediation_limit', Number(text))}
                error={errors.remote_remediation_limit}
              />
            </div>
          </details>
        </section>

        {/* Section 6: Jev settings (grouped disclosure) */}
        <section className={styles.setupSection}>
          <details
            data-testid="section-disclosure-jev"
            open={jevOpen}
            onToggle={event => setJevOpen(event.currentTarget.open)}
          >
            <summary className={styles.setupSectionTitle} style={{ cursor: 'pointer' }}>
              {jevSectionNum}. Jev settings ·{' '}
              <span style={{ fontSize: '0.9rem', fontWeight: 'normal', color: 'var(--muted-foreground, #666)' }}>
                {value.jev ? `Configured (${value.jev.mode})` : 'Inherited from profile'}
              </span>
            </summary>

            <div style={{ marginTop: '1rem' }}>
              <JevSettings
                value={value.jev}
                onChange={next => set('jev', next)}
                errors={errors}
                checkboxLabel="Configure Jev for this project"
                policyNotice="Clear this setting to inherit the selected profile's Jev default. Set mode to Off to disable Jev for this project."
              />
            </div>
          </details>
        </section>

        {/* Section 7: API models & budgets (grouped disclosure) */}
        <section className={styles.setupSection}>
          <details
            data-testid="section-disclosure-api-models"
            open={modelsOpen}
            onToggle={event => setModelsOpen(event.currentTarget.open)}
          >
            <summary className={styles.setupSectionTitle} style={{ cursor: 'pointer' }}>
              {modelsSectionNum}. API models &amp; budgets ·{' '}
              <span style={{ fontSize: '0.9rem', fontWeight: 'normal', color: 'var(--muted-foreground, #666)' }}>
                Planner, Developer, Reviewer
              </span>
            </summary>

            <div style={{ marginTop: '1rem' }}>
              <p className="meta" style={{ marginBottom: '1rem' }}>
                Subscription profiles configure CLI models and reasoning. Configure API models, reasoning strength, and budgets here.
              </p>
              {(['planner', 'developer', 'reviewer'] as const).map(role => (
                <ModelPolicy
                  key={role}
                  role={role[0].toUpperCase() + role.slice(1)}
                  value={value[`${role}_model`] ?? defaultModel}
                  onChange={next => set(`${role}_model`, next)}
                  errors={nested(`${role}_model`)}
                />
              ))}
            </div>
          </details>
        </section>
      </fieldset>

      {error && <p role="alert">{error}</p>}
      {!!Object.keys(errors).length && (
        <ul aria-label="Validation errors">
          {Object.entries(errors).map(([path, message]) => (
            <li key={path}>{path}: {message}</li>
          ))}
        </ul>
      )}
      <button disabled={pending} type="submit">
        {pending ? 'Saving…' : policyOnly ? 'Create policy version' : 'Register project'}
      </button>
    </form>
  );
}

function PathList({ label, value, onChange, error }: {
  label: string; value: string[]; onChange: (value: string[]) => void; error?: string;
}) {
  return (
    <Field
      label={label}
      multiline
      value={value.join('\n')}
      onChange={text => onChange(text.split(/\r?\n/))}
      error={error}
    />
  );
}
