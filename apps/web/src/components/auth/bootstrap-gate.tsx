"use client";

import { useEffect, useRef, useState, type ReactNode } from 'react';
import { api } from '@/lib/api/client';
import { csrf } from '@/lib/api/csrf';

type Session = { csrfToken: string };
type Props = {
  children: ReactNode;
  exchange?: (token: string, signal: AbortSignal) => Promise<Session>;
  session?: (signal: AbortSignal) => Promise<Session>;
};

export function BootstrapGate({ children, exchange = exchangeBootstrap, session = recoverSession }: Props) {
  const [state, setState] = useState<'pending' | 'ready' | 'failed'>('pending');
  const fragment = useRef<string | null>(null);
  useEffect(() => {
    const owner = new AbortController();
    if (fragment.current === null) {
      const hash = window.location.hash;
      // Remove credentials before decoding or starting any asynchronous operation.
      if (hash) window.history.replaceState(null, '', window.location.pathname + window.location.search);
      fragment.current = hash;
    }
    // Start after StrictMode's synchronous setup/cleanup replay. Its first owner
    // is canceled before consuming the one-time token or sending a request.
    const attempt = Promise.resolve().then(async () => {
      owner.signal.throwIfAborted();
      const hash = fragment.current ?? '';
      fragment.current = '';
      if (hash.startsWith('#bootstrap=')) {
        const token = decodeURIComponent(hash.slice(11));
        if (!token) throw new Error('Missing bootstrap');
        await exchange(token, owner.signal);
      }
      owner.signal.throwIfAborted();
      return session(owner.signal);
    });
    void attempt.then(result => {
      if (!owner.signal.aborted) {
        if (!result.csrfToken) { csrf.clear(); setState('failed'); return; }
        csrf.set(result.csrfToken);
        setState('ready');
      }
    }, () => {
      if (!owner.signal.aborted) { csrf.clear(); setState('failed'); }
    });
    const unsubscribe = csrf.onInvalidated(() => {
      owner.abort();
      setState('failed');
    });
    return () => { owner.abort(); unsubscribe(); };
  }, [exchange, session]);
  if (state === 'ready') return children;
  if (state === 'failed') return <main><div role="alert"><h1>Sign-in required</h1><p>Use a fresh bootstrap link to start a session.</p></div></main>;
  return <p role="status">Starting secure session…</p>;
}

async function exchangeBootstrap(token: string, signal: AbortSignal): Promise<Session> {
  const result = await api<{ csrf_token: string }>('/auth/bootstrap', {
    method: 'POST', signal, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ token }),
  });
  if (!result?.csrf_token) throw new Error('Invalid session response');
  return { csrfToken: result.csrf_token };
}

async function recoverSession(signal: AbortSignal): Promise<Session> {
  await api('/auth/session', { cache: 'no-store', signal });
  signal.throwIfAborted();
  const result = await api<{ csrf_token: string }>('/auth/csrf', { cache: 'no-store', signal });
  if (!result?.csrf_token) throw new Error('Invalid session response');
  return { csrfToken: result.csrf_token };
}
