/** Shared, JSON-only contract for the host-owned Codex delegation seam. */

import type { SessionEventMap, SessionEventType, AgentCancelCause } from '@deepseek-ai/dsh-session'
import type { Agent } from '@deepseek-ai/dsh-agent'
import type { Branded } from '@deepseek-ai/dsh-brand'
import type {
  CodexApprovalDecision,
  CodexCharacter,
  CodexSafeErrorCode,
  CodexTerminalStatus,
} from './remote-types.ts'

export type {
  CodexAccountProof,
  CodexApprovalDecision,
  CodexApprovalResult,
  CodexCapability,
  CodexCharacter,
  CodexExecutionView,
  CodexInterruptResult,
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
} from './remote-types.ts'

/** Opaque identifiers are branded after Host wire validation. */
export type CodexSessionId = Branded<'CodexSessionId'>
export type CodexExecutionId = Branded<'CodexExecutionId'>
export type CodexThreadId = Branded<'CodexThreadId'>
export type CodexTurnId = Branded<'CodexTurnId'>
export type CodexActivityId = Branded<'CodexActivityId'>
export type CodexApprovalId = Branded<'CodexApprovalId'>
export type CodexLoginId = Branded<'CodexLoginId'>

const MAX_CODEX_ID_LENGTH = 256

function parseId(value: unknown): string | undefined {
  return typeof value === 'string' && value.length > 0 && value.length <= MAX_CODEX_ID_LENGTH ? value : undefined
}

/** Brand a validated session identifier at the Host wire boundary. */
export function CodexSessionId(value: string): CodexSessionId { return value as CodexSessionId }
/** Brand a validated execution identifier at the Host wire boundary. */
export function CodexExecutionId(value: string): CodexExecutionId { return value as CodexExecutionId }
/** Brand a validated thread identifier at the Host wire boundary. */
export function CodexThreadId(value: string): CodexThreadId { return value as CodexThreadId }
/** Brand a validated turn identifier at the Host wire boundary. */
export function CodexTurnId(value: string): CodexTurnId { return value as CodexTurnId }
/** Brand a validated activity identifier at the Host wire boundary. */
export function CodexActivityId(value: string): CodexActivityId { return value as CodexActivityId }
/** Brand a validated approval identifier at the Host wire boundary. */
export function CodexApprovalId(value: string): CodexApprovalId { return value as CodexApprovalId }
/** Brand a validated login identifier at the Host wire boundary. */
export function CodexLoginId(value: string): CodexLoginId { return value as CodexLoginId }

/** Parse a bounded session identifier from an untrusted wire value. */
export function parseCodexSessionId(value: unknown): CodexSessionId | undefined { const parsed = parseId(value); return parsed === undefined ? undefined : CodexSessionId(parsed) }
/** Parse a bounded execution identifier from an untrusted wire value. */
export function parseCodexExecutionId(value: unknown): CodexExecutionId | undefined { const parsed = parseId(value); return parsed === undefined ? undefined : CodexExecutionId(parsed) }
/** Parse a bounded thread identifier from an untrusted wire value. */
export function parseCodexThreadId(value: unknown): CodexThreadId | undefined { const parsed = parseId(value); return parsed === undefined ? undefined : CodexThreadId(parsed) }
/** Parse a bounded turn identifier from an untrusted wire value. */
export function parseCodexTurnId(value: unknown): CodexTurnId | undefined { const parsed = parseId(value); return parsed === undefined ? undefined : CodexTurnId(parsed) }
/** Parse a bounded activity identifier from an untrusted wire value. */
export function parseCodexActivityId(value: unknown): CodexActivityId | undefined { const parsed = parseId(value); return parsed === undefined ? undefined : CodexActivityId(parsed) }
/** Parse a bounded approval identifier from an untrusted wire value. */
export function parseCodexApprovalId(value: unknown): CodexApprovalId | undefined { const parsed = parseId(value); return parsed === undefined ? undefined : CodexApprovalId(parsed) }
/** Parse a bounded login identifier from an untrusted wire value. */
export function parseCodexLoginId(value: unknown): CodexLoginId | undefined { const parsed = parseId(value); return parsed === undefined ? undefined : CodexLoginId(parsed) }

