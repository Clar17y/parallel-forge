'use client';

import type { components } from '@/lib/api/schema';
import { Field } from './form-fields';

export type JevPolicy = components['schemas']['JevPolicy'];

export const defaultJev: JevPolicy = {
  mode: 'off',
  allow_remote: false,
  model: 'jev-latest',
  semantic_search: true,
  review_focus: true,
  top_k: 15,
  max_requests_per_run: 64,
  max_input_units_per_run: 250000,
  max_candidates: 96,
  max_result_chars: 12000,
  timeout_seconds: 15,
  cache_ttl_seconds: 3600,
};

export interface JevSettingsProps {
  value: JevPolicy | null | undefined;
  onChange: (value: JevPolicy | undefined) => void;
  errors?: Record<string, string>;
  checkboxLabel?: string;
  disabled?: boolean;
  policyNotice?: string;
  legend?: string;
  description?: string;
}

export function JevSettings({
  value,
  onChange,
  errors = {},
  checkboxLabel = 'Configure Jev default',
  disabled = false,
  policyNotice,
  legend = 'Jev advisory search and review',
  description = 'Jev can add remote semantic search and advisory review context. Source processing is remote only when explicitly allowed; projects without explicit settings use their profile default or stay unconfigured.',
}: JevSettingsProps) {
  const isConfigured = value != null;

  return (
    <fieldset disabled={disabled}>
      <legend>{legend}</legend>
      {description && <p>{description}</p>}
      <label>
        <input
          type="checkbox"
          checked={isConfigured}
          disabled={disabled}
          onChange={event => onChange(event.target.checked ? { ...defaultJev } : undefined)}
        />
        {checkboxLabel}
      </label>
      {policyNotice && <p>{policyNotice}</p>}
      {value && (
        <>
          <label>
            Jev mode{' '}
            <select
              value={value.mode}
              onChange={event =>
                onChange({ ...value, mode: event.target.value as JevPolicy['mode'] })
              }
            >
              <option value="off">Off</option>
              <option value="shadow">Shadow · measure without changing results</option>
              <option value="on">On · apply advisory ranking</option>
            </select>
          </label>
          <label>
            <input
              type="checkbox"
              checked={value.allow_remote}
              onChange={event => onChange({ ...value, allow_remote: event.target.checked })}
            />
            Allow remote processing of bounded, redacted source excerpts
          </label>
          <Field
            label="Jev model alias"
            required
            maxLength={128}
            value={value.model}
            onChange={text => onChange({ ...value, model: text })}
            error={errors['jev.model']}
          />
          <label>
            <input
              type="checkbox"
              checked={value.semantic_search}
              onChange={event => onChange({ ...value, semantic_search: event.target.checked })}
            />
            Enable semantic search
          </label>
          <label>
            <input
              type="checkbox"
              checked={value.review_focus}
              onChange={event => onChange({ ...value, review_focus: event.target.checked })}
            />
            Enable advisory review focus
          </label>
          {(
            [
              ['top_k', 'Top results', 1, 100],
              ['max_requests_per_run', 'Maximum requests per run', 1, 1000],
              ['max_input_units_per_run', 'Input allowance per run', 1, 10000000],
              ['max_candidates', 'Maximum candidates', 1, 100],
              ['max_result_chars', 'Maximum result characters', 1, 100000],
              ['timeout_seconds', 'Request timeout seconds', 1, 60],
              ['cache_ttl_seconds', 'Cache lifetime seconds', 0, 86400],
            ] as const
          ).map(([key, label, min, max]) => (
            <Field
              key={key}
              label={label}
              type="number"
              step={key === 'timeout_seconds' ? 'any' : 1}
              min={min}
              max={max}
              required
              value={value[key]}
              onChange={text => onChange({ ...value, [key]: Number(text) })}
              error={errors[`jev.${key}`]}
            />
          ))}
        </>
      )}
    </fieldset>
  );
}
