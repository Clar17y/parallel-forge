"use client";
import { useEffect } from 'react';
import { useRouter } from 'next/navigation';

/** Mounted only after BootstrapGate confirms the session. */
export default function Home() {
  const router = useRouter();
  useEffect(() => { router.replace('/runs'); }, [router]);
  return <p role="status">Opening runs…</p>;
}
