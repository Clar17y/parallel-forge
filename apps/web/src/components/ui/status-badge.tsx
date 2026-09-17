import { AlertTriangle, Check, Circle, Info } from 'lucide-react';
import type { Tone } from './tone';

export function StatusBadge({ label, tone = 'neutral' }: { label: string; tone?: Tone }) {
  const Icon = tone === 'success' ? Check : tone === 'danger' || tone === 'warning' ? AlertTriangle : tone === 'info' ? Info : Circle;
  return <span className="status-badge" data-tone={tone}><Icon aria-hidden="true" />{label}</span>;
}
