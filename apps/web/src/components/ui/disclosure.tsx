import type { ReactNode } from 'react';

export interface DisclosureProps {
  summary: ReactNode;
  children: ReactNode;
  defaultOpen?: boolean;
  open?: boolean;
  className?: string;
  id?: string;
}

export function Disclosure({
  summary,
  children,
  defaultOpen,
  open,
  className,
  id,
}: DisclosureProps) {
  return (
    <details
      id={id}
      className={['disclosure-card', className].filter(Boolean).join(' ')}
      open={open ?? (defaultOpen ? true : undefined)}
    >
      <summary className="disclosure-summary">{summary}</summary>
      <div className="disclosure-body">{children}</div>
    </details>
  );
}
