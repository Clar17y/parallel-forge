'use client';

import { useState } from 'react';
import { validateSafeInteger } from '@/lib/api/monitoring-policy';

export interface OptionalTokenCapInputProps {
  value: number | string | null | undefined;
  onChange: (value: number | string | null) => void;
  caption: string;
  ariaLabel: string;
  fieldName: string;
  helpText: string;
  error?: string;
  allowSigned64?: boolean;
}

export function OptionalTokenCapInput({
  value,
  onChange,
  caption,
  ariaLabel,
  fieldName,
  helpText,
  error,
  allowSigned64 = false,
}: OptionalTokenCapInputProps) {
  const [draft, setDraft] = useState(value == null ? '' : String(value));
  const [prevValue, setPrevValue] = useState(value);
  const [localError, setLocalError] = useState<string>();

  if (value !== prevValue) {
    setPrevValue(value);
    setDraft(value == null ? '' : String(value));
  }

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
            allowSigned64,
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
