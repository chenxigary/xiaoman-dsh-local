/** Browser voice plugin: session-scoped controls and native-composer routing. */
import type { ClientContext, SessionId } from '@deepseek-ai/dsh-client-runtime/client'
import type {} from '@deepseek-ai/dsh-client-locale/client'
import type {} from '@deepseek-ai/dsh-client-ui-conversation/client'
import type { ModelSelectInjected } from '@deepseek-ai/dsh-client-ui-model-selection/client'
import type {} from '@deepseek-ai/dsh-api-remotes/client'
import type { SlotComponent } from '@deepseek-ai/dsh-client-ui-slots'
import { MicButton } from './MicButton.tsx'
import { BusyToggle } from './BusyToggle.tsx'
import { BridgeStatus } from './BridgeStatus.tsx'
import { ReplySpeakerMount } from './voice/reply-listener.tsx'
import { ReplySpeaker } from './voice/speaker.ts'
import { VoiceToggle } from './VoiceToggle.tsx'
import { CompanionToggle } from './CompanionToggle.tsx'
import { CompanionWindow } from './voice/companion.tsx'
import { CompanionRenderer } from './voice/companion-controller.ts'
import { bridgeBase } from './bridge.ts'
import type { AgentModelSelectInjected, VoiceInjected, VoiceInputControlProps } from './contract.ts'
import { en, zh, type VoiceKey } from './locales.ts'
import {
  createComposerModeSnapshot,
  createVoiceStore,
  updateComposerModeSnapshot,
  type AgentMode,
  type VoiceActions,
} from './agent-mode.ts'
import { shouldInterruptCodex } from './voice/codex-gate.ts'
import { createCodexClient, MAX_CODEX_TEXT_CHARS } from './codex-remote-client.ts'
import { registerCodexConversationNode } from './codex-conversation.ts'
import { CodexAnswerNodeView } from './CodexAnswerNodeView.tsx'
import { CodexUserNodeView } from './CodexUserNodeView.tsx'
import { AgentModeToggle } from './AgentModeToggle.tsx'
import { AgentStatus } from './AgentStatus.tsx'
import { SessionOwnerMap } from './voice/session-owner.ts'
import { interruptAndAwaitCodexTerminal } from './voice/codex-release.ts'
import { SessionOperationOwner } from './voice/operation-owner.ts'
import { AvatarAudioRoute } from './voice/avatar-audio-route.ts'
import { installComposerSubmitRoute, type RoutableComposerInput } from './voice/native-composer-route.ts'
import { AgentModelSelect } from './AgentModelSelect.tsx'
import type { CodexModelSelection } from '../types.ts'

declare module '@deepseek-ai/dsh-client-ui-slots' {
  interface LocaleNamespaceMap {
    /** The voice control's copy. */
    voice: VoiceKey
  }
}

const NS = 'voice'

/** Services used by the client plugin. */
export const inject = ['slots', 'locale', 'sessions', 'remote', 'remote.codex', 'conversation', 'conversationEvents', 'modelDirectories']

/**
 * Mount the voice controls. One handle is shared by all session entries; the
 * renderer creates one store instance per session scope.
 * @param ctx - client root context.
 * @returns plugin disposer.
 */
