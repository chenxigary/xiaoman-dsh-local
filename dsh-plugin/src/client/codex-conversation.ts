/** Durable Codex delegated-turn -> DSH ConversationNodeDefinition projection. */
import type { Context } from '@deepseek-ai/cordis'
import type {
  ConversationNodeContext,
  ConversationNodeDefinition,
  ConversationViewNode,
} from '@deepseek-ai/dsh-client-runtime/client'
import type { SessionEvent } from '@deepseek-ai/dsh-session/types'
import type { CodexCharacter, CodexTerminalStatus } from '../types.ts'
import type { CodexEventType } from './session-events.ts'
import type {} from './session-events.ts'
import type {} from '@deepseek-ai/dsh-client-ui-conversation/client'

export interface CodexAnswerChatData {
  readonly executionId: string
  readonly character: CodexCharacter
  readonly text: string
  readonly status: 'running' | CodexTerminalStatus
  readonly phase: 'commentary' | 'final_answer'
  readonly speakable: boolean
  readonly sequence: number
  /** A missing delta sequence is a protocol fault; no text is spoken until a final converges. */
  readonly sequenceGap: boolean
  readonly safeToolSummary?: string
}

export interface CodexUserChatData {
  readonly executionId: string
  readonly character: CodexCharacter
  readonly text: string
}

declare module '@deepseek-ai/dsh-client-ui-conversation/client' {
  interface ChatNodeDataMap {
    /** Durable user request handed to the Codex host seam. */
    'codex-user': CodexUserChatData
    /** Host-owned Codex visible answer; never a native DSH assistant message. */
    'codex-answer': CodexAnswerChatData
  }
}

type CodexEvent = Extract<SessionEvent, { type: CodexEventType }>

interface CodexAnswerState {
  readonly executionId: string
  readonly character: CodexCharacter
  readonly text: string
  readonly status: 'running' | CodexTerminalStatus
  readonly phase: 'commentary' | 'final_answer'
  readonly speakable: boolean
  readonly sequence: number
  readonly sequenceGap: boolean
  /** `text-final` is only a candidate until the terminal event authorizes it. */
  readonly pendingFinalText?: string | undefined
  readonly safeToolSummary?: string
}

function isCodexEvent(event: SessionEvent): event is Extract<SessionEvent, { type: CodexEventType }> {
  switch (event.type) {
    case 'codex/user-start':
    case 'codex/delegation-start':
    case 'codex/text-delta':
    case 'codex/text-final':
    case 'codex/tool-status':
    case 'codex/approval-request':
    case 'codex/approval-decision':
    case 'codex/interrupt-intent':
    case 'codex/terminal':
      return true
    default:
      return false
  }
}

function executionIdOf(event: CodexEvent): string {
  return event.data.executionId
}

function characterOf(value: CodexCharacter): CodexCharacter {
  return value
}

function answerNode<
  State,
>(context: ConversationNodeContext<State>, kind: 'codex-user' | 'codex-answer', data: CodexUserChatData | CodexAnswerChatData): ConversationViewNode {
  const location = context.start?.location ?? context.matches[0]?.location ?? { kind: 'unresolved' as const }
  return {
    key: context.key,
    kind,
    id: context.id,
    target: 'chat',
    anchorSeq: context.start?.event.seq ?? context.matches[0]?.event.seq ?? 0,
    location,
    visibility: 'visible',
    data,
  } as ConversationViewNode
}

export const codexUserConversationDefinition: ConversationNodeDefinition<CodexUserChatData> = {
  kind: 'codex-delegated-user',
  target: 'chat',
  match: event => {
    if (event.type !== 'codex/user-start') return null
    return { id: event.data.executionId, role: 'start' }
  },
  start: (_context, match) => {
    if (match.event.type !== 'codex/user-start') throw new Error('codex user requires user-start')
    return {
      executionId: match.event.data.executionId,
      character: characterOf(match.event.data.character),
      text: match.event.data.text,
    }
  },
  update: context => context.state,
  buildViewNode: context => context.state === undefined
    ? null
    : answerNode(context, 'codex-user', context.state),
}

