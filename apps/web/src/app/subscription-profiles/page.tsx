'use client';

import { useApi } from '@/hooks/use-api';
import type { components } from '@/lib/api/schema';
import { ProfileList } from '@/components/subscription-profiles/profile-list';
import { SubscriptionRuntimeStatus } from '@/components/subscription-profiles/runtime-status';
import { Button } from '@/components/ui/button';

export default function SubscriptionProfilesPage() {
  const profiles = useApi<components['schemas']['ProfileResponse'][]>('/subscription-profiles', {
    keepPreviousOnRefresh: true,
    keepPreviousOnError: true,
  });
  return (
    <div className="profiles-page">
      <header className="page-header">
        <h1>Subscription profiles</h1>
        <p className="page-description">
          Manage versioned requested routes and explicit fallbacks for future runs.
        </p>
      </header>

      <details><summary>Worker and client reports</summary><SubscriptionRuntimeStatus /></details>

      <Button type="button" disabled={profiles.refreshing} onClick={profiles.refresh}>Refresh profile history</Button>

      {profiles.loading && <p role="status">Loading profile history…</p>}
      {profiles.failed && (
        <p role="alert">
          Profile history unavailable. {profiles.value ? 'Showing the last loaded history; saving is paused until it is verified. ' : ''}
          <Button onClick={profiles.refresh}>Retry</Button>
        </p>
      )}
      {profiles.value && <ProfileList profiles={profiles.value} refresh={profiles.refresh} unverified={profiles.failed} />}
    </div>
  );
}
