"use client";
import Link from 'next/link';
import { usePathname } from 'next/navigation';

const links = [
  ['Runs', '/runs'], ['Approvals', '/approvals'], ['Projects', '/projects'],
  ['Policies', '/policies'], ['Agents & models', '/agents'], ['Tool permissions', '/tools'],
  ['Evaluations', '/evaluations'], ['Audit log', '/audit'], ['Usage', '/usage'],
] as const;

export function Navigation({ close }: { close?: () => void }) {
  const path = usePathname();
  return <nav aria-label="Primary"><ul>{links.map(([label, href]) => <li key={href}>
    <Link href={href} aria-current={path === href || path.startsWith(`${href}/`) ? 'page' : undefined} onClick={close}>{label}</Link>
  </li>)}</ul></nav>;
}
