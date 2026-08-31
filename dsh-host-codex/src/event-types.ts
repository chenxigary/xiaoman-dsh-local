/** Browser-safe durable event vocabulary for the Host-owned Codex seam. */

import type {
  CodexApprovalDecision,
  CodexCharacter,
  CodexSafeErrorCode,
  CodexTerminalStatus,
} from './remote-types.ts'

export interface CodexSessionEventMap {
  'codex/user-start': { executionId: string; text: string; character: CodexCharacter }
  'codex/delegation-start': { executionId: string; sessionId: string; character: CodexCharacter }
  'codex/text-delta': { executionId: string; phase: 'commentary' | 'final_answer'; text: string; speakable: boolean; sequence: number }
  'codex/text-final': { executionId: string; text: string }
  'codex/tool-status': { executionId: string; activityId: string; activity: string; status: 'started' | 'progress' | 'completed' | 'denied' | 'failed'; safeSummary?: string }
  'codex/approval-request': { executionId: string; approvalId: string; kind: 'command' | 'file_change' | 'unknown'; safeSummary: string }
  'codex/approval-decision': { executionId: string; approvalId: string; decision: CodexApprovalDecision }
  'codex/interrupt-intent': { executionId: string; reason: 'user' | 'barge-in' | 'mode-switch' | 'shutdown' | 'restart' }
  'codex/terminal': { executionId: string; status: CodexTerminalStatus; reason: string; errorCode?: CodexSafeErrorCode; text?: string }
}
