import type { ReactNode } from 'react';
import clsx from 'clsx';
import { AlertTriangle, Check, Circle, Clock, Info, Loader2 } from 'lucide-react';
import type { Tone } from './tone';

export interface ActivityStatusProps {
  title: string;
  description?: string;
  tone?: Tone;
  isExecuting?: boolean;
  isWaiting?: boolean;
  actionRequired?: string | null;
  actions?: ReactNode;
  variant?: 'banner' | 'inline' | 'compact';
  role?: 'status' | 'alert';
  className?: string;
  children?: ReactNode;
}

export function ActivitySpinner({
  className = 'w-4 h-4',
  label = 'Loading…',
}: {
  className?: string;
  label?: string;
}) {
  return (
    <span className="inline-flex items-center gap-2" role="status">
      <Loader2 className={clsx(className, 'animate-spin motion-reduce:animate-none flex-none')} aria-hidden="true" />
      <span className="visually-hidden">{label}</span>
    </span>
  );
}

function StatusIcon({
  tone = 'neutral',
  isExecuting = false,
  isWaiting = false,
}: {
  tone?: Tone;
  isExecuting?: boolean;
  isWaiting?: boolean;
}) {
  if (isExecuting) {
    return (
      <Loader2
        className="w-5 h-5 animate-spin motion-reduce:animate-none flex-none text-[var(--info)]"
        aria-hidden="true"
      />
    );
  }
  if (isWaiting && tone === 'warning') {
    return <Clock className="w-5 h-5 flex-none text-[var(--warning)]" aria-hidden="true" />;
  }
  switch (tone) {
    case 'success':
      return <Check className="w-5 h-5 flex-none text-[var(--success)]" aria-hidden="true" />;
    case 'danger':
      return <AlertTriangle className="w-5 h-5 flex-none text-[var(--danger)]" aria-hidden="true" />;
    case 'warning':
      return <AlertTriangle className="w-5 h-5 flex-none text-[var(--warning)]" aria-hidden="true" />;
    case 'info':
      return <Info className="w-5 h-5 flex-none text-[var(--info)]" aria-hidden="true" />;
    default:
      return <Circle className="w-5 h-5 flex-none text-[var(--muted)]" aria-hidden="true" />;
  }
}

export function ActivityStatus({
  title,
  description,
  tone = 'neutral',
  isExecuting = false,
  isWaiting = false,
  actionRequired,
  actions,
  variant = 'banner',
  role,
  className,
  children,
}: ActivityStatusProps) {
  const effectiveRole = role ?? (tone === 'danger' ? 'alert' : 'region');

  if (variant === 'inline' || variant === 'compact') {
    return (
      <span
        role={effectiveRole}
        aria-label={title}
        aria-live={effectiveRole === 'alert' ? 'assertive' : 'polite'}
        className={clsx('inline-flex items-center gap-2 text-sm', className)}
        data-tone={tone}
      >
        <StatusIcon tone={tone} isExecuting={isExecuting} isWaiting={isWaiting} />
        <span className="font-medium">{title}</span>
        {description ? <span className="text-[var(--muted)]">— {description}</span> : null}
      </span>
    );
  }

  const borderClass =
    tone === 'danger'
      ? 'border-[var(--danger)] bg-[var(--danger-soft)] text-[var(--danger)]'
      : tone === 'warning'
        ? 'border-[var(--warning)] bg-[var(--warning-soft)] text-[var(--warning)]'
        : tone === 'success'
          ? 'border-[var(--success)] bg-[var(--success-soft)] text-[var(--success)]'
          : tone === 'info'
            ? 'border-[var(--info)] bg-[var(--info-soft)] text-[var(--text)]'
            : 'border-[var(--border)] bg-[var(--surface-muted)] text-[var(--text)]';

  return (
    <aside
      role={effectiveRole}
      aria-label={title}
      aria-live={effectiveRole === 'alert' ? 'assertive' : 'polite'}
      className={clsx(
        'activity-status p-4 rounded-lg border space-y-3 text-sm',
        borderClass,
        className
      )}
      data-tone={tone}
      data-executing={isExecuting ? 'true' : 'false'}
    >
      <div className="flex items-start gap-3">
        <StatusIcon tone={tone} isExecuting={isExecuting} isWaiting={isWaiting} />
        <div className="flex-1 min-w-0 space-y-1">
          <h3 className="font-semibold text-base leading-snug">{title}</h3>
          {description ? <p className="text-sm opacity-90">{description}</p> : null}
          {actionRequired ? (
            <p className="text-xs font-semibold uppercase tracking-wider pt-1 opacity-80">
              Next action: {actionRequired}
            </p>
          ) : null}
        </div>
      </div>
      {children}
      {actions ? <div className="flex flex-wrap items-center gap-2 pt-1">{actions}</div> : null}
    </aside>
  );
}
