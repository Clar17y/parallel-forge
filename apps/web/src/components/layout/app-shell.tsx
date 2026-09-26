'use client';
import { useRef, useState, type ReactNode } from 'react';
import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { Menu, Plus, X } from 'lucide-react';
import { Navigation, navigationGroups, isActivePath } from './navigation';
import { Button } from '@/components/ui/button';

export function AppShell({ children }: { children: ReactNode }) {
  const dialog = useRef<HTMLDialogElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const [open, setOpen] = useState(false);
  const path = usePathname();
  const context = navigationGroups.map(group => group.links.find(link => isActivePath(path, link.href))?.label).find(label => label !== undefined) ?? 'Workspace';
  return <>
    <a className="skip-link" href="#main-content">Skip to content</a>
    <div className="shell">
      <header className="app-topbar">
        <Button ref={trigger} className="drawer-trigger" aria-label="Open navigation" aria-haspopup="dialog" aria-expanded={open}
          onClick={() => { dialog.current?.showModal(); setOpen(true); }}><Menu aria-hidden="true" /></Button>
        <Link href="/runs" className="brand" aria-label="Forge runs"><span className="brand-mark" aria-hidden="true" />Forge</Link>
        <span className="topbar-context">{context}</span>
        <div className="topbar-actions"><span className="environment-label meta">Local control plane</span>
          <Link href="/runs/new" className="button" data-variant="primary"><Plus aria-hidden="true" />New run</Link>
        </div>
      </header>
      <aside className="app-sidebar" aria-label="Sidebar"><Navigation /><p className="sidebar-note">Evidence-led delivery.<br />Explicit human approvals.</p></aside>
      <dialog ref={dialog} className="navigation-drawer" aria-label="Navigation" onClose={() => { setOpen(false); trigger.current?.focus(); }}>
        <div className="drawer-heading"><span className="brand">Forge</span>
          <Button aria-label="Close navigation" onClick={() => dialog.current?.close()}><X aria-hidden="true" /></Button>
        </div>
        <Navigation close={() => dialog.current?.close()} />
      </dialog>
      <main id="main-content" className="app-main" tabIndex={-1}>{children}</main>
    </div>
  </>;
}
