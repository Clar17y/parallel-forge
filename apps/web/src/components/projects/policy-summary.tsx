'use client';

import type { components } from '@/lib/api/schema';
import styles from './project-policy.module.css';

type PolicyResponse = components['schemas']['ProjectPolicyResponse'];
type CommandSpec = components['schemas']['CommandSpec'];

const knownKeys = new Set([
  'runner_mode',
  'trusted_project',
  'commands',
  'database',
  'allowed_environment_files',
  'secret_paths',
  'allowed_merge_methods',
  'local_remediation_limit',
  'remote_remediation_limit',
  'publication_blocking_severities',
  'merge_blocking_severities',
  'jev',
  'planner_model',
  'developer_model',
  'reviewer_model',
]);

export function PolicySummary({ policy }: { policy: PolicyResponse }) {
  const doc = (policy.document ?? {}) as Record<string, unknown>;
  const commands = Array.isArray(doc.commands) ? (doc.commands as CommandSpec[]) : [];

  const unknownFields = Object.entries(doc).filter(([key]) => !knownKeys.has(key));

  return (
    <div className={styles.policySummary} aria-label="Policy summary">
      {/* Runner & sandboxing */}
      {(doc.runner_mode !== undefined || doc.trusted_project !== undefined) && (
        <section className={styles.section}>
          <h3 className={styles.sectionTitle}>Runner &amp; sandboxing</h3>
          <div className={styles.metaRow}>
            {doc.runner_mode !== undefined && (
              <span>
                Runner: <strong>{String(doc.runner_mode)}</strong>
              </span>
            )}
            {doc.trusted_project !== undefined && (
              <span>
                Host trust: <strong>{doc.trusted_project === true ? 'Trusted' : doc.trusted_project === false ? 'Not trusted' : 'Unspecified'}</strong>
              </span>
            )}
          </div>
        </section>
      )}

      {/* Named commands */}
      <section className={styles.section}>
        <h3 className={styles.sectionTitle}>
          Named commands ({commands.length})
        </h3>
        {commands.length === 0 ? (
          <p className="meta">No named commands configured in this policy version.</p>
        ) : (
          <div className={styles.commandsList}>
            {commands.map((cmd, idx) => {
              const argv = Array.isArray(cmd.argv) ? cmd.argv : [];
              return (
                <details
                  key={`${cmd.name || idx}-${idx}`}
                  data-testid="command-summary-disclosure"
                  className={styles.commandDetails}
                >
                  <summary className={styles.commandSummary}>
                    <strong>{cmd.name || `Command ${idx + 1}`}</strong>
                    <span className={`${styles.badge} ${styles.badgeKind}`}>
                      kind: {cmd.kind || 'unspecified'}
                    </span>
                    <code className={styles.argvPreview}>
                      {argv.join(' ') || '(empty argv)'}
                    </code>
                    {cmd.required === true ? (
                      <span className={`${styles.badge} ${styles.badgeRequired}`}>
                        Required
                      </span>
                    ) : cmd.required === false ? (
                      <span className={`${styles.badge} ${styles.badgeOptional}`}>
                        Optional
                      </span>
                    ) : <span className={styles.badge}>Requirement unspecified</span>}
                    {cmd.network_enabled === true ? (
                      <span className={`${styles.badge} ${styles.badgeNetwork}`}>
                        Network allowed
                      </span>
                    ) : cmd.network_enabled === false ? (
                      <span className={`${styles.badge} ${styles.badgeNoNetwork}`}>
                        No network
                      </span>
                    ) : <span className={styles.badge}>Network unspecified</span>}
                    {cmd.timeout_seconds !== undefined && (
                      <span className={styles.badge}>
                        {cmd.timeout_seconds}s timeout
                      </span>
                    )}
                  </summary>

                  <div className={styles.commandBody}>
                    <p><strong>Exact argument order:</strong> Quoted values preserve spaces; escapes show tabs and quotes.</p>
                    <ol className={styles.argvList}>
                      {argv.map((arg, argIdx) => (
                        <li key={argIdx} className={styles.argvItem}>
                          <span className={styles.argIndex}>Arg {argIdx}:</span>
                          <code>{JSON.stringify(arg)}</code>
                        </li>
                      ))}
                    </ol>
                    {Array.isArray(cmd.environment_keys) && cmd.environment_keys.length > 0 && (
                      <p style={{ marginTop: '8px' }}>
                        Allowed environment keys:{' '}
                        <code>{cmd.environment_keys.join(', ')}</code>
                      </p>
                    )}
                  </div>
                </details>
              );
            })}
          </div>
        )}
      </section>

      {/* Worktree resources */}
      {(doc.database !== undefined ||
        doc.allowed_environment_files !== undefined ||
        doc.secret_paths !== undefined) && (
        <section className={styles.section}>
          <h3 className={styles.sectionTitle}>Worktree resources</h3>
          <div className={styles.metaRow}>
            {doc.database !== undefined && typeof doc.database === 'object' && doc.database !== null && (
              <span>
                PostgreSQL database:{' '}
                <strong>{(doc.database as { enabled?: boolean }).enabled === true ? 'Enabled' : (doc.database as { enabled?: boolean }).enabled === false ? 'Disabled' : 'Unspecified'}</strong>
                {(doc.database as { enabled?: boolean }).enabled && (doc.database as { injected_environment_key?: string }).injected_environment_key && (
                  <> (key: <code>{(doc.database as { injected_environment_key?: string }).injected_environment_key}</code>)</>
                )}
              </span>
            )}
            {Array.isArray(doc.allowed_environment_files) && (
              <span>
                Allowed environment files: <strong>{doc.allowed_environment_files.length}</strong>
                {doc.allowed_environment_files.length > 0 ? ` (${doc.allowed_environment_files.join(', ')})` : ''}
              </span>
            )}
            {Array.isArray(doc.secret_paths) && (
              <span>
                Secret paths: <strong>{doc.secret_paths.length}</strong>
                {doc.secret_paths.length > 0 ? ` (${doc.secret_paths.join(', ')})` : ''}
              </span>
            )}
          </div>
        </section>
      )}

      {/* Delivery and remediation */}
      {(doc.allowed_merge_methods !== undefined ||
        doc.local_remediation_limit !== undefined ||
        doc.remote_remediation_limit !== undefined ||
        doc.publication_blocking_severities !== undefined ||
        doc.merge_blocking_severities !== undefined) && (
        <section className={styles.section}>
          <h3 className={styles.sectionTitle}>Delivery &amp; remediation</h3>
          <div className={styles.metaRow}>
            {Array.isArray(doc.allowed_merge_methods) && (
              <span>
                Merge methods: <strong>{doc.allowed_merge_methods.join(', ')}</strong>
              </span>
            )}
            {doc.local_remediation_limit !== undefined && (
              <span>
                Local remediation limit: <strong>{String(doc.local_remediation_limit)}</strong>
              </span>
            )}
            {doc.remote_remediation_limit !== undefined && (
              <span>
                Remote remediation limit: <strong>{String(doc.remote_remediation_limit)}</strong>
              </span>
            )}
            {([
              ['publication_blocking_severities', 'Publication blocking severities'],
              ['merge_blocking_severities', 'Merge blocking severities'],
            ] as const).map(([key, label]) => {
              const severities = doc[key];
              return <span key={key}>
                {label}: <strong>{Array.isArray(severities)
                  ? severities.length ? severities.join(', ') : 'None'
                  : 'Unspecified'}</strong>
              </span>;
            })}
          </div>
        </section>
      )}

      {/* Jev settings */}
      {doc.jev !== undefined && typeof doc.jev === 'object' && doc.jev !== null && (
        <section className={styles.section}>
          <h3 className={styles.sectionTitle}>Jev settings</h3>
          <div className={styles.metaRow}>
            <span>
              Mode: <strong>{String((doc.jev as { mode?: string }).mode ?? 'unspecified')}</strong>
            </span>
            {(doc.jev as { model?: string }).model && (
              <span>
                Model: <code>{(doc.jev as { model?: string }).model}</code>
              </span>
            )}
            {(doc.jev as { allow_remote?: boolean }).allow_remote !== undefined && (
              <span>
                Remote processing: <strong>{(doc.jev as { allow_remote?: boolean }).allow_remote ? 'Allowed' : 'Local only'}</strong>
              </span>
            )}
          </div>
        </section>
      )}

      {/* API models */}
      {(doc.planner_model !== undefined ||
        doc.developer_model !== undefined ||
        doc.reviewer_model !== undefined) && (
        <section className={styles.section}>
          <h3 className={styles.sectionTitle}>API models &amp; budgets</h3>
          <div className={styles.commandsList}>
            {(['planner', 'developer', 'reviewer'] as const).map(role => {
              const setting = doc[`${role}_model`];
              if (!setting || typeof setting !== 'object' || Array.isArray(setting)) return null;
              const model = setting as Record<string, unknown>;
              return <details key={role} className={styles.commandDetails}>
                <summary className={styles.commandSummary}>
                  <strong>{role[0].toUpperCase() + role.slice(1)}</strong>
                  <code>{String(model.model ?? 'Unspecified model')}</code>
                  <span>Reasoning: {String(model.reasoning_effort ?? 'Automatic (provider default)')}</span>
                </summary>
                <dl>
                  {([
                    ['provider', 'Provider'], ['max_input_tokens', 'Input token limit'],
                    ['max_output_tokens', 'Output token limit'], ['max_tool_calls', 'Tool call limit'],
                    ['max_duration_seconds', 'Duration limit (seconds)'], ['max_cost_minor', 'Cost limit (minor currency units)'],
                  ] as const).map(([key, label]) => <div key={key}>
                    <dt>{label}</dt><dd>{String(model[key] ?? 'Unspecified')}</dd>
                  </div>)}
                </dl>
              </details>;
            })}
          </div>
        </section>
      )}

      {/* Unknown or legacy fields */}
      {unknownFields.length > 0 && (
        <section className={styles.section}>
          <h3 className={styles.sectionTitle}>Additional policy settings</h3>
          <div className={styles.metaRow}>
            {unknownFields.map(([key, val]) => (
              <span key={key}>
                <strong>{key}</strong>: <code>{typeof val === 'object' ? JSON.stringify(val) : String(val)}</code>
              </span>
            ))}
          </div>
        </section>
      )}

      {/* Raw document disclosure */}
      <details data-testid="raw-document-disclosure" className={styles.rawDisclosure}>
        <summary className={styles.rawSummary}>Raw policy document</summary>
        <pre className="policy-document" style={{ marginTop: '0.75rem', overflowX: 'auto' }}>
          {JSON.stringify(policy, null, 2)}
        </pre>
      </details>
    </div>
  );
}
