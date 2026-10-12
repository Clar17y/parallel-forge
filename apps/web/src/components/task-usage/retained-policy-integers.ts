type RetainedPolicyIntegers = {
  manual_estimate_tokens?: number | null;
  hard_token_cap?: number | null;
  hard_context_cap?: number | null;
};

export function retainedPolicyIntegersAreSafe(policy: RetainedPolicyIntegers): boolean {
  return [policy.manual_estimate_tokens, policy.hard_token_cap, policy.hard_context_cap]
    .every(value => value == null || (Number.isSafeInteger(value) && value >= 0));
}
