'use client';

import { useId, useState } from 'react';

const REFERENCE_LIMITS: Record<string, { input: number; output: number; source: string }> = {
  'google:gemini-3.5-flash': { input: 1_048_576, output: 65_536, source: 'https://ai.google.dev/gemini-api/docs/models/gemini-3.5-flash' },
  'google:gemini-3.8-flash': { input: 1_048_576, output: 65_536, source: 'https://ai.google.dev/gemini-api/docs/models/gemini-3.8-flash' },
  'google:gemini-2.5-flash': { input: 1_048_576, output: 65_536, source: 'https://ai.google.dev/gemini-api/docs/models/gemini-2.5-flash' },
  'google:gemini-2.5-pro': { input: 1_048_576, output: 65_536, source: 'https://ai.google.dev/gemini-api/docs/models/gemini-2.5-pro' },
  'openai:gpt-6-sol': { input: 1_050_000, output: 128_000, source: 'https://developers.openai.com/api/docs/models/gpt-6-sol' },
  'openai:gpt-6-luna': { input: 1_050_000, output: 128_000, source: 'https://developers.openai.com/api/docs/models/gpt-6-luna' },
  'openai:gpt-6.1-sol': { input: 1_050_000, output: 128_000, source: 'https://developers.openai.com/api/docs/models/gpt-6.1-sol' },
  'openai:gpt-6-astra': { input: 1_050_000, output: 128_000, source: 'https://developers.openai.com/api/docs/models/gpt-6-astra' },
  'anthropic:claude-opus-5-5': { input: 1_000_000, output: 128_000, source: 'https://platform.claude.com/docs/en/models/opus-5-5/overview' },
};

export function tokenReference(provider: string | null | undefined, model: string | null | undefined, dimension: 'input' | 'output') {
  if (!provider || !model) return undefined;
  return REFERENCE_LIMITS[`${provider}:${model}`]?.[dimension];
}

export function tokenReferenceSource(provider: string | null | undefined, model: string | null | undefined) {
  if (!provider || !model) return undefined;
  return REFERENCE_LIMITS[`${provider}:${model}`]?.source;
}

export function TokenBudgetSlider({
  label, dimension, value, onChange, provider, model, min = 0, rangeMax, rangeStep,
  error, disabled = false, idPrefix, hideLabel = false,
}: {
  label: string; dimension: 'input' | 'output'; value: number; onChange: (value: number) => void;
  provider?: string | null; model?: string | null; min?: number; rangeMax?: number; rangeStep?: number;
  error?: string;
  disabled?: boolean;
  idPrefix?: string;
  hideLabel?: boolean;
}) {
  const reactId = useId();
  const id = idPrefix ?? reactId;
  const publishedReference = rangeMax == null ? tokenReference(provider, model, dimension) : undefined;
  const referenceMax = rangeMax ?? publishedReference ?? 1_000_000;
  const [exact, setExact] = useState(false);
  const rangeKey = JSON.stringify([provider, model, dimension, rangeMax, min]);
  const [range, setRange] = useState(() => ({ key: rangeKey, max: Math.max(referenceMax, value, min + 1) }));
  const sliderMax = Math.max(range.key === rangeKey ? range.max : referenceMax, value, min + 1);
  // Keep the scale steady during edits; a changed model or range starts a new scale.
  if (range.key !== rangeKey || value > range.max) {
    setRange({ key: rangeKey, max: sliderMax });
  }
  const presetScale = publishedReference ?? sliderMax;
  const setPercent = (percent: number) => onChange(Math.max(min, Math.round(presetScale * percent / 100)));
  return <div className="space-y-2">
    {!hideLabel && <label htmlFor={`${id}-range`} className="block font-medium">{label}</label>}
    <p className="text-sm" aria-live="polite">{value.toLocaleString()} tokens</p>
    <input id={`${id}-range`} aria-label={label} type="range" min={min} max={sliderMax} step={rangeStep ?? 1} value={Math.min(value, sliderMax)} disabled={disabled} onChange={event => onChange(Math.max(min, Math.round(Number(event.target.value))))} aria-invalid={Boolean(error)} aria-describedby={error ? `${id}-error` : undefined} className="w-full" />
    <div className="flex flex-wrap gap-2" aria-label={`${label} presets`}>
      {[25, 50, 75, 100].map(percent => <button key={percent} type="button" className="rounded border px-2 py-1 text-xs" disabled={disabled} onClick={() => setPercent(percent)}>{percent}%</button>)}
    </div>
    <p className="text-xs text-[var(--muted)]">{publishedReference ? `Model reference: ${publishedReference.toLocaleString()} tokens` : `Budget range: ${sliderMax.toLocaleString()} tokens`}</p>
    <details className="text-xs text-[var(--muted)]">
      <summary className="cursor-pointer">About limits</summary>
      <p className="mt-2">{publishedReference ? <>Presets use the published {dimension === 'input' ? 'context' : 'output'} capacity (checked 2026-10-09). Your configured client may have a different limit. These budgets cover token use across a task; input and output share context in each request. <a href={tokenReferenceSource(provider, model)} target="_blank" rel="noreferrer">Published model reference</a></> : <>{rangeMax == null ? 'No published capacity is available for this model.' : 'This shared budget can cover many tasks and requests.'} Adjust the slider range under Exact value. Presets use the displayed budget range.</>}</p>
    </details>
    <label className="flex items-center gap-2 text-xs"><input type="checkbox" checked={exact} disabled={disabled} onChange={event => setExact(event.target.checked)} />Exact value</label>
    {exact && <>
      <input aria-label={`${label} exact value`} type="number" min={min} step={1} value={value} disabled={disabled} onChange={event => { if (event.target.value !== '') onChange(Math.max(min, Math.round(Number(event.target.value)))); }} className="w-full rounded border px-2 py-1" />
      {!tokenReference(provider, model, dimension) || rangeMax != null ? <label className="block text-xs">Slider range maximum
        <input aria-label={`${label} range maximum`} type="number" min={min + 1} step={1} value={sliderMax} disabled={disabled} onChange={event => { if (event.target.value !== '') setRange({ key: rangeKey, max: Math.max(value, min + 1, Math.round(Number(event.target.value))) }); }} className="mt-1 w-full rounded border px-2 py-1" />
      </label> : null}
    </>}
    {error && <p id={`${id}-error`} role="alert" className="text-xs text-[var(--danger)]">{error}</p>}
  </div>;
}
