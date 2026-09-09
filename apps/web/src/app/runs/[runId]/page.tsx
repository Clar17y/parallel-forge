'use client';
import { useParams } from 'next/navigation';
import { useApi } from '@/hooks/use-api';
import type { components } from '@/lib/api/schema';
import { RunCockpit } from '@/components/runs/run-cockpit';

export default function RunPage() {
  const { runId } = useParams<{ runId: string }>();
  const projection = useApi<components['schemas']['RunProjection']>(`/runs/${runId}/projection`);
  if (projection.loading) return <p role="status">Loading run…</p>;
  if (!projection.value) return <p role="alert">Run unavailable. <button onClick={projection.refresh}>Retry</button></p>;
  return <RunCockpit key={runId} initial={projection.value} />;
}
