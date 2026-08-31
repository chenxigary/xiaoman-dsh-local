/**
 * Client-side durable Codex event declaration merge.
 *
 * This TSX leaf is excluded from the pinned persistence source glob, so the
 * Host package remains the single owning event vocabulary. The Client
 * compiler imports this file type-only from session-events.ts.
 */
import type {
  ClientCodexApprovalDecision,
  ClientCodexCharacter,
  ClientCodexSafeErrorCode,
  ClientCodexTerminalStatus,
} from './session-events.ts'

declare module '@deepseek-ai/dsh-session/types' {
  interface SessionEventMap {
    /** Records the user utterance accepted for one Codex execution. */
    'codex/user-start': { executionId: string; text: string; character: ClientCodexCharacter }
    /** Records the durable delegation boundary before the bridge is contacted. */
    'codex/delegation-start': { executionId: string; sessionId: string; character: ClientCodexCharacter }
    /** Records one bounded, ordered Codex answer delta and whether it is speakable. */
    'codex/text-delta': { executionId: string; phase: 'commentary' | 'final_answer'; text: string; speakable: boolean; sequence: number }
    /** Records the authoritative answer snapshot for a Codex execution. */
    'codex/text-final': { executionId: string; text: string }
    /** Records bounded tool activity without raw command or backend payloads. */
    'codex/tool-status': { executionId: string; activityId: string; activity: string; status: 'started' | 'progress' | 'completed' | 'denied' | 'failed'; safeSummary?: string }
    /** Records a bounded approval request shown to the user. */
    'codex/approval-request': { executionId: string; approvalId: string; kind: 'command' | 'file_change' | 'unknown'; safeSummary: string }
    /** Records the user's one-shot decision for an approval request. */
    'codex/approval-decision': { executionId: string; approvalId: string; decision: ClientCodexApprovalDecision }
    /** Records the durable intent to interrupt one Codex execution. */
    'codex/interrupt-intent': { executionId: string; reason: 'user' | 'barge-in' | 'mode-switch' | 'shutdown' | 'restart' }
    /** Records the sole authoritative terminal for one Codex execution. */
    'codex/terminal': { executionId: string; status: ClientCodexTerminalStatus; reason: string; errorCode?: ClientCodexSafeErrorCode; text?: string }
  }
}
