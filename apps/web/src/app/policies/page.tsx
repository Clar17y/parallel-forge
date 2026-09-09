'use client';
import Link from 'next/link';
import { PolicyBrowser } from '@/components/projects/policy-browser';

export default function PoliciesPage() {
  return <><h1>Policies</h1><p>Inspect an immutable version or open the project to create its next version.</p>
    <PolicyBrowser>{(policy, project) => <section>
      <h2>Version {policy.version}</h2><p>Digest: <code>{policy.policy_digest}</code></p>
      <p><Link href={`/projects/${project.id}`}>Create a new policy version</Link></p>
      <pre className="policy-document">{JSON.stringify(policy.document, null, 2)}</pre>
    </section>}</PolicyBrowser>
  </>;
}
