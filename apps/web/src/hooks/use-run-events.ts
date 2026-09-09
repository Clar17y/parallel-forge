'use client';
import { useEffect, useRef, useState } from 'react';
import { csrf } from '@/lib/api/csrf';

type Connection = 'connecting' | 'connected' | 'reconnecting' | 'unavailable';
const MAX_FRAME = 262144;
const MAX_CURSOR = 9223372036854775807n;
class InvalidStream extends Error {}

function sequence(value: string): bigint {
  if (!/^[0-9]{1,19}$/.test(value) || BigInt(value) > MAX_CURSOR) throw new InvalidStream();
  return BigInt(value);
}

function delay(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise(resolve => {
    const done = () => { clearTimeout(timer); signal.removeEventListener('abort', done); resolve(); };
    const timer = setTimeout(done, ms);
    signal.addEventListener('abort', done, { once: true });
    if (signal.aborted) done();
  });
}

/** Fetch supports exact resume headers and all named server events. Event payloads
 * never mutate cockpit state; each new sequence requests an authoritative read. */
export function useRunEvents(runId: string, after: string, onRefresh: () => void,
  { retryMs = 1000, idleMs = 45000 }: { retryMs?: number; idleMs?: number } = {}) {
  const [connection, setConnection] = useState<Connection>('connecting');
  const refresh = useRef(onRefresh);
  useEffect(() => { refresh.current = onRefresh; }, [onRefresh]);
  useEffect(() => {
    const owner = new AbortController();
    async function listen() {
      let cursor: bigint;
      try { cursor = sequence(after); } catch { setConnection('unavailable'); return; }
      let failures = 0;
      while (!owner.signal.aborted) {
        const transport = new AbortController();
        const signal = AbortSignal.any([owner.signal, transport.signal]);
        let idle: ReturnType<typeof setTimeout>;
        const resetIdle = () => { clearTimeout(idle); idle = setTimeout(() => transport.abort(), idleMs); };
        let reader: ReadableStreamDefaultReader<Uint8Array> | undefined;
        const cancel = () => { void reader?.cancel().catch(() => {}); };
        try {
          resetIdle();
          const response = await fetch(`/api/runs/${encodeURIComponent(runId)}/events`, {
            credentials: 'same-origin', cache: 'no-store', signal,
            headers: { Accept: 'text/event-stream', 'Last-Event-ID': String(cursor) },
          });
          if (response.status === 401) { csrf.clear(); throw new InvalidStream(); }
          if (response.status >= 400 && response.status < 500) throw new InvalidStream();
          if (!response.ok) throw new Error('Stream unavailable');
          if (!response.headers.get('content-type')?.startsWith('text/event-stream') || !response.body) throw new InvalidStream();
          signal.throwIfAborted();
          reader = response.body.getReader();
          signal.addEventListener('abort', cancel, { once: true });
          setConnection('connected');
          refresh.current(); // Reconnect also reconciles changes missed during disconnection.
          const decoder = new TextDecoder();
          let buffer = '';
          while (!signal.aborted) {
            const { value, done } = await reader.read();
            if (done || signal.aborted) break;
            resetIdle();
            if (buffer.length + value.byteLength > MAX_FRAME) throw new InvalidStream();
            buffer += decoder.decode(value, { stream: true });
            let end: number;
            while ((end = buffer.indexOf('\n\n')) >= 0) {
              const frame = buffer.slice(0, end);
              buffer = buffer.slice(end + 2);
              const lines = frame.split('\n');
              const id = lines.find(line => line.startsWith('id:'))?.slice(3).trim();
              if (id === undefined) continue; // Heartbeats have no persisted event ID.
              const next = sequence(id);
              if (next <= cursor) continue;
              const data = JSON.parse(lines.filter(line => line.startsWith('data:')).map(line => line.slice(5).trimStart()).join('\n')) as { sequence?: unknown };
              if (typeof data?.sequence !== 'number' || !Number.isSafeInteger(data.sequence) || BigInt(data.sequence) !== next) throw new InvalidStream();
              cursor = next;
              failures = 0;
              refresh.current(); // Gaps are handled by the same mandatory authoritative read.
            }
          }
        } catch (error) {
          if (owner.signal.aborted) return;
          if (error instanceof InvalidStream || error instanceof SyntaxError) { setConnection('unavailable'); return; }
        } finally {
          clearTimeout(idle!);
          signal.removeEventListener('abort', cancel);
          cancel();
          reader?.releaseLock();
          transport.abort();
        }
        if (!owner.signal.aborted) {
          setConnection('reconnecting');
          await delay(Math.min(retryMs * 2 ** Math.min(failures++, 4), 15000), owner.signal);
        }
      }
    }
    void listen();
    return () => owner.abort();
  }, [runId, after, retryMs, idleMs]);
  return connection;
}
