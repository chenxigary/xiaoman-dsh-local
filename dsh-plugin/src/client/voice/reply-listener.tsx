/**
 * ReplySpeakerMount: hidden per-session component (renders null) that streams
 * assistant text to TTS sentence-by-sentence — mirroring the original
 * backend's LMOutputProcessor (per-sentence chunks) so long replies start
 * speaking while the rest are still being synthesized.
 *
 * Each assistant chat node's text is split into complete sentences; as new
 * complete sentences appear (the node streams via `assistant/chunk`
 * publications), they are fetched from the bridge /api/tts through a serial
 * chain and played back in order through the shared ReplySpeaker's FIFO
 * queue. The trailing partial sentence is not spoken until it completes.
 *
 * History replay protection: the first DSH `open` snapshot fences only
 * settled durable assistant anchors, while running nodes remain live and are
 * spoken immediately. Barge-in (mic start) swallows the rest of the current
 * reply.
 */
import { memo, useEffect, useRef } from 'react'
import type { PropsLocale, PropsRuntime, PropsStore } from '@deepseek-ai/dsh-client-ui-slots'
// Type-only: pulls ui-conversation's SlotMap merge for PropsRuntime resolution.
import type {} from '@deepseek-ai/dsh-client-ui-conversation/client'
import type { AssistantChatData } from '@deepseek-ai/dsh-client-ui-conversation/client'
import { ttsStream } from '../bridge.ts'
import type { VoiceInjected } from '../contract.ts'
import type { VoiceStoreHandle } from '../agent-mode.ts'
import { cleanReplyText } from './clean.ts'
import { boundSpeechText, splitSentences } from './sentences.ts'
import { latencyEvent, newTraceId } from '../latency.ts'
import { SentenceAssembler } from './sentence-assembler.ts'
import type { CodexAnswerChatData } from '../codex-conversation.ts'
import {
  collectReplySpeechJobs,
  recordReplySentenceAccepted,
  establishReplyHistoryBaseline,
  isCodexLiveReplyNode,
  shouldRunCodexSpeechProjection,
  type ReplyHistoryAnchor,
  type ReplySpeechNode,
  MAX_REPLY_HISTORY,
  rollbackReplySpeechJob,
  type ReplySpeechJob,
} from './reply-history.ts'
import { ReplyExecutionFence, ReplyTtsGeneration } from './reply-generation.ts'
import { MAX_REPLY_TTS_BYTES, MAX_REPLY_TTS_RETRIES, ReplyTtsJobLedger } from './reply-job-ledger.ts'

const RETRY_WAKE_MS = 100
const MAX_NATIVE_RETRY_JOBS = 128
const MAX_CODEX_DEFERRED_JOBS = 128

interface DeferredCodexSpeechJob {
  readonly text: string
  readonly key: string
  readonly generation: number
}

/** Read the assistant row payload off a chat view node (kind `assistant-step`). */
function assistantData(node: { kind: string; data: unknown }): AssistantChatData | undefined {
  if (node.kind !== 'assistant-step') return undefined
  return node.data as AssistantChatData
}

/** Join the node's text blocks (reasoning/tool-call/image excluded). */
function nodeText(data: AssistantChatData): string {
  return data.blocks
    .filter((block) => block.kind === 'text')
    .map((block) => block.text)
    .join('\n')
}

/** Full props: framework runtime share + `voice` locale seat + injected face. */
export type ReplySpeakerMountProps =
  PropsRuntime<'conversation.input.left'> & PropsStore<VoiceStoreHandle> & PropsLocale<'voice'> & VoiceInjected

/**
 * @param props - framework runtime + locale + injected speaker/abort face.
 */
