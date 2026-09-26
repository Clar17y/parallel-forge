import type { ComponentProps } from 'react';
import clsx from 'clsx';

export function Button({ variant = 'secondary', type = 'button', className, ...props }: ComponentProps<'button'> & {
  variant?: 'primary' | 'secondary' | 'danger' | 'quiet';
}) {
  return <button {...props} type={type} className={clsx('button', className)} data-variant={variant} />;
}
