import { writeFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import openapiTS, { astToString, COMMENT_HEADER } from 'openapi-typescript';
import ts from 'typescript';

const policyExactFields = ['hard_token_cap', 'hard_context_cap', 'manual_estimate_tokens',
  'history_limit', 'minimum_comparables', 'window_days'];
const tokenCapFields = ['max_input_tokens', 'max_output_tokens'];
const exactFields = {
  AgentModelPolicy: tokenCapFields,
  MonitoringPolicy: [...policyExactFields, 'revision'],
  MonitoringPolicyResponse: [...policyExactFields, 'revision'],
  MonitoringPolicyRevisionRequest: [...policyExactFields, 'expected_revision'],
  TaskBudget: tokenCapFields,
  TokenBudgetDefaultsInput: tokenCapFields,
  BaselineReference: ['checkpoint_threshold_tokens', 'warning_threshold_tokens',
    'reference_tokens', 'coverage_count', 'minimum_comparables', 'outcome_count',
    'policy_revision', 'sample_count', 'source_count', 'window_limit'],
  CheckpointRecord: ['generation', 'monitoring_policy_revision',
    'next_checkpoint_threshold_tokens', 'policy_revision', 'version'],
  OwnerCommandReceipt: ['result_version'],
  TaskUsageOwnerCommandRequest: ['expected_version'],
  TaskUsageHistoryResponse: ['failed_attempt_count', 'excluded_outcomes'],
  UsageHistoryFiltersResponse: ['history_limit', 'minimum_comparables', 'window_days'],
  UsageHistorySummaryResponse: ['coverage_count', 'eligible_count', 'excluded_count',
    'sample_count'],
  UsagePhaseRoleResponse: ['tokens'],
  UsageReport: ['cached_input_tokens', 'checkpoint_version', 'compaction_count',
    'context_capacity_tokens', 'context_occupied_tokens', 'context_peak_tokens',
    'input_tokens', 'output_tokens', 'policy_revision', 'reasoning_output_tokens'],
  UsageSourceReport: ['compaction_count', 'context_capacity_tokens',
    'context_occupied_tokens', 'context_peak_tokens', 'input_tokens', 'output_tokens'],
  UsageSummary: ['cached_input_tokens', 'duration_ms', 'input_tokens',
    'model_calls', 'output_tokens', 'tool_calls'],
};

export async function renderApiTypes() {
  const schema = await openapiTS(new URL('../openapi.json', import.meta.url), {
    transform(value, { path }) {
      const parts = path?.split('/') ?? [];
      if (value.type !== 'integer' || parts[0] !== '#' || parts[1] !== 'components' ||
          parts[2] !== 'schemas' ||
          !exactFields[parts[3]]?.includes(parts[4])) return undefined;
      return ts.factory.createUnionTypeNode([
        ts.factory.createKeywordTypeNode(ts.SyntaxKind.NumberKeyword),
        ts.factory.createKeywordTypeNode(ts.SyntaxKind.StringKeyword),
      ]);
    },
  });
  return COMMENT_HEADER + astToString(schema);
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  await writeFile(new URL('../src/lib/api/schema.d.ts', import.meta.url), await renderApiTypes());
}
