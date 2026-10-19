export function formatDate(value: string | Date): string {
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: 'medium',
    timeStyle: 'short',
  }).format(new Date(value));
}

export function formatDurationSeconds(seconds: number | null | undefined): string {
  if (typeof seconds !== 'number' || !Number.isFinite(seconds) || seconds < 0) {
    return 'Unknown';
  }
  const s = Math.round(seconds);
  if (s === 0) return '0s';
  if (s < 60) return `${s}s`;

  if (s < 3600) {
    const mins = Math.floor(s / 60);
    const remSec = s % 60;
    return remSec > 0 ? `${mins}m ${remSec}s` : `${mins}m`;
  }

  if (s < 86400) {
    const hours = Math.floor(s / 3600);
    const remMins = Math.floor((s % 3600) / 60);
    return remMins > 0 ? `${hours}h ${remMins}m` : `${hours}h`;
  }

  const days = Math.floor(s / 86400);
  const remHours = Math.floor((s % 86400) / 3600);
  return remHours > 0 ? `${days}d ${remHours}h` : `${days}d`;
}

export function formatDurationMs(ms: number | null | undefined): string {
  if (typeof ms !== 'number' || !Number.isFinite(ms) || ms < 0) {
    return 'Unknown';
  }
  if (ms === 0) return '0ms';
  if (ms < 1000) return `${Math.round(ms)}ms`;

  if (ms < 60000) {
    const sec = ms / 1000;
    const rounded = Number(sec.toFixed(1));
    return `${rounded}s`;
  }

  return formatDurationSeconds(Math.round(ms / 1000));
}
