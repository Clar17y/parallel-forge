'use client';
import Link from 'next/link';
import { AttentionInbox } from '@/components/home/attention-inbox';

/** Mounted only after BootstrapGate confirms the session. */
export default function Home() {
  return <>
    <h1>Needs your attention</h1>
    <p>Decisions and blocked work across every project, in one place. Start something new from <Link href="/runs/new">New run</Link>, or browse <Link href="/runs">all runs</Link>.</p>
    <AttentionInbox />
  </>;
}
