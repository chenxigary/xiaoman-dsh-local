/**
 * Browser-safe public contract for the Host-owned Codex Remote.
 *
 * This file is intentionally limited to JSON-shaped values and primitive
 * identifiers.  It must not import Cordis, dsh-agent, or the Session runtime:
 * those types augment the Host Context and are not part of the Client graph.
 * Host implementation-only transport/session types live in `types.ts`.
 */

export type CodexCharacter = 'default' | 'xiaoman'

export type CodexSafeErrorCode =
  | 'not_authenticated'
  | 'bridge_unavailable'
  | 'bridge_protocol'
  | 'turn_in_progress'
  | 'turn_failed'
  | 'interrupt_timeout'
  | 'invalid_request'
  | 'approval_unavailable'
  | 'host_restart'
  | 'interrupt_isolated'
  | 'isolation_failed'
  | 'mapping_commit_failed'
  | 'security_isolation_unavailable'
  | 'internal_error'

export type CodexTerminalStatus = 'completed' | 'interrupted' | 'failed'
export type CodexApprovalDecision = 'accept' | 'decline' | 'cancel'

export interface CodexAccountProof {
  readonly type: 'chatgpt'
  readonly planType:
    | 'free' | 'go' | 'plus' | 'pro' | 'prolite' | 'team'
    | 'self_serve_business_prolite' | 'self_serve_business_usage_based'
    | 'business' | 'ent26' | 'enterprise_cbp_automation'
    | 'enterprise_cbp_usage_based' | 'enterprise' | 'edu' | 'unknown'
}

/** The split-process backend currently exposes read-only turns only. */
export type CodexCapability = 'read-only' | 'unavailable'

export interface CodexStatus {
  readonly capability: CodexCapability
  readonly loggedIn: boolean
  readonly requiresOpenAiAuth: boolean
  readonly account: CodexAccountProof | null
  readonly loginUrl: 'https://chatgpt.com/auth/login'
  readonly message?: 'ChatGPT login required' | 'Codex bridge unavailable' | 'Codex turn execution unavailable'
}

/** Picker-safe subset of one App Server reasoning option. */
export interface CodexReasoningEffortOption {
  readonly id: string
  readonly description: string
}

/** Picker-safe subset of one App Server service tier. */
export interface CodexServiceTierOption {
  readonly id: string
  readonly name: string
  readonly description: string
}

/** Browser-safe model row produced from the Host-owned App Server catalog. */
export interface CodexModelOption {
  readonly id: string
  readonly displayName: string
  readonly description: string
  readonly defaultReasoningEffort: string
  readonly supportedReasoningEfforts: readonly CodexReasoningEffortOption[]
  readonly serviceTiers: readonly CodexServiceTierOption[]
}

export interface CodexModelCatalogResult {
  readonly models: readonly CodexModelOption[]
}

/** Explicit next-turn overrides selected in the resident composer seat. */
export interface CodexModelSelection {
  readonly model: string
  readonly reasoningEffort: string
  /** null selects the ordinary non-Fast tier. */
  readonly serviceTier: string | null
}

export interface CodexStartRequest {
  readonly text: string
  readonly character?: CodexCharacter
  readonly model?: string
  readonly reasoningEffort?: string
  readonly serviceTier?: string | null
}

export interface CodexStartResult {
  readonly executionId: string
}

export interface CodexInterruptResult {
  readonly executionId: string
  readonly accepted: true
}

export interface CodexApprovalResult {
  readonly executionId: string
  readonly approvalId: string
  readonly decision: CodexApprovalDecision
}

export interface CodexExecutionView {
  readonly executionId: string
  readonly state: 'starting' | 'running' | 'settling' | 'terminal' | 'blocked'
  readonly terminal?: CodexTerminalStatus
}

export interface CodexRuntimeStatus extends CodexStatus {
  readonly executions: readonly CodexExecutionView[]
}

export interface CodexLoginStartResult {
  readonly loginId: string
  readonly status: 'pending'
  readonly authUrl: string
}

export interface CodexLoginStatusResult {
  readonly loginId: string
  readonly status: 'pending' | 'completed' | 'failed' | 'canceled' | 'not_found'
  readonly success?: boolean
}

export interface CodexLoginCancelResult {
  readonly loginId: string
  /** Idempotent owner release: the backend may have won the race already. */
  readonly status: 'canceled' | 'completed' | 'failed' | 'not_found'
  readonly success: false
}
