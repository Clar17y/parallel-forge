'use client';
import { useApi } from '@/hooks/use-api';
import type { components } from '@/lib/api/schema';

export default function ToolsPage() {
  const permissions = useApi<components['schemas']['ListPage_PermissionItem_']>('/tool-permissions?limit=100');
  return <><h1>Tool permissions</h1><p>These capabilities are assigned by the Forge server. Project command and runner limits further constrain each invocation.</p>
    <p>Docker isolates commands by default. Trusted-host projects execute without a sandbox. Change project settings through a new policy version.</p>
    {permissions.loading && <p role="status">Loading permissions…</p>}
    {permissions.failed && <p role="alert">Permissions unavailable. <button onClick={permissions.refresh}>Retry</button></p>}
    {permissions.value && <ul>{permissions.value.items.map(item => <li key={item.role}><h2>{item.role}</h2><ul>{item.tools.map(tool => <li key={tool}><code>{tool}</code></li>)}</ul></li>)}</ul>}
  </>;
}
