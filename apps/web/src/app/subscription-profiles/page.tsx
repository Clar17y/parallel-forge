'use client';

import { LoadingStatus } from '@/components/ui/loading-status';
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

      <nav aria-label="Section navigation" className="filter-chips mb-4">
        <a href="#client-setup" className="button" data-variant="quiet">
          Client health &amp; readiness
        </a>
        <a href="#profile-history" className="button" data-variant="quiet">
          Profile history
        </a>
        <a href="#profile-editor" className="button" data-variant="quiet">
          Create / edit profile
        </a>
      </nav>

      <SubscriptionRuntimeStatus />

      <div id="profile-history" className="mb-4">
        <div className="flex flex-wrap items-center justify-between gap-3 mb-4">
          <Button type="button" disabled={profiles.refreshing} onClick={profiles.refresh}>
            Refresh profile history
          </Button>
        </div>

        {profiles.loading && <LoadingStatus>Loading profile history…</LoadingStatus>}
        {profiles.failed && (
          <p role="alert">
            Profile history unavailable.{' '}
            {profiles.value ? 'Showing the last loaded history; saving is paused until it is verified. ' : ''}
            <Button onClick={profiles.refresh}>Retry</Button>
          </p>
        )}
        {profiles.value && (
          <ProfileList profiles={profiles.value} refresh={profiles.refresh} unverified={profiles.failed} />
        )}
      </div>
    </div>
  );
}
