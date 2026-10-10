import { useId } from 'react';

export function Field({ label, value, onChange, error, hint, multiline = false, ...input }: {
  label: string; value: string | number; onChange: (value: string) => void; error?: string; hint?: string;
  multiline?: boolean; required?: boolean; type?: string; min?: number; max?: number; step?: number | 'any'; maxLength?: number; pattern?: string; placeholder?: string;
}) {
  const id = useId();
  const descId = error ? `${id}-error` : hint ? `${id}-hint` : undefined;
  const common = { id, value, onChange: (event: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement>) => onChange(event.target.value),
    'aria-invalid': !!error, 'aria-describedby': descId };
  return <div className="form-field"><label htmlFor={id}>{label}</label>
    {hint && <p id={`${id}-hint`} className="field-hint" style={{ fontSize: '0.8rem', color: 'var(--muted-foreground, #666)', margin: '0 0 4px' }}>{hint}</p>}
    {multiline ? <textarea {...common} required={input.required} rows={4} /> : <input {...common} {...input} />}
    {error && <p id={`${id}-error`}>{error}</p>}
  </div>;
}

export function lines(value: string): string[] {
  return value.split(/\r?\n/).map(item => item.trim()).filter(Boolean);
}
