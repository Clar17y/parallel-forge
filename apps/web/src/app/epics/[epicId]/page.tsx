'use client';

import { Suspense, use, useEffect, useRef, useState } from 'react';
import Link from 'next/link';
import { useRouter, useSearchParams } from 'next/navigation';
import { EpicMutationProvider, useEpicMutations } from '@/hooks/epics/use-epic-mutations';
import { EpicWorkspaceProvider, useEpicWorkspace } from '@/hooks/epics/use-epic-workspace';
import { EvidenceDetails } from '@/components/epics/evidence-details';
import { Button } from '@/components/ui/button';
import { StatusBadge } from '@/components/ui/status-badge';
import { BriefEditor } from '@/components/epics/brief-editor';
import { GraphEditor } from '@/components/epics/graph-editor';
import { AuthoringWorkspace } from '@/components/epics/authoring-workspace';
import { DeliveryWorkspace } from '@/components/epics/delivery-workspace';

type WorkspaceTab = 'brief' | 'graph' | 'authoring' | 'delivery';

function EpicWorkspaceContent({ epicId }: { epicId: string }) {
  const searchParams = useSearchParams();
  const router = useRouter();
  const tab = searchParams.get('tab');
  const initialTab: WorkspaceTab = tab && ['brief', 'graph', 'authoring', 'delivery'].includes(tab) ? tab as WorkspaceTab : 'brief';
  const executionId = searchParams.get('execution_id');

  const [activeTab, setActiveTab] = useState<WorkspaceTab>(initialTab);

  const workspace = useEpicWorkspace(epicId);
  const mutations = useEpicMutations(epicId);
  const { registerCompletion } = mutations;
  const replace = router.replace;
  const epic = workspace.epic;

  const loadedRef = useRef(!!epic);
  useEffect(() => { loadedRef.current = !!epic; }, [epic]);
  const [confirmation, setConfirmation] = useState<string | null>(null);
  const confirmationRef = useRef<HTMLDivElement>(null);
  useEffect(() => registerCompletion('*', (receipt, request) => {
    setConfirmation(request.idempotencyKey);
    if (loadedRef.current) return;
    const result = receipt as { execution_id?: string; conversation_id?: string; job_id?: string };
    const url = new URL(window.location.href);
    const pathSubject = request.path.split('/').at(-2);
    let remembered = false;
    const remember = (name: string, value: string | undefined) => {
      if (value) { url.searchParams.set(name, value); remembered = true; }
    };
    if (request.kind === 'execution-start') remember('execution_id', result.execution_id);
    if (request.kind === 'execution-command') remember('execution_id', pathSubject);
    if (request.kind === 'conversation-start') {
      remember('conversation_id', result.conversation_id);
      url.searchParams.delete('job_id');
    }
    if (request.kind === 'conversation-turn' || request.kind === 'job-submit') {
      if (url.searchParams.get('conversation_id') !== pathSubject) url.searchParams.delete('job_id');
      remember('conversation_id', pathSubject);
    }
    if (request.kind === 'job-submit') remember('job_id', result.job_id);
    if (['job-cancel', 'job-retry', 'proposal-adopt'].includes(request.kind ?? '')) remember('job_id', result.job_id ?? pathSubject);
    if (remembered) replace(url.pathname + url.search, { scroll: false });
  }), [registerCompletion, replace]);
  useEffect(() => { if (confirmation) confirmationRef.current?.focus(); }, [confirmation]);

  const recovery = <>
    {mutations.hasPendingRetry && <div role="alert" className="p-4 rounded border border-[var(--border)] bg-[var(--warning-soft)] space-y-2">
      <p>{mutations.loading ? 'Waiting for the current action to finish.' : 'An earlier action has an uncertain result. Retry it before starting another action.'}</p>
      <p>{mutations.executionId
        ? `The original request belongs to execution ${mutations.executionId}.`
        : 'The original request belongs to this epic.'}</p>
      <Button disabled={mutations.loading} onClick={() => { void mutations.retryPending().catch(() => undefined); }}>Retry original request</Button>
      {!mutations.reloadProtected && <p>This browser cannot preserve the request if you reload or close this tab. You can navigate within this tab, but keep it open until the request is resolved.</p>}
      <EvidenceDetails summary="Inspect pending request"><p>{mutations.pendingMutation?.method} {mutations.pendingMutation?.path}</p><pre className="overflow-auto">{JSON.stringify(mutations.pendingMutation?.body, null, 2)}</pre></EvidenceDetails>
    </div>}
    {confirmation && <div role="status" tabIndex={-1} ref={confirmationRef}>Action confirmed by the server.</div>}
    {!epic && mutations.error && !mutations.hasPendingRetry && <div role="alert">{mutations.error}</div>}
  </>;
  const selectSection = (section: WorkspaceTab) => {
    setActiveTab(section);
    const url = new URL(window.location.href);
    url.searchParams.set('tab', section);
    router.replace(url.pathname + url.search, { scroll: false });
  };


  if (workspace.loading && !epic) {
    return <div className="space-y-4">{recovery}<p role="status">Loading epic workspace…</p></div>;
  }

  if (workspace.failed && !epic) {
    return (
      <div className="space-y-4">{recovery}<div role="alert" className="p-6 bg-[var(--danger-soft)] text-[var(--danger)] rounded border border-[var(--border)] space-y-3">
        <h2 className="text-lg font-bold">Epic Not Found or Unavailable</h2>
        <p className="text-sm">The epic could not be retrieved from the server.</p>
        <EvidenceDetails summary="Inspect epic reference">{epicId}</EvidenceDetails>
        <div className="flex space-x-2">
          <Button variant="secondary" onClick={workspace.refreshEpic}>
            Retry
          </Button>
          <Link href="/epics" className="button" data-variant="quiet">
            Back to Epics
          </Link>
        </div>
      </div></div>
    );
  }

  if (!epic) return null;

  return (
    <div className="epic-workspace space-y-6">
      {recovery}
      {workspace.failed && <div role="alert">The latest refresh failed. Your current edits remain available. <Button variant="quiet" onClick={workspace.refreshProjections}>Retry refresh</Button></div>}
      {/* Top Header */}
      <header className="border-b border-[var(--border)] pb-4 space-y-2">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <div className="flex items-center space-x-2">
              <Link href={`/epics?project_id=${encodeURIComponent(epic.project_id)}`} className="text-xs text-[var(--focus)] hover:underline">
                ← Back to Project Epics
              </Link>
              <span className="text-xs text-[var(--muted)]">•</span>
              <span className="text-xs text-[var(--muted)]">Version {epic.version}</span>
            </div>
            <h1 className="text-2xl font-bold mt-1">{epic.title}</h1>
          </div>

          <div className="flex flex-wrap items-center gap-2">
            {epic.accepted_brief_revision_id ? (
              <StatusBadge label="Brief Accepted" tone="success" />
            ) : (
              <StatusBadge label="Draft Brief Only" tone="neutral" />
            )}
            {epic.accepted_graph_revision_id ? (
              <StatusBadge label="Graph Accepted" tone="success" />
            ) : (
              <StatusBadge label="No Graph Accepted" tone="neutral" />
            )}
          </div>
        </div>
      </header>

      {/* Main Section Navigation */}
      <nav aria-label="Epic sections" className="flex flex-wrap border-b border-[var(--border)] gap-2 pb-1">
        <Button
          variant={activeTab === 'brief' ? 'primary' : 'quiet'}
          aria-current={activeTab === 'brief' ? 'page' : undefined}
          onClick={() => selectSection('brief')}
        >
          Requirements Brief
        </Button>
        <Button
          variant={activeTab === 'graph' ? 'primary' : 'quiet'}
          aria-current={activeTab === 'graph' ? 'page' : undefined}
          onClick={() => selectSection('graph')}
        >
          Work-Item Graph
        </Button>
        <Button
          variant={activeTab === 'authoring' ? 'primary' : 'quiet'}
          aria-current={activeTab === 'authoring' ? 'page' : undefined}
          onClick={() => selectSection('authoring')}
        >
          Authoring & Brainstorm
        </Button>
        <Button
          variant={activeTab === 'delivery' ? 'primary' : 'quiet'}
          aria-current={activeTab === 'delivery' ? 'page' : undefined}
          onClick={() => selectSection('delivery')}
        >
          Delivery & Progress
        </Button>
      </nav>

      {/* Tab Panels */}
      <div>
        <div hidden={activeTab !== 'brief'}><BriefEditor epicId={epicId} /></div>
        <div hidden={activeTab !== 'graph'}><GraphEditor epicId={epicId} /></div>
        <div hidden={activeTab !== 'authoring'}>
          <AuthoringWorkspace
            epicId={epicId}
            projectId={epic.project_id}
            epicVersion={epic.version}
          />
        </div>
        <div hidden={activeTab !== 'delivery'}>
          <DeliveryWorkspace
            epicId={epicId}
            initialExecutionId={executionId}
            epicVersion={epic.version}
          />
        </div>
      </div>
    </div>
  );
}

function EpicPageInner({ params }: { params: Promise<{ epicId: string }> }) {
  const { epicId } = use(params);
  return <EpicMutationProvider key={epicId} epicId={epicId}><EpicWorkspaceProvider epicId={epicId}><EpicWorkspaceContent epicId={epicId} /></EpicWorkspaceProvider></EpicMutationProvider>;
}

export default function EpicPage({ params }: { params: Promise<{ epicId: string }> }) {
  return (
    <Suspense fallback={<p role="status">Loading epic workspace…</p>}>
      <EpicPageInner params={params} />
    </Suspense>
  );
}
