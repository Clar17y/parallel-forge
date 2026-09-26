'use client';
import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { Play, CheckSquare, Folder, Shield, Bot, KeyRound, FlaskConical, History, BarChart3, Settings2 } from 'lucide-react';

export const navigationGroups = [
  { label: 'Operate', links: [
    { label: 'Runs', href: '/runs', icon: Play },
    { label: 'Approvals', href: '/approvals', icon: CheckSquare },
    { label: 'Projects', href: '/projects', icon: Folder },
  ] },
  { label: 'Govern', links: [
    { label: 'Policies', href: '/policies', icon: Shield },
    { label: 'Agents & models', href: '/agents', icon: Bot },
    { label: 'Tool permissions', href: '/tools', icon: KeyRound },
    { label: 'Subscription profiles', href: '/subscription-profiles', icon: Settings2 },
    { label: 'Evaluations', href: '/evaluations', icon: FlaskConical },
  ] },
  { label: 'Inspect', links: [
    { label: 'Audit log', href: '/audit', icon: History },
    { label: 'Usage', href: '/usage', icon: BarChart3 },
  ] },
] as const;

export function isActivePath(path: string, href: string): boolean {
  return path === href || path.startsWith(`${href}/`);
}

export function Navigation({ close }: { close?: () => void }) {
  const path = usePathname();
  return <nav aria-label="Primary" className="primary-navigation">
    {navigationGroups.map(group => <section key={group.label} className="nav-group" aria-label={group.label}>
      <h2>{group.label}</h2><ul>{group.links.map(({ label, href, icon: Icon }) => <li key={href}>
        <Link href={href} aria-current={isActivePath(path, href) ? 'page' : undefined} onClick={close}>
          <Icon aria-hidden="true" />{label}
        </Link>
      </li>)}</ul>
    </section>)}
  </nav>;
}
