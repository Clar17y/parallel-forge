import { useId } from 'react';

export function Field({ label, value, onChange, error, multiline = false, ...input }: {
  label: string; value: string | number; onChange: (value: string) => void; error?: string;
  multiline?: boolean; required?: boolean; type?: string; min?: number; max?: number; pattern?: string;
}) {
  const id = useId();
  const common = { id, value, onChange: (event: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement>) => onChange(event.target.value),
    'aria-invalid': !!error, 'aria-describedby': error ? `${id}-error` : undefined };
  return <div className="form-field"><label htmlFor={id}>{label}</label>
    {multiline ? <textarea {...common} required={input.required} rows={4} /> : <input {...common} {...input} />}
    {error && <p id={`${id}-error`}>{error}</p>}
  </div>;
}

export function lines(value: string): string[] {
  return value.split(/\r?\n/).map(item => item.trim()).filter(Boolean);
}
