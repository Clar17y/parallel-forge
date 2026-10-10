/**
 * Model reasoning strength definitions and compatibility for Forge project API policies.
 */

export type ApiReasoningEffort = 'low' | 'medium' | 'high';

export interface ReasoningSupport {
  supported: boolean;
  supportedEfforts: ApiReasoningEffort[];
  unsupportedReason?: string;
  note?: string;
}

export const reasoningEffortChoices: Array<{
  value: ApiReasoningEffort | '';
  label: string;
}> = [
  { value: '', label: 'Default / automatic' },
  { value: 'low', label: 'Low (faster)' },
  { value: 'medium', label: 'Medium (balanced)' },
  { value: 'high', label: 'High (deeper)' },
];

const BUDGET_REASONING_MODELS = new Set([
  'gemini-2.5-flash',
  'gemini-2.5-pro',
]);

const LEVEL_REASONING_MODELS = new Set([
  'gemini-3.5-flash',
  'gemini-3.8-flash',
  'gemini-3.1-pro-preview',
  'gemini-3.1-pro-preview-customtools',
]);

const RESERVED_CLI_EFFORTS = new Set([
  'low',
  'medium',
  'high',
  'xhigh',
  'max',
  'maximum',
  'ultra',
  'minimal',
  'none',
  'auto',
]);

export function getGeminiCliComposite(model?: string | null): { baseModel: string; effort: string } | null {
  if (!model) return null;
  const lastDash = model.lastIndexOf('-');
  if (lastDash <= 0) return null;
  const prefix = model.slice(0, lastDash);
  const suffix = model.slice(lastDash + 1).toLowerCase();
  if (!prefix.startsWith('gemini-')) return null;
  if (RESERVED_CLI_EFFORTS.has(suffix)) {
    return { baseModel: prefix, effort: suffix };
  }
  return null;
}

export function getModelReasoningSupport(
  provider?: string | null,
  model?: string | null
): ReasoningSupport {
  const exactProvider = provider ?? 'google';
  const exactModel = model ?? '';

  if (exactProvider !== 'google') {
    return {
      supported: false,
      supportedEfforts: [],
      unsupportedReason: `Reasoning strength is not supported for provider "${provider}".`,
    };
  }

  const cliComposite = getGeminiCliComposite(exactModel);
  if (cliComposite) {
    return {
      supported: false,
      supportedEfforts: [],
      unsupportedReason: `Model "${exactModel}" is a CLI composite identity; use base API model "${cliComposite.baseModel}" with separately selected reasoning instead.`,
    };
  }

  if (BUDGET_REASONING_MODELS.has(exactModel)) {
    return {
      supported: true,
      supportedEfforts: ['low', 'medium', 'high'],
      note: 'Preset thinking budgets for 2.5: Low (1,024 tokens), Medium (4,096 tokens), High (8,192 tokens). Output token limits remain unchanged.',
    };
  }

  if (LEVEL_REASONING_MODELS.has(exactModel)) {
    return {
      supported: true,
      supportedEfforts: ['low', 'medium', 'high'],
    };
  }

  return {
    supported: false,
    supportedEfforts: [],
    unsupportedReason: `Reasoning strength is not supported for model "${model}".`,
  };
}

export function validateReasoningSetting(
  provider?: string | null,
  model?: string | null,
  effort?: ApiReasoningEffort | string | null
): string | null {
  const exactModel = model ?? '';
  const cliComposite = (provider ?? 'google') === 'google' ? getGeminiCliComposite(exactModel) : null;
  if (cliComposite) {
    return `Model "${exactModel}" is a CLI composite identity; use base API model "${cliComposite.baseModel}" with separately selected reasoning instead.`;
  }
  if (!effort) {
    return null;
  }
  const support = getModelReasoningSupport(provider, model);
  if (!support.supported) {
    return support.unsupportedReason ?? `Reasoning strength is not supported for ${model ?? 'unspecified model'}.`;
  }
  if (!support.supportedEfforts.includes(effort as ApiReasoningEffort)) {
    return `Reasoning strength "${effort}" is not supported for ${model}. Supported choices: ${support.supportedEfforts.join(', ')}.`;
  }
  return null;
}
