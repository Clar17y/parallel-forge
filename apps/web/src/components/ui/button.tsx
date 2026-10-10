import type { ComponentProps } from 'react';
import clsx from 'clsx';
import { Loader2 } from 'lucide-react';

export function Button({
  variant = 'secondary',
  type = 'button',
  className,
  busy = false,
  children,
  ...props
}: ComponentProps<'button'> & {
  variant?: 'primary' | 'secondary' | 'danger' | 'quiet';
  busy?: boolean;
}) {
  return (
    <button
      {...props}
      type={type}
      className={clsx('button', className)}
      data-variant={variant}
      aria-busy={busy ? 'true' : undefined}
    >
      {busy ? <Loader2 className="w-4 h-4 animate-spin motion-reduce:animate-none flex-none" aria-hidden="true" /> : null}
      {children}
    </button>
  );
}
