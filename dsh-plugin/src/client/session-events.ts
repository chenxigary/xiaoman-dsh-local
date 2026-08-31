/**
 * Browser-safe type-only entrypoint for the durable Codex event seam.
 *
 * The browser imports SessionEvent through api/remotes/runtime. It must also
 * load this declaration merge explicitly; importing a Host implementation (or
 * relying on a Host-only augmentation) would make the assembled client type
 * graph unsound. No value or runtime dependency is emitted from this file.
 */
import type { SessionEvent, SessionEventMap } from '@deepseek-ai/dsh-session/types'
// Keep the declaration merge in a TSX leaf: the persistence catalog scans
// package `src/**/*.ts` and must have one owning declaration, while the Client
// compiler still sees this type-only augmentation through the import graph.
import type {} from './session-events-augmentation.tsx'

/** Duplicated only as JSON vocabulary so this entrypoint stays Host-free. */
export type ClientCodexCharacter = 'default' | 'xiaoman'
export type ClientCodexTerminalStatus = 'completed' | 'interrupted' | 'failed'
export type ClientCodexApprovalDecision = 'accept' | 'decline' | 'cancel'
export type ClientCodexSafeErrorCode =
  | 'not_authenticated' | 'bridge_unavailable' | 'bridge_protocol' | 'turn_in_progress'
  | 'turn_failed' | 'interrupt_timeout' | 'invalid_request' | 'approval_unavailable'
  | 'host_restart' | 'interrupt_isolated' | 'isolation_failed'
  | 'mapping_commit_failed' | 'security_isolation_unavailable' | 'internal_error'

export type CodexEventType =
  | 'codex/user-start'
  | 'codex/delegation-start'
  | 'codex/text-delta'
  | 'codex/text-final'
  | 'codex/tool-status'
  | 'codex/approval-request'
  | 'codex/approval-decision'
  | 'codex/interrupt-intent'
  | 'codex/terminal'

export interface CodexSessionEventMap {
  'codex/user-start': { executionId: string; text: string; character: ClientCodexCharacter }
  'codex/delegation-start': { executionId: string; sessionId: string; character: ClientCodexCharacter }
  'codex/text-delta': { executionId: string; phase: 'commentary' | 'final_answer'; text: string; speakable: boolean; sequence: number }
  'codex/text-final': { executionId: string; text: string }
  'codex/tool-status': { executionId: string; activityId: string; activity: string; status: 'started' | 'progress' | 'completed' | 'denied' | 'failed'; safeSummary?: string }
  'codex/approval-request': { executionId: string; approvalId: string; kind: 'command' | 'file_change' | 'unknown'; safeSummary: string }
  'codex/approval-decision': { executionId: string; approvalId: string; decision: ClientCodexApprovalDecision }
  'codex/interrupt-intent': { executionId: string; reason: 'user' | 'barge-in' | 'mode-switch' | 'shutdown' | 'restart' }
  'codex/terminal': { executionId: string; status: ClientCodexTerminalStatus; reason: string; errorCode?: ClientCodexSafeErrorCode; text?: string }
}

export type CodexSessionEvent<T extends CodexEventType = CodexEventType> = SessionEvent<T>

// Keep the imported map in the declaration-only graph so an assembled client
// catches a missing merge instead of eliding this entrypoint as unused.
export type ClientSessionEventMap = SessionEventMap
