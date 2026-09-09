import type { components } from '@/lib/api/schema';

export function ObservationEvidence({ observation }: { observation: components['schemas']['RemoteObservationSection'] }) {
  return <><p>Observed head: <code>{observation.head_sha ?? 'Unavailable'}</code></p>
    <p><a href={`/api/artifacts/${observation.observation_digest}/download`}>Remote observation evidence</a> · <code>{observation.observation_digest}</code></p></>;
}
