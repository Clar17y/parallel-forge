"use client";
import { LoadingStatus } from '@/components/ui/loading-status';
import { useEffect } from 'react';
import { useRouter } from 'next/navigation';

/** Mounted only after BootstrapGate confirms the session. */
export default function Home() {
  const router = useRouter();
  useEffect(() => { router.replace('/runs'); }, [router]);
  return <LoadingStatus>Opening runs…</LoadingStatus>;
}
