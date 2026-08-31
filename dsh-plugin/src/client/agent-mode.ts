/** Session-scoped voice state and compatibility helpers for agent-mode selection. */

import { defineStore, type EngineStoreHandle } from '@deepseek-ai/dsh-client-runtime/client'
import type { BakedActions } from '@deepseek-ai/dsh-client-ui-slots'
import type { ComposerChainProps } from '@deepseek-ai/dsh-client-ui-conversation/client'
export { shouldInterruptCodex } from './voice/codex-gate.ts'

/** The two explicitly selectable agent paths. */
export type AgentMode = 'dsh' | 'codex'

/** State shared by every voice control mounted for one session. */
export interface VoiceState {
  mode: AgentMode
  draft: string
  character: 'default' | 'xiaoman'
  companion: boolean
  voice: boolean
  interruptEpoch: number
  ttsEpoch: number
  /** Monotonic per-session start intent; set before a typed Codex start. */
  codexStartIntent: number
  /** The last intent already observed by the durable reply projection. */
  codexStartIntentConsumed: number
  /** Settled durable high-water captured when the current intent was armed. */
  codexStartHighWater: number
  /** Bound execution identity, when the Remote start has been accepted. */
  codexStartExecutionId: string | null
  /** Latest settled durable high-water observed during hydration. */
  codexHistoryHighWater: number
  /** Start/voice controls stay disabled until the first open snapshot is hydrated. */
  codexHistoryHydrated: boolean
}

/** Complete action declaration for {@link VoiceState}. */
/* Action parameter tuples differ per member; the runtime contract uses any[]
 * at this erased declaration position for the same reason. */
// oxlint-disable-next-line typescript/no-explicit-any -- action tuples are erased by the store declaration contract.
export interface VoiceActionDecl extends Record<string, (draft: VoiceState, ...params: any[]) => void> {
  setMode: (draft: VoiceState, mode: AgentMode) => void
  setDraft: (draft: VoiceState, text: string) => void
  setCharacter: (draft: VoiceState, character: VoiceState['character']) => void
  setCompanion: (draft: VoiceState, visible: boolean) => void
  setVoice: (draft: VoiceState, enabled: boolean) => void
  bumpInterruptEpoch: (draft: VoiceState) => void
  bumpTtsEpoch: (draft: VoiceState) => void
  markCodexStartIntent: (draft: VoiceState) => void
  bindCodexStartIntent: (draft: VoiceState, executionId: string) => void
  acknowledgeCodexStartIntent: (draft: VoiceState, intent: number) => void
  cancelCodexStartIntent: (draft: VoiceState) => void
  setCodexHistoryHighWater: (draft: VoiceState, anchor: number) => void
  markCodexHistoryHydrated: (draft: VoiceState, anchor: number) => void
}

/** Store handle used by all session-scoped voice registrations in one apply. */
export type VoiceStoreHandle = EngineStoreHandle<VoiceState, VoiceActionDecl>

/** The action callbacks exposed to components and inject factories. */
export type VoiceActions = BakedActions<VoiceState, VoiceActionDecl>

/**
 * Declare the session store. The factory is intentionally called from
 * `apply`, so plugin reloads and tests receive independent handles.
 * @returns a fresh store handle.
 */
export function createVoiceStore(): VoiceStoreHandle {
  return defineStore({
    init: (): VoiceState => ({
      mode: 'dsh',
      draft: '',
      character: 'xiaoman',
      companion: true,
      voice: true,
      interruptEpoch: 0,
      ttsEpoch: 0,
      codexStartIntent: 0,
      codexStartIntentConsumed: 0,
      codexStartHighWater: 0,
      codexStartExecutionId: null,
      codexHistoryHighWater: 0,
      codexHistoryHydrated: false,
    }),
    // Mode is deliberately not persisted: a restored Codex bit could elect a
    // composer before its owner-only snapshot is initialized. Reload is safe
    // DSH by construction.
    actions: {
      setMode: (draft, mode) => { draft.mode = mode === 'codex' ? 'codex' : 'dsh' },
      setDraft: (draft, text) => { draft.draft = text },
      setCharacter: (draft, character) => { draft.character = character === 'xiaoman' ? 'xiaoman' : 'default' },
      setCompanion: (draft, visible) => { draft.companion = visible },
      setVoice: (draft, enabled) => { draft.voice = enabled },
      bumpInterruptEpoch: (draft) => { draft.interruptEpoch += 1 },
      bumpTtsEpoch: (draft) => { draft.ttsEpoch += 1 },
      markCodexStartIntent: (draft) => {
        if (!draft.codexHistoryHydrated) return
        draft.codexStartIntent += 1
        draft.codexStartHighWater = draft.codexHistoryHighWater
        draft.codexStartExecutionId = null
      },
      bindCodexStartIntent: (draft, executionId) => {
        if (draft.codexStartIntent > draft.codexStartIntentConsumed) draft.codexStartExecutionId = executionId
      },
      acknowledgeCodexStartIntent: (draft, intent) => {
        if (intent !== draft.codexStartIntent || intent <= draft.codexStartIntentConsumed) return
        draft.codexStartIntentConsumed = intent
        draft.codexStartExecutionId = null
      },
      cancelCodexStartIntent: (draft) => {
        draft.codexStartIntentConsumed = draft.codexStartIntent
        draft.codexStartExecutionId = null
      },
      setCodexHistoryHighWater: (draft, anchor) => {
        if (Number.isFinite(anchor) && anchor > draft.codexHistoryHighWater) draft.codexHistoryHighWater = anchor
      },
      markCodexHistoryHydrated: (draft, anchor) => {
        draft.codexHistoryHydrated = true
        if (Number.isFinite(anchor) && anchor > draft.codexHistoryHighWater) draft.codexHistoryHighWater = anchor
      },
    },
  })
}

