'use client';

import type { components } from '@/lib/api/schema';
import Link from 'next/link';
import { AlertTriangle, ArrowRight } from 'lucide-react';
import { Button } from '@/components/ui/button';

type Diagnostic = components['schemas']['SubscriptionQueueDiagnosticView'];

const titleForStatus: Record<Diagnostic['status'], string> = {
  no_current_worker: 'Queued task cannot start: no worker reporting',
  no_matching_registered_route: 'Queued task cannot start: no matching worker registered',
  matched_unavailable: 'Queued task cannot start: matching worker unavailable',
  matching_ready: 'Matching worker ready',
};

export function RunQueueDiagnosticsBanner({
  diagnostic,
  onTasks,
}: {
  diagnostic?: Diagnostic | null;
  onTasks?: () => void;
}) {
  if (!diagnostic || diagnostic.status === 'matching_ready') return null;

  const title = titleForStatus[diagnostic.status] ?? 'Queued task cannot start';
  const hasAntigravityWarning = diagnostic.warnings?.includes('approved_tools_unproved');

  return (
    <section
      className="run-notice run-queue-diagnostic-banner rounded-lg border border-amber-500 bg-amber-500/10 p-4 my-3 space-y-2"
      role="alert"
      aria-label="Queue diagnostic: matching worker unavailable"
    >
      <div className="flex items-center gap-2 font-semibold text-amber-500">
        <AlertTriangle className="w-5 h-5 flex-none" aria-hidden="true" />
        <h2>{title}</h2>
      </div>
      <p className="font-medium text-sm">
        {diagnostic.message}
      </p>
      {diagnostic.guidance && (
        <p className="text-sm opacity-90">
          {diagnostic.guidance}
        </p>
      )}
      {hasAntigravityWarning && (
        <p className="text-xs text-amber-400">
          <strong>Warning:</strong> Forge cannot guarantee that Antigravity limits itself to Forge-approved tools; native tools may be available to the model.
        </p>
      )}
      <div className="flex flex-wrap items-center gap-3 pt-1 text-sm">
        <Link
          href="/subscription-profiles#client-setup"
          className="inline-flex items-center gap-1 underline underline-offset-4 font-semibold text-blue-400 hover:text-blue-300"
        >
          Inspect worker registration and client setup <ArrowRight className="w-3.5 h-3.5" aria-hidden="true" />
        </Link>
        {onTasks && (
          <Button variant="quiet" onClick={onTasks}>
            Inspect tasks
          </Button>
        )}
      </div>
    </section>
  );
}
