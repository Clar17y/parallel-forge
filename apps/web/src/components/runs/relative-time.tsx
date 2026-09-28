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
function serverSnapshot() { return 0; }

export function RelativeTime({
  timestamp,
  className,
}: {
  timestamp: string | undefined;
  className?: string;
}) {
  const now = useSyncExternalStore(subscribe, snapshot, serverSnapshot);

  const relative = formatRelativeTime(timestamp, now);
  const exact = safeExactTime(timestamp);

  return (
    <time dateTime={exact === 'Unknown time' ? undefined : timestamp} title={exact} className={className}>
      {relative}
    </time>
  );
}
