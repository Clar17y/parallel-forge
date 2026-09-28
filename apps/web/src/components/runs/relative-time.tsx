'use client';

import { useSyncExternalStore } from 'react';
import { formatRelativeTime, safeExactTime } from '@/lib/activity-presentation';

let currentTime = Date.now();
let timer: ReturnType<typeof setInterval> | undefined;
const listeners = new Set<() => void>();
function subscribe(listener: () => void) {
  listeners.add(listener);
  currentTime = Date.now();
  listener();
  if (!timer) timer = setInterval(() => {
    currentTime = Date.now();
    listeners.forEach(notify => notify());
  }, 15_000);
  return () => {
    listeners.delete(listener);
    if (!listeners.size && timer) {
      clearInterval(timer);
      timer = undefined;
    }
  };
}
function snapshot() { return currentTime; }
// Keep server HTML and the first hydration render neutral until the client clock is available.
function serverSnapshot() { return null; }

export function RelativeTime({
  timestamp,
  className,
}: {
  timestamp: string | undefined;
  className?: string;
}) {
  const now = useSyncExternalStore<number | null>(subscribe, snapshot, serverSnapshot);

  const exact = safeExactTime(timestamp);
  const relative = exact === 'Unknown time' ? exact
    : now === null ? 'Loading time…' : formatRelativeTime(timestamp, now);

  return (
    <time dateTime={exact === 'Unknown time' ? undefined : timestamp} title={now === null ? undefined : exact} className={className}>
      {relative}
    </time>
  );
}
