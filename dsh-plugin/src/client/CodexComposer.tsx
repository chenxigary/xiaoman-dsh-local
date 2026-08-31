/** Legacy standalone Codex composer, retained for compatibility but no longer registered. */
import { memo, useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import type { FormEvent, KeyboardEvent } from 'react'
import type { PropsLocale, PropsRuntime, PropsStore } from '@deepseek-ai/dsh-client-ui-slots'
import type { ComposerChainProps } from '@deepseek-ai/dsh-client-ui-conversation/client'
import type { VoiceControlProps, VoiceInjected } from './contract.ts'
import { createCodexStartGate, type CodexStartGate } from './codex-start-gate.ts'
import type { VoiceStoreHandle } from './agent-mode.ts'
import { AgentModeToggle } from './AgentModeToggle.tsx'
import { AgentStatus } from './AgentStatus.tsx'
import { MicButton } from './MicButton.tsx'
import { VoiceToggle } from './VoiceToggle.tsx'
import { CompanionToggle } from './CompanionToggle.tsx'
import { MAX_CODEX_TEXT_CHARS } from './codex-remote-client.ts'
import { codexExecutionStillOwned, interruptExactAndAwaitCodexTerminal } from './voice/codex-release.ts'
import { CodexOwnerQuarantine, type CodexOwner } from './voice/codex-owner.ts'
import css from './CodexComposer.module.css'

export interface CodexComposerMatch { readonly mode: 'codex' }
const CODEX_MATCH: CodexComposerMatch = Object.freeze({ mode: 'codex' })

export type CodexComposerProps = PropsRuntime<'conversation.composer'>
  & PropsStore<VoiceStoreHandle>
  & { matched: CodexComposerMatch }
  & PropsLocale<'voice'>
  & VoiceInjected

/** Pure selector helper retained for focused unit tests and local callers. */
export function selectCodexComposerForMode(owner: ComposerChainProps, mode: 'dsh' | 'codex'): CodexComposerMatch | null {
  const sessionId = owner.session?.sessionId
  return sessionId !== undefined && mode === 'codex' ? CODEX_MATCH : null
}

export const CodexComposer = memo(function CodexComposer(props: CodexComposerProps) {
  const {
    sessionId,
    useStore,
    actions,
    codexStatus,
    codexStart,
    codexInterrupt,
  } = props
  const id = String(sessionId)
  const draft = useStore(state => state.draft)
  const character = useStore(state => state.character)
  const codexHistoryHydrated = useStore(state => state.codexHistoryHydrated)
  const [activeOwner, setActiveOwner] = useState<CodexOwner | undefined>()
  const [starting, setStarting] = useState(false)
  const [error, setError] = useState(false)
  const [codexUnavailable, setCodexUnavailable] = useState(false)
  const startAbortRef = useRef<AbortController | null>(null)
  const startGateRef = useRef<CodexStartGate | null>(null)
  if (startGateRef.current === null) startGateRef.current = createCodexStartGate()
  const generationRef = useRef(0)
  const mountedRef = useRef(true)
  const sessionRef = useRef(sessionId)
  const activeOwnerRef = useRef<CodexOwner | undefined>()
  const previousSessionRef = useRef(id)
  const quarantineRef = useRef(new CodexOwnerQuarantine())
  const quarantineRetryTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  sessionRef.current = sessionId

  const setOwner = useCallback((owner: CodexOwner | undefined): void => {
    activeOwnerRef.current = owner
    setActiveOwner(owner)
  }, [])

  useEffect(() => {
    mountedRef.current = true
    return () => { mountedRef.current = false }
  }, [])

  useEffect(() => {
    generationRef.current += 1
    startAbortRef.current?.abort()
    startAbortRef.current = null
    const controller = new AbortController()
    let cancelled = false
    setOwner(undefined)
    setError(false)
    setCodexUnavailable(false)
    void codexStatus(id, controller.signal).then(status => {
      if (cancelled || sessionRef.current !== sessionId || String(sessionRef.current) !== id) return
      setCodexUnavailable(status.state === 'unavailable' || status.capability === 'unavailable')
      const active = status.executions.find(codexExecutionStillOwned)
      if (active !== undefined) setOwner({ sessionId: id, executionId: active.executionId })
    }).catch(() => {
      if (!cancelled && !controller.signal.aborted) setError(true)
    })
    return () => {
      cancelled = true
      controller.abort()
      generationRef.current += 1
      startAbortRef.current?.abort()
      startAbortRef.current = null
      actions.cancelCodexStartIntent()
    }
  }, [actions, codexStatus, id, setOwner])

  useEffect(() => {
    if (activeOwner === undefined) return
    const controller = new AbortController()
    let cancelled = false
    const poll = async (): Promise<void> => {
      try {
        const status = await codexStatus(id, controller.signal)
        if (cancelled || sessionRef.current !== sessionId || String(sessionRef.current) !== id) return
        setCodexUnavailable(status.state === 'unavailable' || status.capability === 'unavailable')
        if (!status.executions.some(item => item.executionId === activeOwner.executionId && codexExecutionStillOwned(item))) {
          actions.cancelCodexStartIntent()
          setOwner(undefined)
        }
      } catch {
        if (!cancelled && !controller.signal.aborted) setError(true)
      }
    }
    void poll()
    const timer = globalThis.setInterval(() => { void poll() }, 500)
    return () => {
      cancelled = true
      controller.abort()
      globalThis.clearInterval(timer)
    }
  }, [actions, activeOwner, codexStatus, id, setOwner])

  const releaseExact = useCallback((ownerSessionId: string, executionId: string, signal?: AbortSignal) => {
    const options = signal === undefined
      ? { pollMs: 50, maxPolls: 40 }
      : { signal, pollMs: 50, maxPolls: 40 }
    return interruptExactAndAwaitCodexTerminal(
      { status: codexStatus, interrupt: codexInterrupt },
      ownerSessionId,
      executionId,
      options,
    )
  }, [codexInterrupt, codexStatus])

  const releaseOwner = useCallback((owner: CodexOwner): Promise<boolean> => {
    quarantineRef.current.quarantine(owner)
    return quarantineRef.current.release(owner, exact => releaseExact(exact.sessionId, exact.executionId))
  }, [releaseExact])

  const retryQuarantined = useCallback(async (): Promise<void> => {
    await quarantineRef.current.retryAll(owner => releaseExact(owner.sessionId, owner.executionId))
  }, [releaseExact])

  const scheduleQuarantineRetry = useCallback((): void => {
    if (quarantineRetryTimerRef.current !== null) return
    quarantineRetryTimerRef.current = globalThis.setTimeout(() => {
      quarantineRetryTimerRef.current = null
      void retryQuarantined()
    }, 250)
  }, [retryQuarantined])

  // The exact owner survives a renderer/session edge. A failed release is
  // retained in the quarantine and is never overwritten by session B.
  useLayoutEffect(() => {
    if (previousSessionRef.current === id) return
    previousSessionRef.current = id
    generationRef.current += 1
    startAbortRef.current?.abort()
    startAbortRef.current = null
    actions.cancelCodexStartIntent()
    const oldOwner = activeOwnerRef.current
    if (oldOwner !== undefined && oldOwner.sessionId !== id) {
      void releaseOwner(oldOwner).then(ok => {
        if (!ok) {
          scheduleQuarantineRetry()
          if (mountedRef.current && sessionRef.current === sessionId) setError(true)
        }
      })
      setOwner(undefined)
    }
    void retryQuarantined()
  }, [actions, id, releaseOwner, retryQuarantined, scheduleQuarantineRetry, sessionId, setOwner])

  useEffect(() => () => {
    mountedRef.current = false
    generationRef.current += 1
    startAbortRef.current?.abort()
    startAbortRef.current = null
    actions.cancelCodexStartIntent()
    const owner = activeOwnerRef.current
    if (owner !== undefined) {
      void releaseOwner(owner).then(ok => { if (!ok) scheduleQuarantineRetry() })
    }
    void retryQuarantined()
  }, [actions, releaseOwner, retryQuarantined, scheduleQuarantineRetry])

  const submit = useCallback(async (event?: FormEvent) => {
    event?.preventDefault()
    const text = draft.trim()
    if (text === '' || activeOwner !== undefined || starting || codexUnavailable || !codexHistoryHydrated) return
    const startGate = startGateRef.current
    if (startGate === null || !startGate.tryClaim()) return
    setStarting(true)
    const generation = generationRef.current
    const controller = new AbortController()
    startAbortRef.current = controller
    // Arm the per-session live-reply fence before the Remote call.  The Host
    // can publish a durable, already-settled first answer before this
    // component receives the start result, so registering afterwards would
    // incorrectly classify it as hydrated history.
    actions.markCodexStartIntent()
    setError(false)
    try {
      const result = await codexStart(id, { text, character }, controller.signal)
      actions.bindCodexStartIntent(result.executionId)
      if (generation !== generationRef.current || controller.signal.aborted) {
        try {
          const released = await releaseOwner({ sessionId: id, executionId: result.executionId })
          if (!released) throw new Error('Codex 执行未确认结束')
        } catch {
          const owner: CodexOwner = { sessionId: id, executionId: result.executionId }
          quarantineRef.current.quarantine(owner)
          scheduleQuarantineRetry()
          if (mountedRef.current && sessionRef.current === sessionId) {
            setOwner(owner)
            setError(true)
          }
        }
        actions.cancelCodexStartIntent()
        return
      }
      actions.setDraft('')
      setOwner({ sessionId: id, executionId: result.executionId })
    } catch (caught) {
      actions.cancelCodexStartIntent()
      if (!(caught instanceof Error && caught.name === 'AbortError')) setError(true)
    } finally {
      if (startAbortRef.current === controller) startAbortRef.current = null
      startGate.release()
      if (mountedRef.current) setStarting(false)
    }
  }, [actions, activeOwner, character, codexHistoryHydrated, codexStart, codexUnavailable, draft, id, releaseOwner, scheduleQuarantineRetry, sessionId, setOwner, starting])

  const onKeyDown = useCallback((event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault()
      void submit()
    }
  }, [submit])

  const cancel = useCallback(async () => {
    if (activeOwner === undefined) return
    actions.cancelCodexStartIntent()
    generationRef.current += 1
    startAbortRef.current?.abort()
    try {
      const released = await releaseOwner(activeOwner)
      if (!released) throw new Error('Codex 执行未确认结束')
      if (mountedRef.current) setOwner(undefined)
    } catch {
      scheduleQuarantineRetry()
      if (mountedRef.current) setError(true)
    }
  }, [activeOwner, releaseOwner, scheduleQuarantineRetry, setOwner])

  // The overlay fallback owns ReplySpeakerMount and CompanionWindow. Mounting
  // them again here would duplicate TTS and animation subscriptions.
  const controlProps: VoiceControlProps = {
    sessionId: props.sessionId,
    useSession: props.useSession,
    useStore: props.useStore,
    actions: props.actions,
    t: props.t,
    codexStatus: props.codexStatus,
    codexModels: props.codexModels,
    codexStart: props.codexStart,
    codexInterrupt: props.codexInterrupt,
    codexApprovalDecision: props.codexApprovalDecision,
    codexLoginStart: props.codexLoginStart,
    ...(props.codexLoginPending === undefined ? {} : { codexLoginPending: props.codexLoginPending }),
    codexLoginStatus: props.codexLoginStatus,
    codexLoginCancel: props.codexLoginCancel,
    speaker: props.speaker,
    avatarAudio: props.avatarAudio,
    companionState: props.companionState,
    abortTts: props.abortTts,
    interruptReply: props.interruptReply,
    registerTtsAbort: props.registerTtsAbort,
    registerInterruptHandler: props.registerInterruptHandler,
    registerTurnCancel: props.registerTurnCancel,
    registerSessionMount: props.registerSessionMount,
    registerQqSession: props.registerQqSession,
    sendQqReply: props.sendQqReply,
    setQqEnabled: props.setQqEnabled,
    switchMode: props.switchMode,
    sendText: props.sendText,
    setCharacter: props.setCharacter,
    syncComposerRoute: props.syncComposerRoute,
  }
  return (
    <form className={css.root} onSubmit={event => { void submit(event) }}>
      <div className={css.toolbar}>
        <AgentModeToggle {...controlProps} />
        <MicButton {...controlProps} />
        <VoiceToggle {...controlProps} />
        <CompanionToggle {...controlProps} />
        <AgentStatus {...controlProps} />
      </div>
      <div className={css.heading}>Codex 语音</div>
      <textarea
        className={css.input}
        value={draft}
        onChange={event => { actions.setDraft(event.target.value) }}
        onKeyDown={onKeyDown}
        placeholder="输入 Codex 请求…"
        maxLength={MAX_CODEX_TEXT_CHARS}
        disabled={activeOwner !== undefined || starting || codexUnavailable || !codexHistoryHydrated}
        rows={2}
        aria-label="Codex 请求"
      />
      <div className={css.actions}>
        {activeOwner === undefined
          ? <button type="submit" disabled={starting || draft.trim() === '' || codexUnavailable || !codexHistoryHydrated}>发送给 Codex</button>
          : <button type="button" onClick={() => void cancel()}>停止</button>}
        {codexUnavailable && <span role="status">Codex 执行暂不可用</span>}
        {error && !codexUnavailable && <span role="status">Codex 当前不可用</span>}
      </div>
    </form>
  )
})