export const ReplySpeakerMount = memo(function ReplySpeakerMount({
  useSession,
  useStore,
  actions,
  speaker,
  avatarAudio,
  registerTtsAbort,
  registerInterruptHandler,
  registerSessionMount,
  sessionId,
  companionState,
}: ReplySpeakerMountProps) {
  // Subscribe to the WHOLE snapshot (see T6: `s.chat.nodes` is a stable live
  // store whose reference never changes, so selecting it would never re-render
  // — the top-level snapshot object IS swapped on every publication).
  const snapshot = useSession((s) => s)
  const voice = useStore(state => state.voice)
  const mode = useStore(state => state.mode)
  const character = useStore(state => state.character)
  const ttsEpoch = useStore(state => state.ttsEpoch)
  const codexStartIntent = useStore(state => state.codexStartIntent)
  const codexStartIntentConsumed = useStore(state => state.codexStartIntentConsumed)
  const codexStartHighWater = useStore(state => state.codexStartHighWater)
  const codexStartExecutionId = useStore(state => state.codexStartExecutionId)
  const codexHistoryHighWater = useStore(state => state.codexHistoryHighWater)
  const codexHistoryHydrated = useStore(state => state.codexHistoryHydrated)
  const modeRef = useRef(mode)
  const sessionRef = useRef(sessionId)
  modeRef.current = mode
  sessionRef.current = sessionId

  // Per-node complete sentences already spoken (node.key -> count).
  const spokenRef = useRef(new Map<string, number>())
  // History replay protection: baseline anchor. On mount the conversation
  // snapshot can be EMPTY (session history loads asynchronously after a
  // restart), so a one-shot seed there would miss the history and every old
  // reply would replay. Instead we wait until the first SETTLED assistant
  // node arrives, then set the baseline to the current max anchor — nothing
  // at or below it ever speaks. Live (running) nodes are never used for the
  // baseline, so a fresh reply in a brand-new session still speaks.
  const baselineRef = useRef<number | null>(null)
  // Serial TTS fetch chain: sentence N+1's fetch starts after N's resolves
  // (playback drains independently through the speaker queue — pipelined).
  const chainRef = useRef<Promise<void>>(Promise.resolve())
  // Barge-in: swallow the CURRENT reply only. We record the exact anchor of
  // the reply being interrupted (never a "<= max" line): if the interrupt
  // flag is consumed after a NEW reply already appeared in the snapshot, a
  // range-based skip would swallow that fresh reply too — the "new reply
  // never speaks" bug. Exact-anchor skip lets later replies play normally.
  const interruptRef = useRef(false)
  const skipAnchorRef = useRef(0)
  const skipUntilRef = useRef(0)
  // `spokenRef` is bounded to keep session memory finite. This separate
  // monotonic fence prevents an evicted settled node from replaying on a
  // later snapshot.
  const settledHighWaterRef = useRef({
    value: 0,
    openKeys: new Map<string, number>(),
    acceptedAnchors: new Map<string, number>(),
    acceptedCounts: new Map<string, number>(),
  })
  const codexBaselineRef = useRef<number | null>(null)
  const codexSpokenRef = useRef(new Map<string, number>())
  const codexAssemblerRef = useRef<SentenceAssembler | null>(null)
  const codexKeyRef = useRef<string | null>(null)
  const codexTerminalRef = useRef(new Set<string>())
  const codexControllersRef = useRef(new Set<AbortController>())
  const codexChainRef = useRef<Promise<void>>(Promise.resolve())
  const codexStoppedRef = useRef(false)
  const codexBackpressureRef = useRef(false)
  const codexPendingRef = useRef(new ReplyTtsJobLedger())
  // SentenceAssembler may emit more chunks than the bounded ledger can
  // reserve in one synchronous terminal snapshot. Keep the unreserved tail
  // as a bounded source frontier and drain it after accepted jobs retire.
  const codexDeferredRef = useRef<DeferredCodexSpeechJob[]>([])
  const nativeBackpressureRef = useRef(false)
  const codexWakeRef = useRef<(() => void) | null>(null)
  const nativeWakeRef = useRef<(() => void) | null>(null)
  const codexRetryTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const nativeRetryTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const nativeRetryJobsRef = useRef(new Map<string, ReplySpeechJob>())
  const nativeRetryAttemptsRef = useRef(new Map<string, number>())
  const nativeRetryRunningRef = useRef(false)
  // Keep the latest bounded source snapshot so a speaker-drain wake can
  // reserve the next prefix without waiting for another durable publication.
  // `spokenRef` is the reserved cursor; collectReplySpeechJobs therefore
  // resumes at sentence 128 instead of replaying 0..127.
  const nativeSourceNodesRef = useRef<ReplySpeechNode[]>([])
  const codexExecutionRef = useRef(new ReplyExecutionFence())
  // Every queued TTS job captures this generation and its answer-node key.
  // Interrupt/unmount and a new execution advance it, so a stale serial-chain
  // job cannot wake after the next turn has reset `codexStoppedRef` and replay
  // the old answer.
  const codexGenerationRef = useRef(new ReplyTtsGeneration())
  const nativeGenerationRef = useRef(new ReplyTtsGeneration())
  const nativeControllersRef = useRef(new Set<AbortController>())
  const activeSpeakerRef = useRef(speaker)
  const previousSessionRef = useRef(sessionId)
  const previousModeRef = useRef(mode)

  const resetOwnerState = () => {
    nativeGenerationRef.current.advance()
    codexGenerationRef.current.advance()
    for (const controller of nativeControllersRef.current) controller.abort()
    for (const controller of codexControllersRef.current) controller.abort()
    nativeControllersRef.current.clear()
    codexControllersRef.current.clear()
    activeSpeakerRef.current.stop()
    chainRef.current = Promise.resolve()
    codexChainRef.current = Promise.resolve()
    codexAssemblerRef.current?.reset()
    codexAssemblerRef.current = null
    spokenRef.current.clear()
    baselineRef.current = null
    skipAnchorRef.current = 0
    skipUntilRef.current = 0
    settledHighWaterRef.current.value = 0
    settledHighWaterRef.current.openKeys?.clear()
    settledHighWaterRef.current.acceptedAnchors?.clear()
    settledHighWaterRef.current.acceptedCounts?.clear()
    interruptRef.current = false
    codexBaselineRef.current = null
    codexSpokenRef.current.clear()
    codexKeyRef.current = null
    codexTerminalRef.current.clear()
    codexStoppedRef.current = true
    codexBackpressureRef.current = false
    codexPendingRef.current.clear()
    codexDeferredRef.current = []
    nativeBackpressureRef.current = false
    codexWakeRef.current = null
    nativeWakeRef.current = null
    if (codexRetryTimerRef.current !== null) globalThis.clearTimeout(codexRetryTimerRef.current)
    if (nativeRetryTimerRef.current !== null) globalThis.clearTimeout(nativeRetryTimerRef.current)
    codexRetryTimerRef.current = null
    nativeRetryTimerRef.current = null
    nativeRetryJobsRef.current.clear()
    nativeRetryAttemptsRef.current.clear()
    nativeSourceNodesRef.current = []
    codexExecutionRef.current.reset()
    registerTtsAbort(null)
  }

  const stopCodexAudio = () => {
    codexStoppedRef.current = true
    codexGenerationRef.current.advance()
    codexAssemblerRef.current?.reset()
    codexAssemblerRef.current = null
    codexBackpressureRef.current = false
    codexPendingRef.current.clear()
    activeSpeakerRef.current.stop()
    for (const controller of codexControllersRef.current) controller.abort()
    codexControllersRef.current.clear()
    codexChainRef.current = Promise.resolve()
    codexWakeRef.current = null
    codexExecutionRef.current.reset()
    if (codexRetryTimerRef.current !== null) globalThis.clearTimeout(codexRetryTimerRef.current)
    codexRetryTimerRef.current = null
    registerTtsAbort(null)
  }

  const stopNativeAudio = () => {
    nativeGenerationRef.current.advance()
    for (const controller of nativeControllersRef.current) controller.abort()
    nativeControllersRef.current.clear()
    chainRef.current = Promise.resolve()
    nativeWakeRef.current = null
    if (nativeRetryTimerRef.current !== null) globalThis.clearTimeout(nativeRetryTimerRef.current)
    nativeRetryTimerRef.current = null
    nativeRetryJobsRef.current.clear()
    nativeRetryAttemptsRef.current.clear()
    nativeSourceNodesRef.current = []
    activeSpeakerRef.current.stop()
    registerTtsAbort(null)
  }

  const stopAllAudio = () => {
    stopNativeAudio()
    stopCodexAudio()
  }

  useEffect(() => {
    registerSessionMount(true)
    return () => registerSessionMount(false)
  }, [registerSessionMount])

  // A queue drain is an operational wake, not a durable snapshot. Both
  // pipelines subscribe to the shared speaker so a rejected job can retry as
  // soon as capacity returns; the generation/mode checks remain authoritative.
  useEffect(() => speaker.onSpeakingChange(() => {
    if (speaker.speaking) return
    codexWakeRef.current?.()
    nativeWakeRef.current?.()
  }), [speaker])

  // This effect is intentionally declared before either snapshot TTS effect:
  // changing mode or session aborts old work and clears every replay fence
  // before the new owner can enqueue anything.
  useEffect(() => {
    const ownerChanged = previousSessionRef.current !== sessionId
    const modeChanged = previousModeRef.current !== mode
    const speakerChanged = activeSpeakerRef.current !== speaker
    if (ownerChanged || modeChanged || speakerChanged) resetOwnerState()
    activeSpeakerRef.current = speaker
    previousSessionRef.current = sessionId
    previousModeRef.current = mode
  }, [mode, sessionId, speaker, registerTtsAbort]) // eslint-disable-line react-hooks/exhaustive-deps

  // Register the barge-in handler once (the mic calls interruptReply).
  useEffect(() => {
    registerInterruptHandler(() => {
      interruptRef.current = true
    })
    return () => registerInterruptHandler(null)
  }, [registerInterruptHandler])

  // Unmount: stop playback and release any in-flight TTS.
  useEffect(() => () => {
    stopAllAudio()
    registerTtsAbort(null)
  }, [registerTtsAbort, speaker]) // eslint-disable-line react-hooks/exhaustive-deps

  // Local stop/abort happens on the companion INTERRUPTED edge; the host
  // interrupt is awaited separately by the typed Remote coordinator.
  useEffect(() => companionState.onStateChange((state) => {
    if (state === 'INTERRUPTED') stopAllAudio()
  }), [companionState]) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (ttsEpoch > 0) stopAllAudio()
  }, [ttsEpoch]) // eslint-disable-line react-hooks/exhaustive-deps

  // Voice-off is a hard TTS edge for both pipelines.  In particular, a
  // Codex pending job must not be allowed to retry in a microtask loop while
  // `voice` is false; the generation/queue fence makes every stale promise
  // inert until a later snapshot is eligible again.
  useEffect(() => {
    if (!voice) stopAllAudio()
  }, [voice]) // eslint-disable-line react-hooks/exhaustive-deps

  /** Durable Codex answer projection: sentence TTS follows chat nodes, never a bridge socket. */
  useEffect(() => {
    // Hydration is recorded even when voice is disabled; the UI can then
    // safely arm a future turn without using an uninitialized high-water.
    if (mode !== 'codex' || sessionId === undefined || snapshot.openState !== 'open') return

    const scheduleWake = (): void => {
      if (codexRetryTimerRef.current !== null) return
      codexRetryTimerRef.current = globalThis.setTimeout(() => {
        codexRetryTimerRef.current = null
        codexWakeRef.current?.()
      }, RETRY_WAKE_MS)
    }

    // A chunk is source-consumed when the sentence assembler sees it, but it
    // is only playback-committed after TTS succeeds and `speaker.speak`
    // accepts it. Failed/rejected chunks stay in this bounded ledger and are
    // retried by the next durable snapshot once the speaker drains.
    if (codexBackpressureRef.current) {
      if (speaker.speaking) return
      codexBackpressureRef.current = false
      codexStoppedRef.current = false
    }

    const pump = (): void => {
      if (!voice || codexStoppedRef.current || codexBackpressureRef.current) return
      const item = codexPendingRef.current.nextPending()
      if (item === undefined) return
      codexPendingRef.current.markQueued(item)
      codexChainRef.current = codexChainRef.current.then(async () => {
        const current = !codexStoppedRef.current
          && modeRef.current === 'codex'
          && sessionRef.current === sessionId
          && codexGenerationRef.current.isCurrent(item.generation)
          && codexKeyRef.current === item.key
          && voice
        if (!current) {
          codexPendingRef.current.markRetry(item, false)
          return
        }
        const controller = new AbortController()
        codexControllersRef.current.add(controller)
        codexGenerationRef.current.track(controller)
        registerTtsAbort(controller)
        const traceId = newTraceId()
        latencyEvent('codex.sentence', {
          trace_id: traceId,
          session_id_present: true,
          text_chars: item.text.length,
        })
        try {
          const remoteAudio = character === 'xiaoman' && avatarAudio.remote
          let speechStarted = false
          await ttsStream(item.text, ({ pcm, sampleRate }) => {
            const currentChunk = !codexStoppedRef.current
              && codexGenerationRef.current.isCurrent(item.generation)
              && codexKeyRef.current === item.key
              && !controller.signal.aborted
              && modeRef.current === 'codex'
              && sessionRef.current === sessionId
              && voice
            if (!currentChunk) return false
            const result = speaker.speakPcm(pcm, sampleRate, remoteAudio)
            if (result.accepted && !speechStarted) {
              speechStarted = true
              companionState.dispatch({ type: 'speech_start' })
            }
            return result.accepted
          }, controller.signal, traceId, {
            sessionId: String(sessionId),
            character,
            turnId: item.key,
            generation: item.generation,
          })
          if (!codexStoppedRef.current
            && codexGenerationRef.current.isCurrent(item.generation)
            && codexKeyRef.current === item.key
            && !controller.signal.aborted
            && modeRef.current === 'codex'
            && sessionRef.current === sessionId
            && voice) {
            codexPendingRef.current.markAccepted(item)
          } else {
            codexPendingRef.current.markRetry(item, false)
          }
        } catch (error) {
          const retryable = codexPendingRef.current.markRetry(item)
          if ((error as Error | undefined)?.name !== 'AbortError') {
            console.error('[ui-voice] Codex reply TTS failed:', error)
            // A transport failure is a typed retry boundary, not a spoken
            // cursor advance. Keep this item for the next snapshot.
            codexStoppedRef.current = true
            codexBackpressureRef.current = true
            if (retryable) scheduleWake()
            else companionState.dispatch({ type: 'interrupted' })
          }
        } finally {
          codexControllersRef.current.delete(controller)
          codexGenerationRef.current.untrack(controller)
          if (codexControllersRef.current.size === 0) registerTtsAbort(null)
        }
      }).then(() => {
        if (!codexStoppedRef.current && !codexBackpressureRef.current) pump()
      })
    }

    const enqueueNow = (text: string, key: string, generation: number): boolean => {
      const job = codexPendingRef.current.enqueue(text, key, generation)
      if (job === undefined) return false
      // Keep accepted history bounded without evicting an uncommitted retry.
      codexPendingRef.current.prune()
      return true
    }

    const deferredBytes = (): number => codexDeferredRef.current.reduce(
      (total, item) => total + new TextEncoder().encode(item.text).byteLength,
      0,
    )

    const flushDeferred = (): void => {
      let added = false
      while (codexDeferredRef.current.length > 0) {
        const item = codexDeferredRef.current[0]
        if (item === undefined) break
        if (!codexGenerationRef.current.isCurrent(item.generation) || codexKeyRef.current !== item.key) {
          codexDeferredRef.current.shift()
          continue
        }
        if (!enqueueNow(item.text, item.key, item.generation)) break
        codexDeferredRef.current.shift()
        added = true
      }
      if (added) pump()
    }

    const enqueue = (text: string, key: string, generation: number): void => {
      if (text.trim() === '') return
      if (!enqueueNow(text, key, generation)) {
        const bytes = new TextEncoder().encode(text).byteLength
        // The ledger is intentionally bounded, but source chunks beyond its
        // first prefix remain recoverable until accepted entries retire.
        if (codexDeferredRef.current.length >= MAX_CODEX_DEFERRED_JOBS
          || deferredBytes() + bytes > MAX_REPLY_TTS_BYTES) {
          codexStoppedRef.current = true
          codexBackpressureRef.current = true
          companionState.dispatch({ type: 'interrupted' })
          return
        }
        codexDeferredRef.current.push({ text, key, generation })
        return
      }
      pump()
    }

    const wake = (): void => {
      if (!voice || speaker.speaking || modeRef.current !== 'codex' || sessionRef.current !== sessionId) return
      codexBackpressureRef.current = false
      codexStoppedRef.current = false
      flushDeferred()
      if (codexPendingRef.current.nextPending() === undefined) return
      pump()
    }
    codexWakeRef.current = wake

    const nodes = [...snapshot.chat.nodes.values()]
      .filter((node): node is typeof node & { kind: 'codex-answer'; data: CodexAnswerChatData } => node.kind === 'codex-answer')
      .sort((left, right) => left.anchorSeq - right.anchorSeq)
    if (codexBaselineRef.current === null) {
      const settled = nodes.filter(node => node.data.status !== 'running').map(node => node.anchorSeq)
      codexBaselineRef.current = settled.length === 0 ? 0 : Math.max(...settled)
      if (!codexHistoryHydrated) {
        actions.markCodexHistoryHydrated(codexBaselineRef.current)
      } else if (codexBaselineRef.current > codexHistoryHighWater) {
        actions.setCodexHistoryHighWater(codexBaselineRef.current)
      }
    }
    if (!shouldRunCodexSpeechProjection(mode, voice, sessionId, snapshot.openState)) {
      if (codexWakeRef.current === wake) codexWakeRef.current = null
      return
    }
    const liveIntent = codexStartIntent > codexStartIntentConsumed
    const node = nodes.findLast(candidate => {
      if (!liveIntent) return candidate.anchorSeq > (codexBaselineRef.current ?? 0)
      return isCodexLiveReplyNode({
        liveIntent: true,
        startHighWater: codexStartHighWater,
        executionId: codexStartExecutionId,
      }, { anchorSeq: candidate.anchorSeq, executionId: candidate.data.executionId })
    })
    if (node !== undefined) {
      // A start intent is consumed by the first matching durable answer node,
      // including a first snapshot that is already settled.  Persisting this
      // acknowledgement in the session store prevents a remount from
      // replaying that live first answer as if it were new history.
      if (liveIntent) actions.acknowledgeCodexStartIntent(codexStartIntent)
      const data = node.data
      if (data.status === 'interrupted' || data.status === 'failed') {
        stopCodexAudio()
        companionState.dispatch({ type: 'interrupted' })
        return
      }
      const speakableFinal = data.phase === 'final_answer'
        && data.speakable === true
        && data.sequenceGap !== true
      // A new Codex execution may reuse the same apply-level speaker. Fence
      // every accepted/queued clip from the previous execution before the
      // new durable node can enqueue its first sentence.
      const executionFence = codexExecutionRef.current.begin(data.executionId)
      if (executionFence.changed) {
        activeSpeakerRef.current.stop()
        codexGenerationRef.current.advance()
        for (const controller of codexControllersRef.current) controller.abort()
        codexControllersRef.current.clear()
        codexChainRef.current = Promise.resolve()
        codexAssemblerRef.current?.reset()
        codexAssemblerRef.current = null
        codexPendingRef.current.clear()
        codexDeferredRef.current = []
        codexBackpressureRef.current = false
        codexStoppedRef.current = false
        codexKeyRef.current = null
        codexTerminalRef.current.clear()
      }
      if (codexKeyRef.current !== node.key) {
        codexGenerationRef.current.advance()
        codexAssemblerRef.current?.reset()
        codexPendingRef.current.clear()
        codexDeferredRef.current = []
        codexBackpressureRef.current = false
        codexKeyRef.current = node.key
        codexSpokenRef.current.set(node.key, 0)
        while (codexSpokenRef.current.size > MAX_REPLY_HISTORY) {
          const oldest = codexSpokenRef.current.keys().next().value as string | undefined
          if (oldest === undefined) break
          codexSpokenRef.current.delete(oldest)
        }
        codexAssemblerRef.current = new SentenceAssembler({
          maxChars: 512,
          // Codex text paints immediately; keep the unfinished-clause hold
          // short so local TTS can begin without an extra near-second delay.
          maxWaitMs: 400,
          onChunk: chunk => {
            if (chunk.speakable) enqueue(chunk.text, node.key, codexGenerationRef.current.current)
          },
        })
        codexStoppedRef.current = false
        companionState.dispatch({ type: 'thinking' })
      }
      const consumed = codexSpokenRef.current.get(node.key) ?? 0
      if (speakableFinal && data.text.length > consumed) {
        codexSpokenRef.current.set(node.key, data.text.length)
        // Only final_answer text enters the Codex answer node. Commentary,
        // tool summaries, and the durable user node never reach TTS.
        codexAssemblerRef.current?.push(data.text.slice(consumed))
      }
      if (speakableFinal && data.status === 'completed' && !codexTerminalRef.current.has(node.key)) {
        codexTerminalRef.current.add(node.key)
        while (codexTerminalRef.current.size > MAX_REPLY_HISTORY) {
          const oldest = codexTerminalRef.current.values().next().value as string | undefined
          if (oldest === undefined) break
          codexTerminalRef.current.delete(oldest)
        }
        codexAssemblerRef.current?.finish(data.text)
        companionState.dispatch({ type: 'speech_end' })
      }
    }
    return () => {
      if (codexWakeRef.current === wake) codexWakeRef.current = null
    }
  }, [actions, avatarAudio, character, companionState, codexHistoryHighWater, codexHistoryHydrated, codexStartExecutionId, codexStartHighWater, codexStartIntent, codexStartIntentConsumed, mode, registerTtsAbort, sessionId, snapshot, speaker, voice])

  // Stream new complete sentences to TTS on every snapshot change.
  useEffect(() => {
    if (mode !== 'dsh' || !voice) return
    if (nativeBackpressureRef.current) {
      // Do not reserve another batch while the bounded speaker queue still
      // owns work. A later snapshot retries only after it drains.
      if (speaker.speaking) return
      nativeBackpressureRef.current = false
    }

    // Barge-in swallowed the CURRENT reply: remember its exact anchor so only
    // that reply's remaining sentences are skipped; replies that appear
    // later (or that already appeared) still speak. (Playback/fetch abort is
    // handled by interruptReply itself.)
    if (interruptRef.current) {
      let maxAnchor = 0
      for (const node of snapshot.chat.nodes.values()) {
        if (node.kind === 'assistant-step' && node.anchorSeq > maxAnchor) maxAnchor = node.anchorSeq
      }
      if (maxAnchor > 0) skipAnchorRef.current = maxAnchor
      interruptRef.current = false
      return
    }

    // Hydration is complete only at the DSH `open` snapshot.  Establish the
    // fence from SETTLED assistant anchors present at that moment (never from
    // running/live nodes), then continue through this same effect so a first
    // live reply in an otherwise blank session is not swallowed.
    const historyAnchors: ReplyHistoryAnchor[] = []
    for (const node of snapshot.chat.nodes.values()) {
      if (node.kind !== 'assistant-step') continue
      const data = assistantData(node)
      if (data === undefined) continue
      historyAnchors.push({ anchorSeq: node.anchorSeq, status: data.status })
    }
    const hydratedBaseline = establishReplyHistoryBaseline(snapshot.openState, historyAnchors, baselineRef.current)
    if (baselineRef.current === null && hydratedBaseline !== null) {
      baselineRef.current = hydratedBaseline
      settledHighWaterRef.current.value = hydratedBaseline
      // `0` marks an open snapshot with no settled history; it is a state
      // sentinel, not an anchor fence.  Keep an anchor-0 live node eligible.
      skipUntilRef.current = hydratedBaseline > 0 ? hydratedBaseline : Number.NEGATIVE_INFINITY
    }
    if (baselineRef.current === null) return

    // Collect the complete sentences that are new (beyond each node's spoken
    // count), in (anchor, index) order. A SETTLED node also flushes its
    // trailing partial (the reply ended without a terminal punctuation, like
    // a credit line) — mirroring the original backend's end-of-response
    // flush; running nodes wait for the partial to complete.
    const speechNodes: ReplySpeechNode[] = []
    for (const node of snapshot.chat.nodes.values()) {
      if (node.kind !== 'assistant-step') continue
      if (node.anchorSeq <= skipUntilRef.current) continue
      if (node.anchorSeq === skipAnchorRef.current) continue
      const data = assistantData(node)
      if (data === undefined || data.status === 'interrupted') continue
      const { sentences, partial } = splitSentences(cleanReplyText(nodeText(data), 100000))
      speechNodes.push({
        key: node.key,
        anchorSeq: node.anchorSeq,
        status: data.status,
        sentences,
        partial,
      })
    }
    const jobs = collectReplySpeechJobs(
      speechNodes,
      skipUntilRef.current,
      skipAnchorRef.current,
      spokenRef.current,
      settledHighWaterRef.current,
    )
    nativeSourceNodesRef.current = speechNodes
    const speechNodeByKey = new Map(speechNodes.map(node => [node.key, node]))
    const speechTotalByKey = new Map(
      speechNodes.map(node => [node.key, node.sentences.flatMap(sentence => boundSpeechText(sentence)).length
        + (node.partial === null || node.status !== 'settled' ? 0 : boundSpeechText(node.partial).length)]),
    )

    const jobKey = (job: ReplySpeechJob): string => `${job.key}:${job.index}`
    const scheduleWake = (): void => {
      if (nativeRetryTimerRef.current !== null) return
      nativeRetryTimerRef.current = globalThis.setTimeout(() => {
        nativeRetryTimerRef.current = null
        nativeWakeRef.current?.()
      }, RETRY_WAKE_MS)
    }
    const rememberRetry = (job: ReplySpeechJob, consumeAttempt: boolean): boolean => {
      const key = jobKey(job)
      if (consumeAttempt) {
        const attempts = (nativeRetryAttemptsRef.current.get(key) ?? 0) + 1
        nativeRetryAttemptsRef.current.set(key, attempts)
        if (attempts >= MAX_REPLY_TTS_RETRIES) {
          nativeRetryJobsRef.current.delete(key)
          return false
        }
      }
      if (!nativeRetryJobsRef.current.has(key) && nativeRetryJobsRef.current.size >= MAX_NATIVE_RETRY_JOBS) return false
      nativeRetryJobsRef.current.set(key, job)
      return true
    }
    const runJob = async (job: ReplySpeechJob, generation: number): Promise<boolean> => {
      if (interruptRef.current
        || nativeBackpressureRef.current
        || modeRef.current !== 'dsh'
        || sessionRef.current !== sessionId
        || !voice
        || !nativeGenerationRef.current.isCurrent(generation)) return false
      const controller = new AbortController()
      nativeControllersRef.current.add(controller)
      nativeGenerationRef.current.track(controller)
      registerTtsAbort(controller)
      const traceId = newTraceId()
      latencyEvent('reply.sentence', {
        trace_id: traceId,
        anchor: job.anchor,
        sentence_index: job.index,
        text_chars: job.sentence.length,
      })
      try {
        const remoteAudio = character === 'xiaoman' && avatarAudio.remote
        await ttsStream(job.sentence, ({ pcm, sampleRate }) => {
          const currentChunk = !interruptRef.current
            && modeRef.current === 'dsh'
            && sessionRef.current === sessionId
            && voice
            && nativeGenerationRef.current.isCurrent(generation)
            && !controller.signal.aborted
          if (!currentChunk) return false
          return speaker.speakPcm(pcm, sampleRate, remoteAudio).accepted
        }, controller.signal, traceId, {
          sessionId: sessionId === undefined ? undefined : String(sessionId),
          character,
          turnId: job.key,
          generation,
        })
        if (interruptRef.current
          || modeRef.current !== 'dsh'
          || sessionRef.current !== sessionId
          || !voice
          || !nativeGenerationRef.current.isCurrent(generation)
          || controller.signal.aborted) return false
        nativeRetryJobsRef.current.delete(jobKey(job))
        nativeRetryAttemptsRef.current.delete(jobKey(job))
        const speechNode = speechNodeByKey.get(job.key)
        const speechTotal = speechTotalByKey.get(job.key)
        if (speechNode !== undefined && speechTotal !== undefined) {
          recordReplySentenceAccepted(
            settledHighWaterRef.current,
            job.key,
            speechNode.anchorSeq,
            job.index,
            speechTotal,
            speechNode.status,
          )
        }
        const spoken = spokenRef.current.get(job.key) ?? 0
        if (spoken <= job.index) spokenRef.current.set(job.key, job.index + 1)
        return true
      } catch (error) {
        if ((error as Error | undefined)?.name === 'AbortError') return false
        console.error('[ui-voice] reply TTS failed:', error)
        rollbackReplySpeechJob(spokenRef.current, job)
        nativeBackpressureRef.current = true
        const retryable = rememberRetry(job, true)
        if (retryable) scheduleWake()
        else companionState.dispatch({ type: 'interrupted' })
        return false
      } finally {
        nativeControllersRef.current.delete(controller)
        nativeGenerationRef.current.untrack(controller)
        if (nativeControllersRef.current.size === 0) registerTtsAbort(null)
      }
    }

    const pumpRetries = (): void => {
      if (speaker.speaking || nativeRetryRunningRef.current || nativeRetryJobsRef.current.size === 0) return
      nativeBackpressureRef.current = false
      nativeRetryRunningRef.current = true
      const generation = nativeGenerationRef.current.current
      const retryJobs = [...nativeRetryJobsRef.current.values()].sort((left, right) => (left.anchor - right.anchor) || (left.index - right.index))
      chainRef.current = chainRef.current.then(async () => {
        for (const job of retryJobs) {
          if (!nativeRetryJobsRef.current.has(jobKey(job))) continue
          if (!await runJob(job, generation)) break
        }
      }).finally(() => {
        nativeRetryRunningRef.current = false
      })
    }
    const enqueueJobs = (queuedJobs: readonly ReplySpeechJob[]): void => {
      const generation = nativeGenerationRef.current.current
      let chainFailed = false
      chainRef.current = queuedJobs.reduce(
        (chain, job) => chain.then(async () => {
          if (chainFailed) {
            rollbackReplySpeechJob(spokenRef.current, job)
            rememberRetry(job, false)
            return
          }
          const accepted = await runJob(job, generation)
          if (!accepted) chainFailed = true
        }),
        chainRef.current,
      )
    }

    const wake = (): void => {
      if (modeRef.current !== 'dsh' || sessionRef.current !== sessionId || speaker.speaking) return
      // A single terminal snapshot can contain more than the 128-job safety
      // budget. Once accepted jobs drain, reserve only the next source prefix
      // from the retained snapshot; never drop or re-parse the already
      // reserved prefix.
      const moreJobs = collectReplySpeechJobs(
        nativeSourceNodesRef.current,
        skipUntilRef.current,
        skipAnchorRef.current,
        spokenRef.current,
        settledHighWaterRef.current,
      )
      if (moreJobs.length > 0) enqueueJobs(moreJobs)
      pumpRetries()
    }
    nativeWakeRef.current = wake

    // Chain the fetches serially (order preserved; playback pipelines via the
    // speaker queue). Failed reservations move to the bounded retry ledger;
    // the speaker drain and timer wake it without requiring a new snapshot.
    if (jobs.length > 0) enqueueJobs(jobs)
  }, [avatarAudio, character, mode, registerTtsAbort, sessionId, snapshot, speaker, voice])

  return null
})
