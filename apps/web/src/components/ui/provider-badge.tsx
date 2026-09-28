export type ProviderIdentity = 'anthropic' | 'google' | 'openai' | 'neutral';

export interface ProviderCue {
  tone: ProviderIdentity;
  label: string;
}

export function getProviderCue(rawProvider?: string | null): ProviderCue {
  const norm = (rawProvider ?? '').toLowerCase().trim();
  if (['anthropic', 'claude'].includes(norm)) {
    return {
      tone: 'anthropic',
      label: 'Anthropic / Claude',
    };
  }
  if (['google', 'gemini'].includes(norm)) {
    return {
      tone: 'google',
      label: 'Google / Gemini',
    };
  }
  if (['openai', 'codex', 'chatgpt'].includes(norm)) {
    return {
      tone: 'openai',
      label: 'OpenAI',
    };
  }
  return {
    tone: 'neutral',
    label: rawProvider?.trim() || 'Unknown provider',
  };
}

export function ProviderBadge({
  provider,
}: {
  provider?: string | null;
}) {
  const cue = getProviderCue(provider);
  return (
    <span
      className="provider-badge"
      data-provider={cue.tone}
    >
      <span className="provider-badge-indicator" aria-hidden="true" />
      <span className="visually-hidden">Provider: </span>
      <span>{cue.label}</span>
    </span>
  );
}
