/** Narrow browser-side props shared by the voice controls. */

import type {
  CodexApprovalDecision,
  CodexApprovalResult,
  CodexCharacter,
  CodexLoginCancelResult,
  CodexLoginStartResult,
  CodexLoginStatusResult,
  CodexModelCatalogResult,
  CodexModelSelection,
  CodexStartResult,
} from '../types.ts'
import type { PropsLocale, PropsRuntime, PropsStore } from '@deepseek-ai/dsh-client-ui-slots'
import type { ModelSelectInjected } from '@deepseek-ai/dsh-client-ui-model-selection/client'
import type { CodexAuthStatus } from './codex-remote-client.ts'
import type { CompanionRendererPort } from './voice/companion-controller.ts'
import type { ReplySpeakerPort } from './voice/speaker.ts'
import type { AvatarAudioRoutePort } from './voice/avatar-audio-route.ts'
import type { AgentMode, VoiceStoreHandle } from './agent-mode.ts'
import type { CharacterId } from './character.ts'

/** The store-backed share used by every session-scoped voice component. */
export type VoiceStoreProps = PropsStore<VoiceStoreHandle>

/** Host-backed callbacks; no controller or whole Remote crosses into JSX. */
export interface VoiceCodexCallbacks {
  codexStatus: (sessionId: string | undefined, signal?: AbortSignal) => Promise<CodexAuthStatus>
  codexModels: (sessionId: string, signal?: AbortSignal) => Promise<CodexModelCatalogResult>
  codexStart: (
    sessionId: string,
    request: { readonly text: string; readonly character: CodexCharacter } & Partial<CodexModelSelection>,
    signal?: AbortSignal,
  ) => Promise<CodexStartResult>
  codexInterrupt: (sessionId: string, executionId: string, signal?: AbortSignal) => Promise<void>
  codexApprovalDecision: (
    sessionId: string,
    executionId: string,
    approvalId: string,
    decision: CodexApprovalDecision,
    signal?: AbortSignal,
  ) => Promise<CodexApprovalResult>
  codexLoginStart: (sessionId: string, signal?: AbortSignal) => Promise<CodexLoginStartResult>
  codexLoginPending?: (sessionId: string, signal?: AbortSignal) => Promise<CodexLoginStatusResult | null>
  codexLoginStatus: (sessionId: string, loginId: string, signal?: AbortSignal) => Promise<CodexLoginStatusResult>
  codexLoginCancel: (sessionId: string, loginId: string, signal?: AbortSignal) => Promise<CodexLoginCancelResult>
}

/** Renderer hook callbacks for speaker and companion lifecycle projection. */
export interface VoiceRendererHooks {
  readonly speaker: ReplySpeakerPort
  readonly avatarAudio: AvatarAudioRoutePort
  readonly companionState: CompanionRendererPort
  abortTts: () => void
  /** Stops local playback and awaits the session's authoritative Codex release. */
  interruptReply: () => Promise<void>
  registerTtsAbort: (controller: AbortController | null) => void
  registerInterruptHandler: (handler: (() => void) | null) => void
  /** Synchronous owner-local cancellation before a mode state commit. */
  registerTurnCancel: (handler: (() => void) | null) => void
  /** One hidden session renderer retains/releases its apply-level owner. */
  registerSessionMount: (mounted: boolean) => void
  /** Apply-level QQ socket owner; components receive only narrow callbacks. */
  registerQqSession: (sessionId: string | undefined, onText: (text: string) => void) => () => void
  sendQqReply: (sessionId: string, text: string) => boolean
  /** QQ transport is opt-in; toggling it starts/stops the single owner socket. */
  setQqEnabled: (enabled: boolean) => void
}

/**
 * Injected face for components. Values that belong to the session store are
 * intentionally absent; components read them through `props.useStore` and
 * write them through `props.actions`.
 */
export interface VoiceInjected extends VoiceCodexCallbacks, VoiceRendererHooks {
  switchMode: (mode: AgentMode) => Promise<void>
  sendText: (text: string) => Promise<void>
  setCharacter: (character: CharacterId) => void
  /** Keep the native-composer router aligned with this session store. */
  syncComposerRoute: (state: {
    readonly character: CharacterId
    readonly codexHistoryHydrated: boolean
  }) => void
}

/**
 * Common control face shared by the resident composer controls.
 * It deliberately contains only the session standard kit; slot-specific
 * owner props never cross from one seat into another.
 */
export type VoiceControlProps =
  Pick<PropsRuntime<'conversation.composer'>, 'sessionId' | 'useSession'>
  & PropsStore<VoiceStoreHandle>
  & PropsLocale<'voice'>
  & VoiceInjected

/** Full input-left face used only at the input-left registration boundary. */
export type VoiceInputControlProps =
  PropsRuntime<'conversation.input.left'>
  & PropsStore<VoiceStoreHandle>
  & PropsLocale<'voice'>
  & VoiceInjected

/** Business face injected into the mode-aware composer model seat. */
export interface AgentModelSelectInjected {
  readonly dshModel: ModelSelectInjected
  readonly codexModels: (signal?: AbortSignal) => Promise<CodexModelCatalogResult>
  readonly getCodexSelection: () => CodexModelSelection
  readonly setCodexSelection: (selection: CodexModelSelection) => void
}

/** Original composer model seat, with DSH and Codex directories behind one face. */
export type AgentModelSelectProps = PropsRuntime<'conversation.input.model'>
  & PropsStore<VoiceStoreHandle>
  & PropsLocale<'voice'>
  & AgentModelSelectInjected
