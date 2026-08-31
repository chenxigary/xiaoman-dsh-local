/** Durable, log-only Codex SessionEvent declaration and catalog names. */

import type {
  CodexApprovalDecision,
  CodexCharacter,
  CodexSafeErrorCode,
  CodexTerminalStatus,
} from './remote-types.ts'

export type { CodexSessionEventMap } from './event-types.ts'

/** Names consumed by the managed DSH persistence catalog. */
export const CODEX_SESSION_EVENT_TYPES = [
  'codex/user-start',
  'codex/delegation-start',
  'codex/text-delta',
  'codex/text-final',
  'codex/tool-status',
  'codex/approval-request',
  'codex/approval-decision',
  'codex/interrupt-intent',
  'codex/terminal',
] as const

/**
 * Package-owned declaration merge consumed by DSH's persistence catalog. It
 * is deliberately kept out of `types.ts` so the public Client package has no
 * runtime event registry or Host lifecycle edge.
 */
declare module '@deepseek-ai/dsh-session/types' {
  interface SessionEventMap {
    /** Records the user utterance accepted for one Codex execution. */
    'codex/user-start': { executionId: string; text: string; character: CodexCharacter }
    /** Records the durable delegation boundary before the bridge is contacted. */
    'codex/delegation-start': { executionId: string; sessionId: string; character: CodexCharacter }
    /** Records one bounded, ordered Codex answer delta and whether it is speakable. */
    'codex/text-delta': { executionId: string; phase: 'commentary' | 'final_answer'; text: string; speakable: boolean; sequence: number }
    /** Records the authoritative answer snapshot for a Codex execution. */
    'codex/text-final': { executionId: string; text: string }
    /** Records bounded tool activity without raw command or backend payloads. */
    'codex/tool-status': { executionId: string; activityId: string; activity: string; status: 'started' | 'progress' | 'completed' | 'denied' | 'failed'; safeSummary?: string }
    /** Records a bounded approval request shown to the user. */
    'codex/approval-request': { executionId: string; approvalId: string; kind: 'command' | 'file_change' | 'unknown'; safeSummary: string }
    /** Records the user's one-shot decision for an approval request. */
    'codex/approval-decision': { executionId: string; approvalId: string; decision: CodexApprovalDecision }
    /** Records the durable intent to interrupt one Codex execution. */
    'codex/interrupt-intent': { executionId: string; reason: 'user' | 'barge-in' | 'mode-switch' | 'shutdown' | 'restart' }
    /** Records the sole authoritative terminal for one Codex execution. */
    'codex/terminal': { executionId: string; status: CodexTerminalStatus; reason: string; errorCode?: CodexSafeErrorCode; text?: string }
  }
}
