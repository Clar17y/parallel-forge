import type { ReactNode } from 'react';
import clsx from 'clsx';

export function Panel({ title, description, action, children, className }: {
  title: string; description?: string; action?: ReactNode; children: ReactNode; className?: string;
}) {
  return <section className={clsx('panel', className)} aria-label={title}>
    <div className="panel-heading"><div><h2>{title}</h2>{description && <p className="meta">{description}</p>}</div>
      {action && <div className="panel-action">{action}</div>}
    </div>
    {children}
  </section>;
}