export const codexConversationDefinition: ConversationNodeDefinition<CodexAnswerState> = {
  kind: 'codex-delegated-answer',
  target: 'chat',
  match: event => {
    if (!isCodexEvent(event) || event.type === 'codex/user-start') return null
    const executionId = executionIdOf(event)
    return {
      id: executionId,
      role: event.type === 'codex/delegation-start' ? 'start' : 'update',
    }
  },
  start: (_context, match) => {
    if (match.event.type !== 'codex/delegation-start') {
      throw new Error('codex delegated answer requires delegation-start')
    }
    const start = match.event.data
    return {
      executionId: start.executionId,
      character: characterOf(start.character),
      // User text is a separate durable `codex-user` node. Never seed the
      // answer state from it: the sentence TTS projection must not read the
      // prompt back as if it were a Codex answer.
      text: '',
      status: 'running',
      phase: 'final_answer',
      speakable: false,
      // Host coordinator state starts at 0 and increments before publishing
      // each delta, therefore the first real delta is sequence 1.
      sequence: 0,
      sequenceGap: false,
    }
  },
  update: (context, match) => {
    if (!isCodexEvent(match.event)) return context.state
    const event = match.event
    const state = context.state
    if (event.data.executionId !== state.executionId) return state
    // The first terminal event is authoritative.  A replayed terminal or a
    // late delta must not reopen or mutate a durable answer node.
    if (state.status !== 'running') return state
    switch (event.type) {
      case 'codex/text-delta': {
        if (event.data.sequence <= state.sequence) return state
        if (event.data.sequence !== state.sequence + 1) {
          return {
            ...state,
            // Preserve the observed high-water mark for duplicate fencing,
            // but make the protocol fault explicit and non-speakable.
            sequence: event.data.sequence,
            phase: event.data.phase,
            speakable: false,
            sequenceGap: true,
            pendingFinalText: undefined,
          }
        }
        const finalAnswer = event.data.phase === 'final_answer' && event.data.speakable
        if (state.sequenceGap) {
          return {
            ...state,
            phase: event.data.phase,
            speakable: false,
            sequence: event.data.sequence,
            pendingFinalText: undefined,
          }
        }
        // A commentary event may legally follow a speakable final delta. It
        // must still advance the high-water sequence, otherwise the next
        // final delta is falsely classified as a gap. Preserve the already
        // authorized final projection while ignoring commentary text.
        if (state.phase === 'final_answer' && state.speakable && !finalAnswer) {
          return { ...state, sequence: event.data.sequence, pendingFinalText: undefined }
        }
        return {
          ...state,
          phase: event.data.phase,
          speakable: finalAnswer,
          text: finalAnswer ? state.text + event.data.text : state.text,
          sequence: event.data.sequence,
          sequenceGap: false,
          pendingFinalText: undefined,
        }
      }
      case 'codex/text-final':
        if (state.pendingFinalText === event.data.text && !state.speakable) return state
        return {
          ...state,
          phase: 'final_answer',
          // Host appends text-final before the terminal event. Keep it
          // visible but non-speakable until a completed terminal converges.
          speakable: false,
          text: event.data.text,
          sequenceGap: false,
          pendingFinalText: event.data.text,
        }
      case 'codex/tool-status':
        return event.data.safeSummary === undefined
          ? state
          : { ...state, safeToolSummary: event.data.safeSummary }
      case 'codex/terminal':
        if (event.data.status !== 'completed') {
          return {
            ...state,
            status: event.data.status,
            speakable: false,
            pendingFinalText: undefined,
          }
        }
        if (state.pendingFinalText !== undefined && !state.sequenceGap) {
          return {
            ...state,
            status: 'completed',
            phase: 'final_answer',
            text: state.pendingFinalText,
            speakable: true,
            pendingFinalText: undefined,
          }
        }
        return { ...state, status: 'completed', pendingFinalText: undefined }
      default:
        return state
    }
  },
  publication: () => 'immediate',
  buildViewNode: context => context.state === undefined
    ? null
    : answerNode(context, 'codex-answer', {
      executionId: context.state.executionId,
      character: context.state.character,
      text: context.state.text,
      status: context.state.status,
      phase: context.state.phase,
      speakable: context.state.speakable,
      sequence: context.state.sequence,
      sequenceGap: context.state.sequenceGap,
      ...(context.state.safeToolSummary === undefined ? {} : { safeToolSummary: context.state.safeToolSummary }),
    }),
}

export function registerCodexConversationNode(ctx: Context): void {
  ctx.conversationEvents.register(codexUserConversationDefinition)
  ctx.conversationEvents.register(codexConversationDefinition)
}
