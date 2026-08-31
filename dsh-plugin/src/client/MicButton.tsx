/** Continuous microphone control with bounded capture and cancellation. */
import { memo, useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import type {} from '@deepseek-ai/dsh-client-ui-conversation/client'
import { stt, VadStream } from './bridge.ts'
import type { VoiceControlProps, VoiceInjected } from './contract.ts'
import { shouldInterruptCodex } from './voice/codex-gate.ts'
import type { CodexStartResult } from '../types.ts'
import { MicRecorder } from './voice/recorder.ts'
import { UtteranceQueue } from './voice/utterance-queue.ts'
import { UtteranceGeneration } from './voice/turn-generation.ts'
import { latencyEvent, monotonicNow } from './latency.ts'
import { codexExecutionStillOwned, interruptExactAndAwaitCodexTerminal } from './voice/codex-release.ts'
import css from './MicButton.module.css'

/** Full mic-control props: framework seats, store state/actions, and callbacks. */
export type MicButtonProps = VoiceControlProps

type Phase = 'idle' | 'listening' | 'transcribing' | 'blocked'

type ActiveCodexOwner = {
  readonly sessionId: string
  readonly executionId: string
  readonly generation: number
  readonly status: VoiceInjected['codexStatus']
  readonly interrupt: VoiceInjected['codexInterrupt']
}

function sessionKey(sessionId: string | number | undefined): string | undefined {
  return sessionId === undefined ? undefined : String(sessionId)
}

function MicIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z" />
      <path d="M19 10v2a7 7 0 0 1-14 0v-2" />
      <line x1="12" y1="19" x2="12" y2="23" />
      <line x1="8" y1="23" x2="16" y2="23" />
    </svg>
  )
}

