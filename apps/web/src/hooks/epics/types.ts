import type { components } from '@/lib/api/schema';

// Landed Epic Schemas
export type EpicResponse = components['schemas']['EpicResponse'];
export type EpicCreateRequest = components['schemas']['EpicCreateRequest'];
export type EpicDraftUpdateRequest = components['schemas']['EpicDraftUpdateRequest'];
export type BriefContent = components['schemas']['BriefContent'];
export type BriefRequirement = components['schemas']['BriefRequirement'];
export type BriefRevisionResponse = components['schemas']['BriefRevisionResponse'];
export type BriefRevisionCreateRequest = components['schemas']['BriefRevisionCreateRequest'];
export type BriefAdoptionRequest = components['schemas']['BriefAdoptionRequest'];
export type AcceptedBriefResponse = components['schemas']['AcceptedBriefResponse'];
export type AcceptedGraphResponse = components['schemas']['AcceptedGraphResponse'];
export type GraphRevisionResponse = components['schemas']['GraphRevisionResponse'];
export type GraphRevisionCreateRequest = components['schemas']['GraphRevisionCreateRequest'];
export type GraphAdoptionRequest = components['schemas']['GraphAdoptionRequest'];
export type ItemInput = components['schemas']['ItemInput'];
export type ItemSnapshot = components['schemas']['ItemSnapshot'];
export type ItemReadiness = components['schemas']['ItemReadiness'];

export const EMPTY_BRIEF: BriefContent = {
  schema_version: 1,
  problem: '',
  outcomes: [],
  scope: [],
  exclusions: [],
  requirements: [],
  decisions: [],
  assumptions: [],
  open_questions: [],
};

// Brainstorming / Authoring Schemas
export type BrainstormRole = 'operator' | 'assistant';

export interface BrainstormEvidence {
  schema_version: 1;
  path: string;
  content_digest: string;
  excerpt: string;
}

export interface BrainstormProposal {
  schema_version: 1;
  turn_id: string;
  problem: string;
  outcomes: string[];
  scope: string[];
  exclusions: string[];
  requirements: string[];
  requirement_criteria?: Record<string, string[]>;
  decisions: string[];
  assumptions: string[];
  open_questions: string[];
  resolved_turn_ids?: string[];
  evidence?: BrainstormEvidence[];
}

export interface BrainstormTurn {
  schema_version: 1;
  turn_id: string;
  conversation_id: string;
  role: BrainstormRole;
  text: string;
  pending?: boolean;
  proposal?: BrainstormProposal | DecompositionProposal | null;
}

export interface BrainstormThread {
  conversation_id: string;
  conversation_version: number;
  job_ids: string[];
}

export type BrainstormState =
  | 'queued'
  | 'running'
  | 'quota_wait'
  | 'capacity_wait'
  | 'cancel_requested'
  | 'cancelled'
  | 'proposed'
  | 'failed'
  | 'reconciling';

export interface AuthoringReceipt {
  schema_version: 1;
  job_id: string;
  job_version: number;
  state: BrainstormState;
  replay_key: string;
}

