"use client";

import { LoadingStatus } from '@/components/ui/loading-status';
import { useEffect, useRef, useState, type ReactNode } from 'react';
import { api } from '@/lib/api/client';
import { csrf } from '@/lib/api/csrf';
import { SessionIdentity } from './session-identity';

type Session = { csrfToken: string; actorId?: string };
type Props = {
  children: ReactNode;
  exchange?: (token: string, signal: AbortSignal) => Promise<Session>;
  session?: (signal: AbortSignal) => Promise<Session>;
};

export function BootstrapGate({ children, exchange = exchangeBootstrap, session = recoverSession }: Props) {
  const [state, setState] = useState<'pending' | 'ready' | 'failed'>('pending');
  const [actorId, setActorId] = useState<string | null>(null);
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
        setActorId(result.actorId ?? null);
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
  if (state === 'ready') return <SessionIdentity.Provider value={actorId}>{children}</SessionIdentity.Provider>;
  if (state === 'failed') return <main><div role="alert"><h1>Sign-in required</h1><p>Use a fresh bootstrap link to start a session.</p></div></main>;
  return <LoadingStatus>Starting secure session…</LoadingStatus>;
}

async function exchangeBootstrap(token: string, signal: AbortSignal): Promise<Session> {
  const result = await api<{ csrf_token: string }>('/auth/bootstrap', {
    method: 'POST', signal, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ token }),
  });
  if (!result?.csrf_token) throw new Error('Invalid session response');
  return { csrfToken: result.csrf_token };
}

async function recoverSession(signal: AbortSignal): Promise<Session> {
  const session = await api<{ actor_id: string; actor_class: string }>('/auth/session', { cache: 'no-store', signal });
  if (!session?.actor_id || session.actor_class !== 'operator') throw new Error('Invalid session identity');
  signal.throwIfAborted();
  const result = await api<{ csrf_token: string }>('/auth/csrf', { cache: 'no-store', signal });
  if (!result?.csrf_token) throw new Error('Invalid session response');
  return { csrfToken: result.csrf_token, actorId: session.actor_id };
}