export const MicButton = memo(function MicButton({
  t,
  useStore,
  actions,
  sendText,
  speaker,
  interruptReply,
  companionState,
  registerSessionMount,
  codexStatus,
  codexStart,
  codexInterrupt,
  sessionId,
  registerTurnCancel,
}: MicButtonProps) {
  const mode = useStore(state => state.mode)
  const character = useStore(state => state.character)
  const codexHistoryHydrated = useStore(state => state.codexHistoryHydrated)
  const interruptEpoch = useStore(state => state.interruptEpoch)
  const modeRef = useRef(mode)
  const characterRef = useRef(character)
  const codexHistoryHydratedRef = useRef(codexHistoryHydrated)
  const sessionIdRef = useRef(sessionId)
  const sendTextRef = useRef(sendText)
  const codexStatusRef = useRef(codexStatus)
  const codexStartRef = useRef(codexStart)
  const codexInterruptRef = useRef(codexInterrupt)
  const waitForCodexTerminalRef = useRef<(owner: ActiveCodexOwner, signal: AbortSignal) => Promise<void>>(() => Promise.resolve())
  const interruptReplyRef = useRef(interruptReply)
  const companionStateRef = useRef(companionState)
  modeRef.current = mode
  characterRef.current = character
  codexHistoryHydratedRef.current = codexHistoryHydrated
  sessionIdRef.current = sessionId
  sendTextRef.current = sendText
  codexStatusRef.current = codexStatus
  codexStartRef.current = codexStart
  codexInterruptRef.current = codexInterrupt
  interruptReplyRef.current = interruptReply
  companionStateRef.current = companionState
  const [phase, setPhase] = useState<Phase>('idle')
  const [blocked, setBlocked] = useState(false)
  const mountedRef = useRef(true)
  const blockedRef = useRef(false)
  const recorderRef = useRef<MicRecorder | null>(null)
  const queueRef = useRef(new UtteranceQueue())
  const drainRunningRef = useRef(false)
  const generationRef = useRef(new UtteranceGeneration())
  const sttAbortRef = useRef<AbortController | null>(null)
  const codexStartAbortRef = useRef<AbortController | null>(null)
  const codexWaitAbortRef = useRef<AbortController | null>(null)
  const activeCodexRef = useRef<ActiveCodexOwner | null>(null)
  const pendingOwnerReleaseRef = useRef<Promise<boolean> | null>(null)
  const bargeInPendingRef = useRef(false)
  const bargeInTokenRef = useRef(0)
  const previousSessionRef = useRef(sessionId)
  const cancellationModeRef = useRef(mode)
  const drainRef = useRef<(() => Promise<void>) | null>(null)
  const markBlocked = useCallback((value: boolean): void => {
    blockedRef.current = value
    if (mountedRef.current) setBlocked(value)
  }, [])

  useEffect(() => {
    registerSessionMount(true)
    return () => registerSessionMount(false)
  }, [registerSessionMount])

  const releaseExactCodex = useCallback(async (active: ActiveCodexOwner): Promise<boolean> => {
    try {
      await interruptExactAndAwaitCodexTerminal({
        status: active.status,
        interrupt: active.interrupt,
      }, active.sessionId, active.executionId)
      if (activeCodexRef.current?.sessionId === active.sessionId
        && activeCodexRef.current.executionId === active.executionId
        && activeCodexRef.current.generation === active.generation) {
        activeCodexRef.current = null
      }
      markBlocked(false)
      return true
    } catch {
      if (mountedRef.current) {
        markBlocked(true)
        setPhase('blocked')
      }
      companionStateRef.current.dispatch({ type: 'interrupted' })
      return false
    }
  }, [markBlocked])

  const releaseActiveCodex = useCallback(async (): Promise<boolean> => {
    const active = activeCodexRef.current
    if (active === null) return true
    const pending = pendingOwnerReleaseRef.current
    if (pending !== null) return pending
    const release = releaseExactCodex(active)
    pendingOwnerReleaseRef.current = release
    try {
      return await release
    } finally {
      if (pendingOwnerReleaseRef.current === release) pendingOwnerReleaseRef.current = null
    }
  }, [releaseExactCodex])

  // Native DSH listening must not probe or authenticate the Codex bridge.  A
  // mode switch is the only product path that elects the Codex owner; the
  // ref keeps this guard race-safe while a render is in flight.
  const interruptCurrentMode = useCallback(async (): Promise<void> => {
    if (!shouldInterruptCodex(modeRef.current, true)) return
    await interruptReplyRef.current()
  }, [])

  const cancelPending = useCallback((releaseCodex = false): Promise<boolean> => {
    generationRef.current.cancel()
    sttAbortRef.current?.abort()
    sttAbortRef.current = null
    codexStartAbortRef.current?.abort()
    codexStartAbortRef.current = null
    actions.cancelCodexStartIntent()
    codexWaitAbortRef.current?.abort()
    codexWaitAbortRef.current = null
    queueRef.current.clear()
    return releaseCodex ? releaseActiveCodex() : Promise.resolve(true)
  }, [actions, releaseActiveCodex])

  // A session edge fences the old owner before the new session's callbacks can
  // observe it. The exact owner triple is retained while its authoritative
  // release is in flight; the new session must await it before starting.
  useLayoutEffect(() => {
    if (previousSessionRef.current === sessionId) return
    previousSessionRef.current = sessionId
    bargeInTokenRef.current += 1
    bargeInPendingRef.current = false
    recorderRef.current?.stop()
    recorderRef.current = null
    generationRef.current.cancel()
    sttAbortRef.current?.abort()
    sttAbortRef.current = null
    codexStartAbortRef.current?.abort()
    codexStartAbortRef.current = null
    actions.cancelCodexStartIntent()
    codexWaitAbortRef.current?.abort()
    codexWaitAbortRef.current = null
    queueRef.current.clear()
    const active = activeCodexRef.current
    if (active !== null) {
      const release = releaseExactCodex(active)
      pendingOwnerReleaseRef.current = release
      void release.finally(() => {
        if (pendingOwnerReleaseRef.current === release) pendingOwnerReleaseRef.current = null
      })
    }
  }, [actions, releaseExactCodex, sessionId])

  // Mode changes, explicit interrupt epochs, unmount, and stop all share one
  // cancellation fence. A stale STT result can therefore never create a turn.
  useEffect(() => {
    if (cancellationModeRef.current !== mode) {
      cancellationModeRef.current = mode
      bargeInTokenRef.current += 1
      bargeInPendingRef.current = false
    }
    void cancelPending(true)
    if (mountedRef.current && !blockedRef.current) setPhase(recorderRef.current === null ? 'idle' : 'listening')
  }, [cancelPending, interruptEpoch, mode])

  useEffect(() => {
    registerTurnCancel(cancelPending)
    return () => registerTurnCancel(null)
  }, [cancelPending, registerTurnCancel])

  useEffect(() => () => {
    mountedRef.current = false
    recorderRef.current?.stop()
    recorderRef.current = null
    void cancelPending(true)
    actions.bumpInterruptEpoch()
    actions.bumpTtsEpoch()
    companionState.dispatch({ type: 'reset' })
  }, [actions, cancelPending, companionState])

  useEffect(() => speaker.onSpeakingChange(() => {
    const recorder = recorderRef.current
    if (recorder === null) return
    recorder.setPaused(false)
    recorder.setInterruptMode(speaker.speaking)
    companionState.dispatch({ type: speaker.speaking ? 'speech_start' : 'speech_end' })
  }), [companionState, speaker])

  const waitForCodexTerminal = useCallback(async (owner: ActiveCodexOwner, signal: AbortSignal): Promise<void> => {
    while (!signal.aborted) {
      const status = await owner.status(owner.sessionId, signal)
      if (!status.executions.some(execution => execution.executionId === owner.executionId && codexExecutionStillOwned(execution))) return
      await new Promise<void>(resolve => {
        let timer: ReturnType<typeof setTimeout> | null = null
        const onAbort = () => {
          if (timer !== null) globalThis.clearTimeout(timer)
          signal.removeEventListener('abort', onAbort)
          resolve()
        }
        timer = globalThis.setTimeout(() => {
          signal.removeEventListener('abort', onAbort)
          resolve()
        }, 500)
        signal.addEventListener('abort', onAbort, { once: true })
        if (signal.aborted) {
          if (timer !== null) globalThis.clearTimeout(timer)
          onAbort()
        }
      })
    }
  }, [])
  waitForCodexTerminalRef.current = waitForCodexTerminal

  const drain = useCallback(async () => {
    if (drainRunningRef.current) return
    drainRunningRef.current = true
    try {
      let pcm: ArrayBuffer | undefined
      while ((pcm = queueRef.current.dequeue()) !== undefined) {
        if (bargeInPendingRef.current) break
        const turnGeneration = generationRef.current.current
        const modeForTurn = modeRef.current
        const ownerSessionId = sessionKey(sessionIdRef.current)
        const characterForTurn = characterRef.current
        if (recorderRef.current === null
          || !generationRef.current.isCurrent(turnGeneration)
          || ownerSessionId === undefined) break
        setPhase('transcribing')
        companionStateRef.current.dispatch({ type: 'thinking' })
        const sttController = new AbortController()
        sttAbortRef.current = sttController
        generationRef.current.track(sttController)
        const sttStarted = monotonicNow()
        try {
          const result = await stt(pcm, {
            signal: sttController.signal,
            sessionId: ownerSessionId,
            character: characterForTurn,
          })
          if (sttAbortRef.current === sttController) sttAbortRef.current = null
          if (!generationRef.current.isCurrent(turnGeneration)
            || sttController.signal.aborted
            || bargeInPendingRef.current
            || modeForTurn !== modeRef.current
            || ownerSessionId !== sessionKey(sessionIdRef.current)) continue
          const text = result.text.trim()
          if (text === '') {
            latencyEvent('turn.empty', { trace_id: result.traceId, stt_to_send_ms: monotonicNow() - sttStarted })
            continue
          }
          const sendStarted = monotonicNow()
          if (modeForTurn === 'codex') {
            if (ownerSessionId === undefined) throw new Error('Codex 语音需要会话身份')
            if (!codexHistoryHydratedRef.current) continue
            const startController = new AbortController()
            codexStartAbortRef.current = startController
            generationRef.current.track(startController)
            actions.markCodexStartIntent()
            let started: CodexStartResult
            try {
              started = await codexStartRef.current(ownerSessionId, { text, character: characterRef.current }, startController.signal)
              // Register/bind the live fence around the Remote call.  The
              // durable answer may be settled before this promise resolves.
              actions.bindCodexStartIntent(started.executionId)
            } catch (error) {
              actions.cancelCodexStartIntent()
              throw error
            } finally {
              if (codexStartAbortRef.current === startController) codexStartAbortRef.current = null
              generationRef.current.untrack(startController)
            }
            const startedOwner: ActiveCodexOwner = {
              sessionId: ownerSessionId,
              executionId: started.executionId,
              generation: turnGeneration,
              status: codexStatusRef.current,
              interrupt: codexInterruptRef.current,
            }
            if (!generationRef.current.isCurrent(turnGeneration)
              || startController.signal.aborted
              || bargeInPendingRef.current
              || modeForTurn !== modeRef.current
              || ownerSessionId !== sessionKey(sessionIdRef.current)) {
              const released = await releaseExactCodex(startedOwner)
              actions.cancelCodexStartIntent()
              if (!released) {
                queueRef.current.clear()
                break
              }
              continue
            }
            activeCodexRef.current = startedOwner
            const waitController = new AbortController()
            codexWaitAbortRef.current = waitController
            generationRef.current.track(waitController)
            try {
              await waitForCodexTerminalRef.current(startedOwner, waitController.signal)
            } finally {
              if (codexWaitAbortRef.current === waitController) codexWaitAbortRef.current = null
              generationRef.current.untrack(waitController)
            }
            if (activeCodexRef.current?.sessionId === startedOwner.sessionId
              && activeCodexRef.current.executionId === startedOwner.executionId
              && activeCodexRef.current.generation === startedOwner.generation) activeCodexRef.current = null
            actions.cancelCodexStartIntent()
          } else {
            await sendTextRef.current(text)
          }
          if (generationRef.current.isCurrent(turnGeneration) && modeForTurn === modeRef.current) {
            latencyEvent('turn.send', {
              trace_id: result.traceId,
              duration_ms: Math.round((monotonicNow() - sendStarted) * 1000) / 1000,
              stt_to_send_ms: Math.round((sendStarted - sttStarted) * 1000) / 1000,
              text_chars: text.length,
              mode: modeForTurn,
            })
          }
        } catch (error) {
          if (!(error instanceof Error && error.name === 'AbortError')) {
            console.error('[ui-voice] 语音识别或发送失败')
            companionStateRef.current.dispatch({ type: 'interrupted' })
          }
        } finally {
          if (sttAbortRef.current === sttController) sttAbortRef.current = null
          generationRef.current.untrack(sttController)
        }
      }
    } finally {
      drainRunningRef.current = false
      if (mountedRef.current) setPhase(blockedRef.current ? 'blocked' : recorderRef.current === null ? 'idle' : 'listening')
    }
  }, [actions, markBlocked])
  drainRef.current = drain

  const toggle = useCallback(async () => {
    if (blocked || blockedRef.current) return
    if (modeRef.current === 'codex' && !codexHistoryHydratedRef.current) {
      if (mountedRef.current) setPhase('idle')
      return
    }
    const recorder = recorderRef.current
    if (recorder !== null) {
      bargeInTokenRef.current += 1
      bargeInPendingRef.current = false
      recorder.stop()
      recorderRef.current = null
      const released = await cancelPending(true)
      if (!released) return
      actions.bumpInterruptEpoch()
      actions.bumpTtsEpoch()
      companionState.dispatch({ type: 'reset' })
      if (mountedRef.current) setPhase('idle')
      return
    }

    try {
      const pendingRelease = pendingOwnerReleaseRef.current
      if (pendingRelease !== null && !(await pendingRelease)) throw new Error('Codex 执行未确认结束')
      if (!(await releaseActiveCodex())) throw new Error('Codex 执行未确认结束')
      await interruptCurrentMode()
    } catch {
      markBlocked(true)
      if (mountedRef.current) setPhase('blocked')
      return
    }
    companionState.dispatch({ type: 'listen_start' })
    const recorderOwnerSession = sessionKey(sessionIdRef.current)
    const nextRecorder = new MicRecorder({
      minSilenceMs: 1800,
      maxUtteranceMs: 30000,
      preRollMs: 1200,
      rmsThreshold: 0.01,
      noiseGateDb: -35,
      vad: new VadStream(),
      interruptThreshold: 0.06,
      interruptHoldMs: 250,
      onSpeechInterrupt: () => {
        if (recorderOwnerSession !== sessionKey(sessionIdRef.current)) return
        const token = ++bargeInTokenRef.current
        bargeInPendingRef.current = true
        // Capture may continue into the bounded queue, but no queued item can
        // cross into STT or Codex start until the remote release is confirmed.
        void cancelPending().then(() => interruptCurrentMode()).then(() => {
          if (token !== bargeInTokenRef.current || !mountedRef.current) return
          activeCodexRef.current = null
          generationRef.current.cancel()
          bargeInPendingRef.current = false
          void drainRef.current?.()
        }).catch(() => {
          if (token !== bargeInTokenRef.current) return
          bargeInPendingRef.current = false
          queueRef.current.clear()
          recorderRef.current?.stop()
          recorderRef.current = null
          markBlocked(true)
          if (mountedRef.current) setPhase('blocked')
        })
        companionStateRef.current.dispatch({ type: 'interrupted' })
      },
      onUtterance: (audio) => {
        if (recorderOwnerSession !== sessionKey(sessionIdRef.current)) return
        const queued = queueRef.current.enqueue(audio)
        latencyEvent('capture.endpoint', { audio_bytes: audio.byteLength, queue_count: queued.count, queue_bytes: queued.bytes })
        if (!queued.accepted) companionStateRef.current.dispatch({ type: 'interrupted' })
        else if (!bargeInPendingRef.current) void drainRef.current?.()
      },
    })
    recorderRef.current = nextRecorder
    setPhase('listening')
    try {
      await nextRecorder.start()
    } catch {
      console.error('[ui-voice] 麦克风启动失败')
      nextRecorder.stop()
      if (recorderRef.current === nextRecorder) recorderRef.current = null
      if (mountedRef.current) setPhase('idle')
    }
  }, [actions, blocked, cancelPending, companionState, drain, interruptCurrentMode, releaseActiveCodex])

  const label = phase === 'blocked'
    ? 'Codex 执行未确认结束，请稍后重试'
    : phase === 'idle'
      ? t('mic.title')
      : phase === 'listening' ? t('mic.listening') : t('mic.transcribing')
  const className = phase === 'idle'
    ? css.mic
    : phase === 'listening' ? `${css.mic} ${css.listening}` : `${css.mic} ${css.transcribing}`

  return (
    <>
      <button type="button" className={className} title={label} aria-label={label} onClick={() => void toggle()} disabled={blocked}>
        <MicIcon />
      </button>
      {blocked && <span role="status">Codex 执行未确认结束，请稍后重试</span>}
    </>
  )
})
