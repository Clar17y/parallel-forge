'use client';
import { LoadingStatus } from '@/components/ui/loading-status';
import { Suspense } from 'react';
import { useParams, useRouter, useSearchParams } from 'next/navigation';
import { useApi } from '@/hooks/use-api';
import type { components } from '@/lib/api/schema';
import { RunCockpit, parseRunSection, type RunSection } from '@/components/runs/run-cockpit';
import { RunQueueDiagnosticsBanner } from '@/components/runs/run-queue-diagnostics';
import { TaskInspector } from '@/components/subscription-tasks/task-inspector';
import { useSubscriptionTasks, SubscriptionTasksProvider } from '@/hooks/use-subscription-tasks';

function RunPageContent() {
  const { runId } = useParams<{ runId: string }>();
  const router = useRouter();
  const searchParams = useSearchParams();
  const projection = useApi<components['schemas']['RunProjection']>(`/runs/${runId}/projection`);
  const subscriptionTasks = useSubscriptionTasks(runId);
  if (projection.loading) return <LoadingStatus>Loading run…</LoadingStatus>;
  if (!projection.value) return <p role="alert">Run unavailable. <button onClick={projection.refresh}>Retry</button></p>;
  const section = parseRunSection(searchParams.get('section'));
  const onSectionChange = (next: RunSection) => {
    const params = new URLSearchParams(searchParams.toString());
    if (next === 'overview') params.delete('section');
    else params.set('section', next);
    const qs = params.toString();
    router.replace(qs ? `/runs/${runId}?${qs}` : `/runs/${runId}`, { scroll: false });
  };
  const queueDiagnostics = <RunQueueDiagnosticsBanner diagnostic={subscriptionTasks.tasks.value?.queue_diagnostic} onTasks={() => onSectionChange('tasks')} />;
  return <SubscriptionTasksProvider value={subscriptionTasks}>
    <RunCockpit key={runId} initial={projection.value} section={section} onSectionChange={onSectionChange}
      queueDiagnostics={queueDiagnostics} onRefreshTasks={subscriptionTasks.refresh}
      tasks={<TaskInspector key={`tasks-${runId}`} runId={runId} />} />
  </SubscriptionTasksProvider>;
}

export default function RunPage() {
  return <Suspense fallback={<LoadingStatus>Loading run…</LoadingStatus>}><RunPageContent /></Suspense>;
}