/** Immutable bounded per-session mode snapshot captured by a chain selector. */
export interface ComposerModeSnapshot {
  readonly modes: Readonly<Record<string, AgentMode>>
  readonly order: readonly string[]
}

const DEFAULT_SNAPSHOT_LIMIT = 32

/** Create an empty selector snapshot. */
export function createComposerModeSnapshot(): ComposerModeSnapshot {
  return Object.freeze({ modes: Object.freeze({}), order: Object.freeze([]) })
}

/**
 * Clone a selector snapshot, update one session, and prune the oldest ids.
 * The returned object is safe to capture in a pure slot selector.
 * @param previous - previous immutable snapshot.
 * @param sessionId - session identity to update.
 * @param mode - next explicit mode.
 * @param limit - maximum retained session ids.
 * @returns a new immutable snapshot.
 */
export function updateComposerModeSnapshot(
  previous: ComposerModeSnapshot,
  sessionId: string,
  mode: AgentMode,
  limit = DEFAULT_SNAPSHOT_LIMIT,
  protectedSessionIds: readonly string[] = [],
): ComposerModeSnapshot {
  const id = sessionId.trim()
  if (id === '') return previous
  if (previous.modes[id] === mode) return previous
  const safeLimit = Number.isFinite(limit) ? Math.max(1, Math.floor(limit)) : DEFAULT_SNAPSHOT_LIMIT
  const modes: Record<string, AgentMode> = { ...previous.modes, [id]: mode }
  const order = [...previous.order.filter(item => item !== id), id]
  const protectedIds = new Set(protectedSessionIds.map(item => item.trim()).filter(item => item !== ''))
  while (order.length > safeLimit) {
    const removableIndex = order.findIndex(item => !protectedIds.has(item))
    if (removableIndex < 0) {
      // Never evict a live Codex owner into a silent native fallback.  The
      // caller can fail closed and keep the current mode when the bounded
      // cache is saturated by protected sessions.
      return previous
    }
    const removed = order.splice(removableIndex, 1)[0]
    if (removed !== undefined) delete modes[removed]
  }
  return Object.freeze({ modes: Object.freeze(modes), order: Object.freeze(order) })
}

/** Remove one session from a selector snapshot and return a fresh value. */
export function pruneComposerModeSnapshot(
  previous: ComposerModeSnapshot,
  sessionId: string,
): ComposerModeSnapshot {
  if (!Object.prototype.hasOwnProperty.call(previous.modes, sessionId)) return previous
  const modes = { ...previous.modes }
  delete modes[sessionId]
  return Object.freeze({
    modes: Object.freeze(modes),
    order: Object.freeze(previous.order.filter(item => item !== sessionId)),
  })
}

/** The selector result injected into the elected Codex composer. */
export interface CodexComposerMatch {
  readonly mode: 'codex'
}

const CODEX_COMPOSER_MATCH: CodexComposerMatch = Object.freeze({ mode: 'codex' })

/**
 * Pure owner-only selector. It reads only the owner and an immutable snapshot
 * captured by the registration; it never reads a controller or a module bus.
 * @param owner - conversation chain currency.
 * @param snapshot - immutable per-session mode snapshot.
 * @returns the match when this session is in Codex mode.
 */
export function selectCodexComposerForSnapshot(
  owner: ComposerChainProps,
  snapshot: ComposerModeSnapshot,
): CodexComposerMatch | null {
  const sessionId = owner.session?.sessionId
  return sessionId !== undefined && snapshot.modes[String(sessionId)] === 'codex'
    ? CODEX_COMPOSER_MATCH
    : null
}
