/**
 * Client vocabulary is assembled by api/remotes from the generated Host
 * Remote. Keep this file as a type-only compatibility facade: no second
 * runtime schema or browser bridge implementation is allowed here.
 */
export type {
  CodexAccountProof,
  CodexApprovalDecision,
  CodexApprovalResult,
  CodexCapability,
  CodexCharacter,
  CodexExecutionView,
  CodexLoginCancelResult,
  CodexLoginStartResult,
  CodexLoginStatusResult,
  CodexModelCatalogResult,
  CodexModelOption,
  CodexModelSelection,
  CodexReasoningEffortOption,
  CodexRuntimeStatus,
  CodexSafeErrorCode,
  CodexStartRequest,
  CodexStartResult,
  CodexStatus,
  CodexServiceTierOption,
  CodexTerminalStatus,
} from '@deepseek-ai/dsh-api-remotes/client'
