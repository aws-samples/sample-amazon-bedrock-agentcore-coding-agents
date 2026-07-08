/**
 * Billing types shared across cost estimation and
 * desktop (display). All money values are USD with 8-decimal precision.
 */

export type Tier = 'free' | 'pro' | 'ultra' | 'enterprise';

export type BillingMode = 'subscription' | 'topup' | 'welcome' | 'free_monthly' | 'byok' | 'usage_based';

export type ConsoleModelId =
  | 'claude-fable-5'
  | 'claude-opus-4-6'
  | 'claude-sonnet-4-6'
  | 'claude-haiku-4-5';

/**
 * AgentCore Runtime pricing dimensions (us-east-1 / us-west-2 as of 2026-05-16).
 * Source: agentcore-pricing skill, AWS Pricing API.
 *
 * vCPU is FREE during I/O wait. active_vcpu_seconds = wall_clock * (1 - io_wait_pct).
 * Memory is billed for full wall-clock.
 */
export interface AgentCorePriceList {
  runtime_vcpu_per_hour_usd: number;       // $0.0895
  runtime_mem_gb_per_hour_usd: number;     // $0.00945
  gateway_invocation_usd: number;          // $0.000005
  gateway_search_usd: number;              // $0.000025
  gateway_tool_index_per_month_usd: number;// $0.0002
  gateway_vpc_egress_per_gb_usd: number;   // $0.006
  stm_event_usd: number;                   // $0.00025 (writes only)
  ltm_storage_per_month_usd: number;       // $0.00075 built-in
  ltm_retrieval_usd: number;               // $0.0005
  browser_vcpu_per_hour_usd: number;       // $0.0895
  browser_mem_gb_per_hour_usd: number;     // $0.00945
  ci_vcpu_per_hour_usd: number;            // $0.0895
  ci_mem_gb_per_hour_usd: number;          // $0.00945
  evaluations_input_per_mtok_usd: number;  // $2.40
  evaluations_output_per_mtok_usd: number; // $12.00
}

export interface ComputeUsage {
  /** Active vCPU-seconds (wall_clock * (1 - io_wait_pct)) */
  runtime_vcpu_seconds_active: number;
  /** Memory GB-seconds for full wall_clock */
  runtime_mem_gb_seconds_total: number;
  gateway_invocations: number;
  gateway_search_calls: number;
  gateway_tool_index_count: number;
  gateway_vpc_egress_gb: number;
  stm_events: number;
  ltm_storage_gb_seconds: number;
  ltm_retrievals: number;
  browser_vcpu_seconds_active?: number;
  browser_mem_gb_seconds_total?: number;
  ci_vcpu_seconds_active?: number;
  ci_mem_gb_seconds_total?: number;
}

export interface StorageUsage {
  /** S3 Files GB-seconds */
  storage_gb_seconds: number;
}

export interface LlmUsage {
  model: ConsoleModelId | string;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens?: number;
  cache_write_tokens?: number;
}

/**
 * Per-tier monthly entitlements. Mirrors relay D1 schema decisions.
 * Numbers documented in DESIGN.md.
 *
 * One wallet, two kinds of spend: both LLM tokens and AgentCore compute
 * deduct from the same monthly credit pool at a fixed conversion of
 * 1 credit = $0.04 USD. There is NO separate compute budget.
 */
export interface TierEntitlements {
  /** Total credits per month, used for both LLM and compute charges */
  credits_per_month: number;
  s3_files_gb_quota: number;
  concurrent_tasks_max: number;
  models_allowed: ConsoleModelId[];
  /** If true, additional usage past the cap is billed; if false, hard-blocked */
  overage_allowed: boolean;
}

/** Pro tier conversion is the canonical rate for all tiers. */
export const USD_PER_CREDIT = 0.04;
