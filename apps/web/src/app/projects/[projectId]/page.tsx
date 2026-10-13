'use client';
import { LoadingStatus } from '@/components/ui/loading-status';
import { useMemo, useRef } from 'react';
import { useParams } from 'next/navigation';
import Link from 'next/link';
import { useApi } from '@/hooks/use-api';
import { mutate } from '@/lib/api/client';
import type { components } from '@/lib/api/schema';
import { PolicyEditor } from '@/components/projects/policy-editor';
import { SetupChecklist } from '@/components/projects/setup-checklist';
import { ProfileSelector } from '@/components/subscription-profiles/profile-selector';
import { MonitoringPolicySummary } from '@/components/projects/monitoring-policy-summary';
import { MonitoringPolicyEditor } from '@/components/projects/monitoring-policy-editor';
import {
  makeMonitoringPolicySaver,
  getMonitoringPolicy,
  type MonitoringPolicyResponse,
  type TaskUsageHistoryResponse,
} from '@/lib/api/monitoring-policy';

export default function ProjectPage() {
  const { projectId } = useParams<{ projectId: string }>();
  const project = useApi<components['schemas']['ProjectResponse']>(`/projects/${projectId}`, {
    keepPreviousOnRefresh: true,
    keepPreviousOnError: true,
  });
  const monitoringSaver = useMemo(() => makeMonitoringPolicySaver(projectId), [projectId]);
  const attempt = useRef<{ body: string; key: string } | null>(null);
  const value = project.value;
  const policy = useApi<components['schemas']['ProjectPolicyResponse']>(value?.policy_version ? `/projects/${projectId}/policy-versions/${value.policy_version}` : null);
  const profiles = useApi<components['schemas']['ProfileResponse'][]>('/subscription-profiles');
  const selectedProfile = useApi<components['schemas']['ProfileResponse'] | null>(`/projects/${projectId}/subscription-profile`);
  const runsProbe = useApi<components['schemas']['RunListPage']>(`/run-projections?project_id=${projectId}&offset=0&limit=1`);

  const monitoringPolicy = useApi<MonitoringPolicyResponse | null>(`/projects/${projectId}/monitoring-policy`, {
    keepPreviousOnRefresh: true,
    keepPreviousOnError: true,
  });
  const taskUsageReport = useApi<TaskUsageHistoryResponse>(`/task-usage-report?project_id=${projectId}`);

  return <><h1>{value?.name ?? 'Project'}</h1>
    {project.loading && <LoadingStatus>Loading project…</LoadingStatus>}
    {project.failed && <p role="alert">Project unavailable. <button onClick={project.refresh}>Retry</button></p>}
    {value && <>
      <p>{value.repository_path} · {value.github_repository} · {value.default_branch}</p>
      <p><Link href="/runs/new">New run</Link> · <Link href="/policies">Policy history</Link> · <Link href={`/epics?project_id=${projectId}`}>Epics</Link></p>
      <SetupChecklist profileSelected={Boolean(selectedProfile.value)} hasRuns={runsProbe.value ? runsProbe.value.items.length > 0 : null} />
      {policy.loading && <LoadingStatus>Loading policy…</LoadingStatus>}
      {policy.failed && <p role="alert">Policy unavailable. <button onClick={policy.refresh}>Retry</button></p>}
      {policy.value && <PolicyEditor policy={policy.value} onSave={async request => {
        const body = JSON.stringify({ projectId, request });
        if (attempt.current?.body !== body) attempt.current = { body, key: crypto.randomUUID() };
        const saved = await mutate<components['schemas']['ProjectPolicyResponse']>(`/projects/${projectId}/policy-versions`, request,
          { idempotencyKey: attempt.current.key });
        if (!saved) throw new Error('Policy response unavailable');
        project.refresh();
      }} />}
      {profiles.loading || selectedProfile.loading ? <LoadingStatus>Loading subscription profile…</LoadingStatus> : profiles.failed || selectedProfile.failed ? <p role="alert">Subscription profile unavailable. <button onClick={() => { profiles.refresh(); selectedProfile.refresh(); }}>Retry</button></p> : profiles.value && <ProfileSelector key={`${selectedProfile.value?.profile_id ?? 'none'}:${selectedProfile.value?.version ?? 'none'}`} projectId={projectId} profiles={profiles.value} current={selectedProfile.value} refresh={() => { profiles.refresh(); selectedProfile.refresh(); }} />}

      {monitoringPolicy.loading && <LoadingStatus>Loading monitoring policy…</LoadingStatus>}
      {monitoringPolicy.failed && <p role="alert">Monitoring policy unavailable. <button onClick={monitoringPolicy.refresh}>Retry</button></p>}
      {taskUsageReport.failed && <p role="alert">Usage history unavailable. <button onClick={taskUsageReport.refresh}>Retry</button></p>}
      {monitoringPolicy.value !== undefined && (
        <>
          <MonitoringPolicySummary
            policy={monitoringPolicy.value ?? null}
            report={taskUsageReport.value}
            loadingReport={taskUsageReport.loading}
          />
          <MonitoringPolicyEditor
            key={projectId}
            projectId={projectId}
            initial={monitoringPolicy.value ?? null}
            onRefresh={async () => {
              const latest = await getMonitoringPolicy(projectId);
              monitoringSaver.reset();
              monitoringPolicy.refresh();
              return latest;
            }}
            onSave={async (revision) => {
              const res = await monitoringSaver.save(revision);
              monitoringPolicy.refresh();
              taskUsageReport.refresh();
              return res;
            }}
          />
        </>
      )}

      <button onClick={project.refresh}>Reload current policy</button>
    </>}
  </>;
}