export async function apply(ctx: ClientContext): Promise<() => Promise<void>> {
  const voiceStore = createVoiceStore()
  let composerSnapshot = createComposerModeSnapshot()
  const activeCodexSessions = new Map<string, { active: boolean; mounts: number }>()
  const composerRouteState = new Map<string, {
    character: 'default' | 'xiaoman'
    codexHistoryHydrated: boolean
    actions: VoiceActions
  }>()
  const composerRouteDisposers = new Map<string, () => void>()
  const composerStarts = new Set<string>()
  const codexSelections = new Map<string, CodexModelSelection>()
  const selectionFor = (sessionId: string): CodexModelSelection => codexSelections.get(sessionId) ?? {
    model: 'gpt-5.4-mini',
    reasoningEffort: 'low',
    serviceTier: null,
  }
  interface SessionVoiceRuntime {
    readonly speaker: ReplySpeaker
    readonly avatarAudio: AvatarAudioRoute
    readonly companionState: CompanionRenderer
    readonly operations: SessionOperationOwner
  }
  const disposeOwner = (owner: SessionVoiceRuntime): void => {
    owner.operations.dispose()
    owner.speaker.dispose()
    owner.avatarAudio.dispose()
    owner.companionState.dispose()
  }
  const owners = new SessionOwnerMap<SessionVoiceRuntime>(
    () => ({
      speaker: new ReplySpeaker(),
      avatarAudio: new AvatarAudioRoute(),
      companionState: new CompanionRenderer(),
      operations: new SessionOperationOwner(),
    }),
    disposeOwner,
  )
  const codex = createCodexClient(ctx.remote)

  const ensureComposerRoute = (sessionId: SessionId | undefined, actions: VoiceActions): boolean => {
    if (sessionId === undefined) return false
    const id = String(sessionId)
    const previous = composerRouteState.get(id)
    composerRouteState.set(id, {
      character: previous?.character ?? 'xiaoman',
      codexHistoryHydrated: previous?.codexHistoryHydrated ?? false,
      actions,
    })
    if (composerRouteDisposers.has(id)) return true
    const scope = ctx.sessions.scope(sessionId)
    if (scope === undefined) return false
    const input = ctx.conversation.input.for(scope) as RoutableComposerInput
    const dispose = installComposerSubmitRoute(input, (target) => {
      if (composerSnapshot.modes[id] !== 'codex') return false
      const state = composerRouteState.get(id)
      const snapshot = target.state.getSnapshot()
      const text = snapshot.draft.trim()
      if (text === '') return true
      if (snapshot.imageIds.length > 0) {
        target.notify('error', 'Codex 模式暂不支持图片；切回 DSH 可发送图片')
        return true
      }
      if (state === undefined || !state.codexHistoryHydrated) {
        target.notify('info', 'Codex 会话历史仍在初始化，请稍后重试')
        return true
      }
      if (composerStarts.has(id)) return true
      composerStarts.add(id)
      state.actions.markCodexStartIntent()
      if (typeof target.commitSend === 'function') target.commitSend([])
      else target.setDraft('')
      void codex.start(id, { text, character: state.character, ...selectionFor(id) }).then(
        (started) => {
          state.actions.bindCodexStartIntent(started.executionId)
        },
        () => {
          state.actions.cancelCodexStartIntent()
          if (target.state.getSnapshot().draft === '') target.setDraft(text)
          target.notify('error', 'Codex 请求未发送，请重试')
        },
      ).finally(() => { composerStarts.delete(id) })
      return true
    })
    composerRouteDisposers.set(id, dispose)
    return true
  }

  registerCodexConversationNode(ctx)
  ctx.slots.inject('conversation.chat.node', () => ctx.slots.register(
    { name: 'conversation.chat.node', key: 'codex-answer', locale: 'conversation' },
    CodexAnswerNodeView,
  ))
  ctx.slots.inject('conversation.chat.node', () => ctx.slots.register(
    { name: 'conversation.chat.node', key: 'codex-user', locale: 'conversation' },
    CodexUserNodeView,
  ))
  ctx.effect(() => ctx.locale.register(NS, { zh, en }), 'ui-voice: dictionaries')
  ctx.effect(() => () => {
    for (const dispose of composerRouteDisposers.values()) dispose()
    composerRouteDisposers.clear()
    composerRouteState.clear()
    composerStarts.clear()
    codexSelections.clear()
    owners.clear(disposeOwner)
    activeCodexSessions.clear()
  }, 'ui-voice: session renderer teardown')

  const interruptCodexSession = async (sessionId: string): Promise<void> => {
    await interruptAndAwaitCodexTerminal(codex, sessionId)
  }

  const injectFace = (sessionId: SessionId | undefined, actions: VoiceActions): VoiceInjected => {
    // Slot overlay/fallback renderers can coexist for one session. Each
    // injected face gets an identity so one unmount cannot clear another
    // renderer's cancellation or TTS registration.
    const ownerToken = Symbol('voice-renderer')
    let mountLease: ReturnType<typeof owners.acquire> | undefined
    let sessionMounted = false
    return ({
    switchMode: async (next: AgentMode) => {
      const owner = owners.get(sessionId)
      const id = sessionId === undefined ? '' : String(sessionId)
      // A restored store is safe DSH by construction. Infer the only
      // opposite mode for an uncaptured switch so the first toggle still
      // refreshes the chain and cannot become a silent no-op.
      const previous = composerSnapshot.modes[id] ?? (next === 'codex' ? 'dsh' : 'codex')
      if (previous === next) return
      const protectedOwners = [...activeCodexSessions.keys()].filter(ownerId => ownerId !== id)
      if (next === 'codex') protectedOwners.push(id)
      const nextComposerSnapshot = updateComposerModeSnapshot(
        composerSnapshot,
        id,
        next,
        undefined,
        protectedOwners,
      )
      if (next === 'codex' && nextComposerSnapshot.modes[id] !== 'codex') {
        throw new Error('Codex 会话数量已达上限，暂不能切换')
      }
      if (next === 'codex' && !ensureComposerRoute(sessionId, actions)) {
        throw new Error('Codex 输入路由暂不可用')
      }
      // Local playback and remote Codex work stop before the new mode is
      // visible to a subsequent utterance.
      owner.operations.cancelTurns()
      owner.speaker.stop()
      owner.operations.abortTts()
      owner.operations.interrupt()
      actions.bumpInterruptEpoch()
      actions.bumpTtsEpoch()
      if (sessionId !== undefined && previous === 'codex') {
        await interruptCodexSession(String(sessionId))
      }
      actions.setMode(next)
      composerSnapshot = nextComposerSnapshot
      const sessionOwners = activeCodexSessions.get(id)
      if (next === 'codex') {
        activeCodexSessions.set(id, { active: true, mounts: sessionOwners?.mounts ?? 0 })
      } else if (sessionOwners === undefined || sessionOwners.mounts === 0) {
        activeCodexSessions.delete(id)
      } else {
        activeCodexSessions.set(id, { active: false, mounts: sessionOwners.mounts })
      }
      owner.companionState.dispatch({ type: 'reset' })
    },
    sendText: async (text: string) => {
      if (text.trim() === '' || text.length > MAX_CODEX_TEXT_CHARS) throw new Error('语音请求超过长度限制')
      if (sessionId === undefined) throw new Error('当前没有可用会话')
      const binding = ctx.sessions.binding(sessionId)
      const session = binding?.session
      if (session === undefined) throw new Error('当前会话暂不可用')
      let interrupt = true
      try {
        interrupt = localStorage.getItem('s2s.voice.interrupt') !== '0'
      } catch {
        // Persistence is optional; the safe default is immediate steering.
      }
      const running = session.getSnapshot()?.running === true
      const result = await session.prompt([{ type: 'text', text }], running && interrupt ? 'steer' : 'queue')
      if (!result.ok) throw new Error('语音消息未发送')
    },
    setCharacter: (character) => {
      actions.setCharacter(character)
      const id = sessionId === undefined ? '' : String(sessionId)
      const state = composerRouteState.get(id)
      if (state !== undefined) composerRouteState.set(id, { ...state, character })
    },
    syncComposerRoute: (state) => {
      if (sessionId === undefined) return
      const id = String(sessionId)
      const previous = composerRouteState.get(id)
      composerRouteState.set(id, { ...state, actions: previous?.actions ?? actions })
    },
    codexStatus: (id, signal) => codex.status(id, signal),
    codexModels: (id, signal) => codex.models(id, signal),
    codexStart: (id, request, signal) => codex.start(id, { ...request, ...selectionFor(id) }, signal),
    codexInterrupt: (id, executionId, signal) => codex.interrupt(id, executionId, signal),
    codexApprovalDecision: (id, executionId, approvalId, decision, signal) => codex.approvalDecision(id, executionId, approvalId, decision, signal),
    codexLoginStart: (id, signal) => codex.loginStart(id, signal),
    codexLoginPending: (id, signal) => codex.loginPending(id, signal),
    codexLoginStatus: (id, loginId, signal) => codex.loginStatus(id, loginId, signal),
    codexLoginCancel: (id, loginId, signal) => codex.loginCancel(id, loginId, signal),
    speaker: owners.get(sessionId).speaker,
    avatarAudio: owners.get(sessionId).avatarAudio,
    companionState: owners.get(sessionId).companionState,
    abortTts: () => {
      const owner = owners.get(sessionId)
      owner.operations.abortTts(ownerToken)
    },
    interruptReply: async () => {
      const owner = owners.get(sessionId)
      owner.operations.cancelTurns()
      owner.speaker.stop()
      owner.operations.abortTts()
      owner.operations.interrupt()
      actions.bumpInterruptEpoch()
      actions.bumpTtsEpoch()
      owner.companionState.dispatch({ type: 'interrupted' })
      // Native DSH voice must never probe/authenticate the Codex bridge.  Only
      // an explicitly selected Codex session (or a known Codex owner retained
      // by this apply) may enter the authoritative release path.
      const id = sessionId === undefined ? '' : String(sessionId)
      if (id !== '' && shouldInterruptCodex(
        composerSnapshot.modes[id] ?? 'dsh',
        activeCodexSessions.get(id)?.active === true,
      )) {
        await interruptCodexSession(String(sessionId))
      }
    },
    registerTtsAbort: (controller) => {
      const owner = owners.get(sessionId)
      owner.operations.registerTts(ownerToken, controller)
    },
    registerInterruptHandler: (handler) => {
      const owner = owners.get(sessionId)
      owner.operations.registerInterrupt(ownerToken, handler)
    },
    registerTurnCancel: (handler) => {
      const owner = owners.get(sessionId)
      owner.operations.registerTurnCancel(ownerToken, handler)
    },
      registerSessionMount: (mounted) => {
        if (mounted === sessionMounted) return
        sessionMounted = mounted
        if (mounted) {
          if (mountLease === undefined) mountLease = owners.acquire(sessionId)
        } else {
          mountLease?.release()
          mountLease = undefined
        }
        const id = sessionId === undefined ? '' : String(sessionId)
        if (id !== '') {
          const state = activeCodexSessions.get(id)
          if (mounted) {
            activeCodexSessions.set(id, { active: state?.active === true, mounts: (state?.mounts ?? 0) + 1 })
          } else if (state !== undefined) {
            const mounts = Math.max(0, state.mounts - 1)
            if (mounts === 0 && !state.active) activeCodexSessions.delete(id)
            else activeCodexSessions.set(id, { active: state.active, mounts })
          }
        }
      },
      // QQ is intentionally outside this deployment. Keep the narrow legacy
      // face inert so old component types cannot start a socket accidentally.
      registerQqSession: () => () => {},
      sendQqReply: () => false,
      setQqEnabled: () => {},
    })
  }

  const registerControl = (id: string, order: number, component: SlotComponent<VoiceInputControlProps>): void => {
    ctx.slots.inject('conversation.input.left', () => ctx.slots.register(
      { name: 'conversation.input.left', id, order, locale: NS, store: voiceStore, inject: injectFace },
      component,
    ))
  }

  registerControl('voice-mic', 80, MicButton)
  registerControl('voice-agent-mode', 82, AgentModeToggle)
  registerControl('voice-agent-status', 81, AgentStatus)
  registerControl('voice-bridge-status', 83, BridgeStatus)
  registerControl('voice-toggle', 85, VoiceToggle)
  registerControl('voice-companion-toggle', 86, CompanionToggle)
  registerControl('voice-busy-toggle', 87, BusyToggle)
  registerControl('voice-reply', 90, ReplySpeakerMount)
  registerControl('voice-companion', 95, CompanionWindow)

  // Shadow the ordinary single model occupant with one mode-aware occupant.
  // DSH delegates to its resident ModelDirectory; Codex uses the Host-owned
  // App Server catalog while the seat and surrounding composer stay fixed.
  ctx.slots.inject('conversation.input.model', () => ctx.slots.register({
    name: 'conversation.input.model',
    priority: -10,
    locale: NS,
    store: voiceStore,
    inject: (sessionId): AgentModelSelectInjected => {
      const directory = ctx.modelDirectories.directoryFor(sessionId)
      const available = ctx.sessions.subagentAddress(sessionId) === undefined
      const dshModel: ModelSelectInjected = {
        available,
        directory: directory.store,
        load: () => { if (available) void directory.load().catch(() => undefined) },
        select: selection => available
          ? directory.select(selection).then(() => true, () => false)
          : Promise.resolve(false),
      }
      const id = String(sessionId)
      return {
        dshModel,
        codexModels: signal => codex.models(id, signal),
        getCodexSelection: () => selectionFor(id),
        setCodexSelection: selection => { codexSelections.set(id, selection) },
      }
    },
  }, AgentModelSelect))

  console.log('[ui-voice] 已加载，语音桥接地址：', bridgeBase())
  return async () => {
    for (const dispose of composerRouteDisposers.values()) dispose()
    composerRouteDisposers.clear()
  }
}
