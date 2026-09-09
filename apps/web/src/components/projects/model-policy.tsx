import type { components } from '@/lib/api/schema';
import { Field } from './form-fields';

type Model = components['schemas']['AgentModelPolicy'];
export const defaultModel: Model = { provider: 'google', model: 'gemini-3.5-flash',
  max_input_tokens: 100000, max_output_tokens: 16000, max_tool_calls: 100,
  max_duration_seconds: 1800, max_cost_minor: 1000 };

export function ModelPolicy({ role, value, onChange, errors = {} }: {
  role: string; value: Model; onChange: (value: Model) => void; errors?: Record<string, string>;
}) {
  return <fieldset><legend>{role} model and budget</legend>
    {(['provider', 'model'] as const).map(key => <Field key={key} label={`${role} ${key}`} required
      value={value[key] ?? ''} onChange={text => onChange({ ...value, [key]: text })} error={errors[key]} />)}
    {([
      ['max_input_tokens', 'input tokens', 1], ['max_output_tokens', 'output tokens', 1],
      ['max_tool_calls', 'tool calls', 0], ['max_duration_seconds', 'duration seconds', 1],
      ['max_cost_minor', 'cost limit in minor currency units', 0],
    ] as const).map(([key, label, min]) => <Field key={key} label={`${role} ${label}`} type="number" required min={min}
      value={value[key] ?? defaultModel[key] ?? 0} onChange={text => onChange({ ...value, [key]: Number(text) })} error={errors[key]} />)}
  </fieldset>;
}
