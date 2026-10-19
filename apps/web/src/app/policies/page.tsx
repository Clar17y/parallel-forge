'use client';
import Link from 'next/link';
import { PolicyBrowser } from '@/components/projects/policy-browser';
import { PolicySummary } from '@/components/projects/policy-summary';
import { Disclosure } from '@/components/ui/disclosure';

export default function PoliciesPage() {
  return (
    <>
      <div className="page-title-row">
        <h1>Project policies</h1>
      </div>
      <div className="readable-prose mb-4">
        <p>
          Inspect immutable project policies including runner modes, named commands, sandboxing, and
          budgets. Changes are durably recorded by saving a new policy version from the project page.
        </p>
      </div>
      <PolicyBrowser>
        {(policy, project) => (
          <section className="mt-4">
            <div className="summary-line mb-3">
              <div>
                <h2>
                  {project.name} · Version {policy.version}
                </h2>
              </div>
              <div>
                <Link
                  href={`/projects/${project.id}`}
                  className="button"
                  data-variant="primary"
                >
                  Create next policy version
                </Link>
              </div>
            </div>

            <Disclosure
              className="mb-4"
              summary={
                <div className="summary-line">
                  <span className="summary-title font-mono text-xs">
                    Digest: {policy.policy_digest.slice(0, 24)}…
                  </span>
                  <span className="summary-meta">View full sha256 &amp; document schema</span>
                </div>
              }
            >
              <div className="detail-grid text-xs">
                <div>
                  <dt>Full policy digest</dt>
                  <dd>
                    <code className="break-all">{policy.policy_digest}</code>
                  </dd>
                </div>
                <div>
                  <dt>Document schema</dt>
                  <dd>v{policy.document_schema_version ?? 1}</dd>
                </div>
                <div>
                  <dt>Project ID</dt>
                  <dd>
                    <code>{policy.project_id}</code>
                  </dd>
                </div>
              </div>
            </Disclosure>

            <PolicySummary policy={policy} />
          </section>
        )}
      </PolicyBrowser>
    </>
  );
}
