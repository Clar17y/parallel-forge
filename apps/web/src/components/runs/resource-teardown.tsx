'use client';
import { useState } from 'react';
import type { components } from '@/lib/api/schema';

export function ResourceTeardown({ resource, disabled, onConfirm }: {
  resource: components['schemas']['ResourceSection']; disabled: boolean; onConfirm: () => void;
}) {
  const [identity, setIdentity] = useState('');
  const [reviewed, setReviewed] = useState(false);
  return <>
    <dl><dt>Worktree</dt><dd>{resource.worktree_path ?? 'Already absent'}</dd>
      <dt>Branch</dt><dd>Keep branch {resource.branch_name ?? 'Not recorded'}</dd></dl>
    {resource.database_state === 'DISABLED' ? <p>Database: Not configured</p> : <dl>
      <dt>Database state</dt><dd>{resource.database_state}</dd>
      <dt>Database</dt><dd>{resource.database_name ?? 'Already absent'}</dd>
      <dt>Database role</dt><dd>{resource.database_role ?? 'Already absent'}</dd>
    </dl>}
    <p>Exact resource identity: <code>{resource.teardown_confirmation}</code></p>
    {reviewed ? <>
      <p>Confirm removal of these recorded local resources. Local worktree files and any configured run database will be removed. Stored run evidence and the branch remain.</p>
      <button disabled={disabled} onClick={onConfirm}>Confirm remove resources</button>
    </> : <>
      <p>Preserve any local work you need before removing these resources.</p>
      <label>Resource identity confirmation<input autoComplete="off" spellCheck={false} value={identity}
        disabled={disabled} onChange={event => setIdentity(event.target.value)} /></label>
      <button disabled={disabled || identity !== resource.teardown_confirmation}
        onClick={() => setReviewed(true)}>Review resource removal</button>
    </>}
  </>;
}
