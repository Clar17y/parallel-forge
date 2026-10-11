'use client';

import { useState } from 'react';
import { validateSafeInteger } from '@/lib/api/monitoring-policy';

export interface OptionalTokenCapInputProps {
  value: number | null | undefined;
  onChange: (value: number | null) => void;
  caption: string;
  ariaLabel: string;
  fieldName: string;
  helpText: string;
  error?: string;
}

export function OptionalTokenCapInput({
  value,
  onChange,
  caption,
  ariaLabel,
  fieldName,
  helpText,
  error,
}: OptionalTokenCapInputProps) {
  const [draft, setDraft] = useState(value == null ? '' : String(value));
  const [localError, setLocalError] = useState<string>();

  const activeError = error || localError;

  return (
    <label className="block text-xs">
      {caption} {helpText}
      <input
        type="text"
        inputMode="numeric"
        aria-label={ariaLabel}
        aria-invalid={Boolean(activeError)}
        value={draft}
        onChange={event => {
          const raw = event.target.value;
          setDraft(raw);
          const result = validateSafeInteger(raw, {
            fieldName,
            min: 1,
          });
          const message = result.valid ? '' : (result.error ?? '');
          event.target.setCustomValidity(message);
          setLocalError(message || undefined);
          if (result.valid) {
            onChange(result.value ?? null);
          }
        }}
      />
      {activeError && <span role="alert">{activeError}</span>}
    </label>
  );
}
