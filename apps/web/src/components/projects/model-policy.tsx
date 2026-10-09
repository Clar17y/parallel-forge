import { useEffect, useId, useRef } from 'react';
import type { components } from '@/lib/api/schema';
import { Field } from './form-fields';
import { TokenBudgetSlider } from '../model-selection/token-budget-slider';
import {
  getModelReasoningSupport,
  reasoningEffortChoices,
  validateReasoningSetting,
  type ApiReasoningEffort,
} from '../model-selection/api-reasoning';

type Model = components['schemas']['AgentModelPolicy'];
const googleModelChoices = [
  ['gemini-2.5-flash', 'Gemini 2.5 Flash'],
  ['gemini-2.5-pro', 'Gemini 2.5 Pro'],
  ['gemini-3.5-flash', 'Gemini 3.5 Flash'],
  ['gemini-3.8-flash', 'Gemini 3.8 Flash'],
] as const;
export const defaultModel: Model = {
  provider: 'google',
  model: 'gemini-3.5-flash',
  max_input_tokens: 100000,
  max_output_tokens: 16000,
  max_tool_calls: 100,
  max_duration_seconds: 1800,
  max_cost_minor: 1000,
  reasoning_effort: null,
};

export function ModelPolicy({ role, value, onChange, errors = {} }: {
  role: string; value: Model; onChange: (value: Model) => void; errors?: Record<string, string>;
}) {
  const reasoningId = useId();
  const reasoningSelect = useRef<HTMLSelectElement>(null);
  const reasoningSupport = getModelReasoningSupport(value.provider, value.model);
  const reasoningError =
    errors.reasoning_effort ??
    validateReasoningSetting(
      value.provider,
      value.model,
      value.reasoning_effort as ApiReasoningEffort | null
    );
  const helpId = `${reasoningId}-help`;
  const errorId = `${reasoningId}-error`;

  useEffect(() => {
    reasoningSelect.current?.setCustomValidity(validateReasoningSetting(
      value.provider,
      value.model,
      value.reasoning_effort as ApiReasoningEffort | null
    ) ?? '');
  }, [value.provider, value.model, value.reasoning_effort]);

  return (
    <fieldset>
      <legend>{role} model and budget</legend>
      <label>
        {role} model
        <select
          aria-label={`${role} model`}
          value={JSON.stringify([value.provider ?? 'google', value.model])}
          onChange={event => {
            const [provider, model] = JSON.parse(event.target.value) as [string, string];
            onChange({ ...value, provider, model });
          }}
        >
          {googleModelChoices.map(([model, label]) => (
            <option key={model} value={JSON.stringify(['google', model])}>
              {label}
            </option>
          ))}
          {(!googleModelChoices.some(([model]) => value.provider === 'google' && value.model === model)) && (
            <option value={JSON.stringify([value.provider ?? '', value.model])}>Custom / saved: {value.model}</option>
          )}
        </select>
      </label>
      <label htmlFor={reasoningId}>
        {role} reasoning strength
        <select
          ref={reasoningSelect}
          id={reasoningId}
          aria-label={`${role} reasoning strength`}
          aria-invalid={reasoningError ? 'true' : undefined}
          aria-describedby={reasoningError ? errorId : helpId}
          value={value.reasoning_effort ?? ''}
          onChange={event => {
            const raw = event.target.value;
            const effort = raw ? (raw as ApiReasoningEffort) : null;
            onChange({ ...value, reasoning_effort: effort });
          }}
        >
          {reasoningEffortChoices.filter(choice =>
            choice.value === '' || reasoningSupport.supportedEfforts.includes(choice.value)
          ).map(choice => (
            <option key={choice.value} value={choice.value}>
              {choice.label}
            </option>
          ))}
          {value.reasoning_effort &&
            !reasoningSupport.supportedEfforts.includes(
              value.reasoning_effort as ApiReasoningEffort
            ) && (
              <option value={value.reasoning_effort}>
                Unsupported: {value.reasoning_effort}
              </option>
            )}
        </select>
      </label>
      <p
        id={helpId}
        className="field-hint"
        style={{
          fontSize: '0.8rem',
          color: 'var(--muted-foreground, #666)',
          margin: '0 0 4px',
        }}
      >
        Token limits cap usage while reasoning strength controls thinking.
        {reasoningSupport.note && ` ${reasoningSupport.note}`}
      </p>
      {reasoningError && (
        <p
          id={errorId}
          role="alert"
          style={{
            fontSize: '0.8rem',
            color: 'var(--destructive, #d32f2f)',
            margin: '0 0 4px',
          }}
        >
          {reasoningError}
        </p>
      )}
      <details>
        <summary>Advanced model and budget</summary>
        <Field
          label={`${role} provider`}
          required
          value={value.provider ?? ''}
          onChange={text => onChange({ ...value, provider: text })}
          error={errors.provider}
        />
        <Field
          label={`${role} custom model`}
          required
          value={value.model ?? ''}
          onChange={text => onChange({ ...value, model: text })}
          error={errors.model}
        />
        <TokenBudgetSlider
          label={`${role} input tokens`}
          dimension="input"
          provider={value.provider}
          model={value.model}
          min={1}
          value={value.max_input_tokens ?? defaultModel.max_input_tokens!}
          error={errors.max_input_tokens}
          onChange={max_input_tokens => onChange({ ...value, max_input_tokens })}
        />
        <TokenBudgetSlider
          label={`${role} output tokens`}
          dimension="output"
          provider={value.provider}
          model={value.model}
          min={1}
          value={value.max_output_tokens ?? defaultModel.max_output_tokens!}
          error={errors.max_output_tokens}
          onChange={max_output_tokens => onChange({ ...value, max_output_tokens })}
        />
        {([
      ['max_tool_calls', 'tool calls', 0], ['max_duration_seconds', 'duration seconds', 1],
      ['max_cost_minor', 'cost limit in minor currency units', 0],
        ] as const).map(([key, label, min]) => (
          <Field
            key={key}
            label={`${role} ${label}`}
            type="number"
            required
            min={min}
            value={value[key] ?? defaultModel[key] ?? 0}
            onChange={text => onChange({ ...value, [key]: Number(text) })}
            error={errors[key]}
          />
        ))}
      </details>
    </fieldset>
  );
}