/** Normalized event from the host bridge adapter (never a raw backend packet). */
export type CodexBridgeEvent =
  | { readonly type: 'started'; readonly threadId: CodexThreadId; readonly turnId: CodexTurnId }
  | { readonly type: 'text_delta'; readonly phase: 'commentary' | 'final_answer'; readonly text: string; readonly speakable: boolean }
  | { readonly type: 'tool'; readonly activityId: CodexActivityId; readonly activity: string; readonly status: 'started' | 'progress' | 'completed' | 'denied' | 'failed'; readonly safeSummary?: string }
  | { readonly type: 'approval'; readonly approvalId: CodexApprovalId; readonly kind: 'command' | 'file_change' | 'unknown'; readonly safeSummary: string }
  | { readonly type: 'terminal'; readonly status: CodexTerminalStatus; readonly finalText?: string; readonly errorCode?: CodexSafeErrorCode }

/** Exact identity required for an interrupt; no session-only fallback exists. */
export interface CodexBridgeExecution {
  readonly executionId: CodexExecutionId
  readonly sessionId: CodexSessionId
  readonly threadId: CodexThreadId
  readonly turnId: CodexTurnId
}

/**
 * The only successful outcomes of a process-facing isolate request. `released`
 * means the backend already crossed the normal provider cleanup fence and the
 * Host must preserve any held natural terminal. `isolated` is the only outcome
 * that permits the Host to synthesize `interrupt_isolated`.
 */
export type CodexIsolationOutcome = 'released' | 'isolated'

/**
 * Host transport boundary. The implementation is deliberately injectable so
 * tests can prove ordering without opening a socket; the production adapter
 * is responsible for mapping the existing bridge WS contract to this shape.
 */
export interface CodexBridgeTransport {
  reserve(
    request: {
      readonly executionId: string
      readonly sessionId: string
      readonly text: string
      readonly character: CodexCharacter
      readonly model: string
      readonly reasoningEffort: string
      readonly serviceTier: string | null
      readonly signal: AbortSignal
    },
    onEvent: (event: CodexBridgeEvent) => void,
  ): Promise<CodexBridgeExecution>
  interrupt(execution: CodexBridgeExecution, reason: string): Promise<void>
  /** Optional backend approval operation; absent means no executable turn. */
  approvalDecision?(execution: CodexBridgeExecution, approvalId: CodexApprovalId, decision: CodexApprovalDecision): Promise<void>
  isolate(execution: CodexBridgeExecution): Promise<CodexIsolationOutcome>
  /** Recover an open durable execution after host restart, before any new turn. */
  isolateExecution?(sessionId: CodexSessionId, executionId: CodexExecutionId): Promise<CodexIsolationOutcome>
  close?(): Promise<void>
}

/** The durable event subset used by the coordinator and deterministic tests. */
export type CodexSessionEventType = Extract<SessionEventType, `codex/${string}`>
export type CodexSessionEventData = SessionEventMap[CodexSessionEventType]
export interface CodexDurableEvent {
  readonly type: string
  readonly data: unknown
}

/** The subset of Session used by the coordinator and its deterministic tests. */
export interface CodexDurableSession {
  readonly id: string
  readonly events: readonly CodexDurableEvent[]
  append(type: CodexSessionEventType, data: CodexSessionEventData): unknown
}

/** The lifecycle subset required from a live DSH Agent. */
export interface CodexMaintenanceAgent {
  readonly session: CodexDurableSession
  runMaintenance<T>(task: (signal: AbortSignal) => Promise<T>): Promise<T>
  cancel(cause: AgentCancelCause, options?: { readonly keepInbox?: boolean }): void
  readonly ctx?: Agent['ctx']
}
