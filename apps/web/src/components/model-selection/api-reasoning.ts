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

  // Gemini 2.5 Flash / Pro
  if (matchesExact(/^gemini-2\.5-(?:flash|pro)(?:-[a-z0-9.]+)?$/, exactModel)) {
    return {
      supported: true,
      supportedEfforts: ['low', 'medium', 'high'],
      note: 'Preset thinking budgets for 2.5: Low (1,024 tokens), Medium (4,096 tokens), High (8,192 tokens). Output token limits remain unchanged.',
    };
  }

  // Gemini 3.8 Flash, 3.5 Flash, 3.1 Pro
  if (matchesExact(/^gemini-(?:3\.[58]-flash|3\.1-pro)(?:-[a-z0-9.]+)?$/, exactModel)) {
    return {
      supported: true,
      supportedEfforts: ['low', 'medium', 'high'],
    };
  }

  // Older Gemini 3 Pro
  if (matchesExact(/^gemini-3(?:\.0)?-pro(?:-[a-z0-9.]+)?$/, exactModel)) {
    return {
      supported: true,
      supportedEfforts: ['low', 'high'],
      note: 'Gemini 3 Pro supports Low and High reasoning strength only.',
    };
  }

  return {
    supported: false,
    supportedEfforts: [],
    unsupportedReason: `Reasoning strength is not supported for model "${model}".`,
  };
}

function matchesExact(pattern: RegExp, value: string): boolean {
  return pattern.exec(value)?.[0] === value;
}

export function validateReasoningSetting(
  provider?: string | null,
  model?: string | null,
  effort?: ApiReasoningEffort | string | null
): string | null {
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
