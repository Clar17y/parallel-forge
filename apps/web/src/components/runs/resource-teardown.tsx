'use client';
import { useState } from 'react';
import type { components } from '@/lib/api/schema';

export function ResourceTeardown({ resource, disabled, onConfirm }: {
  resource: components['schemas']['ResourceSection']; disabled: boolean; onConfirm: (deleteBranch: boolean) => void;
}) {
  const [identity, setIdentity] = useState('');
  const [reviewed, setReviewed] = useState(false);
  const [deleteBranch, setDeleteBranch] = useState(false);
  const [branchConfirmation, setBranchConfirmation] = useState('');
  return <>
    <dl><dt>Worktree</dt><dd>{resource.worktree_path ?? 'Already absent'}</dd>
      <dt>Branch</dt><dd>{resource.branch_removed ? 'Removal recorded for' : deleteBranch ? 'Delete' : 'Keep'} branch {resource.branch_name ?? 'Not recorded'}</dd></dl>
    {resource.database_state === 'DISABLED' ? <p>Database: Not configured</p> : <dl>
      <dt>Database state</dt><dd>{resource.database_state}</dd>
      <dt>Database</dt><dd>{resource.database_name ?? 'Already absent'}</dd>
      <dt>Database role</dt><dd>{resource.database_role ?? 'Already absent'}</dd>
    </dl>}
    <p>Exact resource identity: <code>{resource.teardown_confirmation}</code></p>
    {reviewed ? <>
      <p>Confirm removal of these recorded local resources. Local worktree files and any configured run database will be removed. Stored run evidence remains. {resource.branch_removed ? 'Branch removal is already recorded.' : deleteBranch ? 'The confirmed local branch will also be deleted.' : 'The branch remains.'}</p>
      <button disabled={disabled} onClick={() => onConfirm(deleteBranch)}>Confirm remove resources</button>
    </> : <>
      <p>Preserve any local work you need before removing these resources.</p>
      <label>Resource identity confirmation<input autoComplete="off" spellCheck={false} value={identity}
        disabled={disabled} onChange={event => setIdentity(event.target.value)} /></label>
      {resource.branch_name && !resource.branch_removed && <>
        <label><input type="checkbox" checked={deleteBranch} disabled={disabled}
          onChange={event => { setDeleteBranch(event.target.checked); setBranchConfirmation(''); }} />Also delete the branch</label>
        {deleteBranch && <label>Branch name confirmation<input autoComplete="off" spellCheck={false}
          value={branchConfirmation} disabled={disabled} onChange={event => setBranchConfirmation(event.target.value)} /></label>}
      </>}
      <button disabled={disabled || identity !== resource.teardown_confirmation || (deleteBranch && branchConfirmation !== resource.branch_name)}
        onClick={() => setReviewed(true)}>Review resource removal</button>
    </>}
  </>;
}
