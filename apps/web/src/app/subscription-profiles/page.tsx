'use client';

import { useApi } from '@/hooks/use-api';
import type { components } from '@/lib/api/schema';
import { ProfileList } from '@/components/subscription-profiles/profile-list';
import { SubscriptionRuntimeStatus } from '@/components/subscription-profiles/runtime-status';

export default function SubscriptionProfilesPage() {
  const profiles = useApi<components['schemas']['ProfileResponse'][]>('/subscription-profiles');
  return <><h1>Subscription profiles</h1><p>Manage versioned requested routes and explicit fallbacks for future runs.</p><SubscriptionRuntimeStatus />{profiles.loading && <p role="status">Loading profile history…</p>}{profiles.failed && <p role="alert">Profile history unavailable. <button onClick={profiles.refresh}>Retry</button></p>}{profiles.value && <ProfileList profiles={profiles.value} refresh={profiles.refresh} />}</>;
}