export interface AuthoringOutcome {
  schema_version: 1;
  job_id: string;
  job_version: number;
  state: BrainstormState;
  proposal_digest?: string | null;
  proposal?: BrainstormProposal | DecompositionProposal | null;
  adopted_revision_id?: string | null;
  failure?: string | null;
  usage_known?: boolean | null;
  process_settled?: boolean;
  usage?: {
    schema_version: 1;
    duration_ms: number | null;
    duration_lower_bound_ms: number;
    tool_call_count: number;
    input_tokens: number | null;
    output_tokens: number | null;
    estimated_api_cost_minor: number | null;
    unknown_fields: Array<'duration_ms' | 'tool_call_count' | 'input_tokens' | 'output_tokens' | 'estimated_api_cost_minor'>;
  } | null;
  unknown_usage_fields?: string[];
  currency?: string | null;
  reservation?: {
    schema_version: 1;
    duration_ms: number;
    tool_call_count: number;
    input_tokens: number | null;
    output_tokens: number | null;
    estimated_api_cost_minor: number | null;
  } | null;
  cumulative_usage?: {
    schema_version: 1;
    duration_ms: number;
    tool_call_count: number;
    input_tokens: number;
    output_tokens: number;
    estimated_api_cost_minor: number;
  };
  held_reservations?: {
    schema_version: 1;
    duration_ms: number;
    tool_call_count: number;
    input_tokens: number;
    output_tokens: number;
    estimated_api_cost_minor: number;
  };
  uncertain_attempts?: number;
  held_reasons?: {
    schema_version: 1;
    duration_ms: 'unsettled_or_unknown' | null;
    tool_call_count: 'unsettled_or_unknown' | null;
    input_tokens: 'unsettled_or_unknown' | null;
    output_tokens: 'unsettled_or_unknown' | null;
    estimated_api_cost_minor: 'unsettled_or_unknown' | null;
  };
}

// Delivery / Execution Schemas
export type EpicItemEligibility = components['schemas']['EpicItemEligibility'];
export type EpicExecutionDispatch = components['schemas']['EpicDispatchProjection'];
export type EpicDispatchRequest = components['schemas']['EpicDispatchRequest'];
export type EpicExecutionProjection = components['schemas']['EpicExecutionProjection'];
export type EpicExecutionSnapshot = components['schemas']['EpicExecutionSnapshot'];
export type EpicChildProjection = components['schemas']['EpicChildProjection'];
export type EpicAttempt = components['schemas']['EpicAttempt'];
export type EpicAttemptResponse = components['schemas']['EpicAttemptResponse'];
export type EpicLaunchRequest = components['schemas']['EpicLaunchRequest'];
export type EpicControlRequest = components['schemas']['EpicControlRequest'];
export type EpicControlReceipt = components['schemas']['EpicControlReceipt'];
export type ExecutionStartRequest = components['schemas']['ExecutionStartRequest'];
export type EpicIntentProjection = components['schemas']['EpicIntentProjection'];
export type EpicOwnerActionProjection = components['schemas']['EpicOwnerActionProjection'];
export type ApprovalGate = components['schemas']['ApprovalGate'];
export type RunState = components['schemas']['RunState'];

// Budget Schemas
export type EpicBudgetProjection = components['schemas']['EpicBudgetProjection'];
export type EpicBudgetEdit = components['schemas']['EpicBudgetEdit'];
export type EpicBudgetReceipt = components['schemas']['EpicBudgetReceipt'];
export type EpicBudgetPermitRequest = components['schemas']['EpicBudgetPermitRequest'];
export type EpicBudgetPermitReceipt = components['schemas']['EpicBudgetPermitReceipt'];
export type EpicBudgetOwnerAction = components['schemas']['EpicBudgetOwnerAction'];
export type EpicBudgetPermitProjection = components['schemas']['EpicBudgetPermitProjection'];
export type TaskBudget = components['schemas']['TaskBudget'];

export const SHARED_BUDGET_DIMENSIONS = [
  'duration_ms',
  'tool_call_count',
  'input_tokens',
  'output_tokens',
  'estimated_api_cost_minor',
  'provider_attempts',
] as const;
export type SharedBudgetDimension = (typeof SHARED_BUDGET_DIMENSIONS)[number];

// Authoring Retry Schemas
export type JobRetry = components['schemas']['JobRetry'];
export type DecompositionJobRetry = components['schemas']['DecompositionJobRetry'];

// Decomposition Schemas
export type DecompositionProposal = components['schemas']['DecompositionProposal'];
export type DecompositionEvidence = components['schemas']['DecompositionEvidence'];
export type DecompositionAdoptionResponse = components['schemas']['DecompositionAdoptionResponse'];
