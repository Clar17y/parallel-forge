"use client";
import { useRef, useState, type ReactNode } from 'react';
import { usePathname } from 'next/navigation';
import { Navigation } from './navigation';

export function AppShell({ children }: { children: ReactNode }) {
  const dialog = useRef<HTMLDialogElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const [open, setOpen] = useState(false);
  const path = usePathname();
  return <div className="shell">
    <aside aria-label="Sidebar"><p className="brand">Parallel Forge</p><Navigation /></aside>
    <button ref={trigger} className="drawer" aria-label="Open navigation" aria-haspopup="dialog" aria-expanded={open}
      onClick={() => { dialog.current?.showModal(); setOpen(true); }}>Menu</button>
    <dialog ref={dialog} aria-label="Navigation" onClose={() => { setOpen(false); trigger.current?.focus(); }}>
      <button onClick={() => dialog.current?.close()}>Close navigation</button>
      <Navigation close={() => dialog.current?.close()} />
    </dialog>
    <main><header><span>Workspace · {path.split('/')[1] || 'Home'}</span><span>Session active</span></header>{children}</main>
  </div>;
}
