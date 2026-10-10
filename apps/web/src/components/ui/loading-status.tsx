import { Loader2 } from 'lucide-react';
import type { ReactNode } from 'react';

/** A visible indicator for an initial data or route read. Retained background data stays static. */
export function LoadingStatus({ children, className }: { children: ReactNode; className?: string }) {
  return <p role="status" className={`inline-flex items-center gap-2 ${className ?? ''}`}>
    <Loader2 className="w-4 h-4 animate-spin motion-reduce:animate-none flex-none" aria-hidden="true" />
    <span>{children}</span>
  </p>;
}
