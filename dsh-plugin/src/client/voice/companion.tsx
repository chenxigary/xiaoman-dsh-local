/**
 * CompanionWindow: reproduces the original hf-realtime-voice right-side
 * animation in DSH — a full-height column on the right (default 55vw):
 *
 *  - Idle: loops `bg-images` videos, advancing to the next on `ended`.
 *  - Speaking: while the ReplySpeaker is playing, cross-fades in a
 *    `task-videos` video (looping), then fades back to idle.
 *  - Draggable: an inner-edge handle resizes the column (240px–70vw,
 *    persisted) and double-clicking it flips the column to the left edge.
 *  - Toggle: `s2s.voice.companion` ('1'/'0', default on) hides it entirely.
 *
 * pointer-events:none on the column so chat interaction is never blocked;
 * only the drag handle is interactive.
 */
import { memo, useCallback, useEffect, useRef, useState } from 'react'
import type { PropsLocale, PropsRuntime, PropsStore } from '@deepseek-ai/dsh-client-ui-slots'
// Type-only: pulls ui-conversation's SlotMap merge for PropsRuntime resolution.
import type {} from '@deepseek-ai/dsh-client-ui-conversation/client'
import { bridgeBase } from '../bridge.ts'
import type { VoiceInjected } from '../contract.ts'
import type { VoiceStoreHandle } from '../agent-mode.ts'
import type { CompanionState } from '../companion-state.ts'
import { avatarPlaybackIsStalled, shouldLoopIdleVideo } from './companion-media.ts'
import { connectAvatar, type AvatarConnection } from './avatar-webrtc.ts'
import css from './CompanionWindow.module.css'

const WIDTH_KEY = 's2s.voice.companionW'
const SIDE_KEY = 's2s.voice.companionSide'

const VIEWPORT_WIDTH = typeof window === 'undefined' ? 1024 : window.innerWidth
const MIN_WIDTH_VW = Math.max(10, 240 / VIEWPORT_WIDTH * 100) // ~240px
const MAX_WIDTH_VW = 70
const AVATAR_STALL_PROBE_MS = 4000

function readWidth(): number {
  try {
    const value = Number.parseFloat(localStorage.getItem(WIDTH_KEY) ?? '')
    if (Number.isFinite(value) && value >= MIN_WIDTH_VW && value <= MAX_WIDTH_VW) return value
  } catch {
    // fall through to default
  }
  return 55
}

function readSide(): 'left' | 'right' {
  try {
    return localStorage.getItem(SIDE_KEY) === 'left' ? 'left' : 'right'
  } catch {
    return 'right'
  }
}

/** Full props: framework runtime share + `voice` locale seat + injected face. */
export type CompanionWindowProps =
  PropsRuntime<'conversation.input.left'> & PropsStore<VoiceStoreHandle> & PropsLocale<'voice'> & VoiceInjected

/**
 * @param props - framework runtime + locale + injected speaker face.
 */
export const CompanionWindow = memo(function CompanionWindow({ speaker, avatarAudio, companionState, useStore, registerSessionMount, sessionId }: CompanionWindowProps) {
  const visible = useStore(state => state.companion)
  const characterId = useStore(state => state.character)
  const [widthVw, setWidthVw] = useState<number>(readWidth)
  const [side, setSide] = useState<'left' | 'right'>(readSide)
  const [speaking, setSpeaking] = useState<boolean>(speaker.speaking)
  const [lifecycleState, setLifecycleState] = useState<CompanionState>(companionState.state)
  const [bgVideos, setBgVideos] = useState<string[]>([])
  const [taskVideos, setTaskVideos] = useState<string[]>([])
  const [bgIndex, setBgIndex] = useState(0)
  const [taskIndex, setTaskIndex] = useState(0)
  const [avatarConnected, setAvatarConnected] = useState(false)
  const [remoteAudio, setRemoteAudio] = useState(false)
  const idleRef = useRef<HTMLVideoElement | null>(null)
  const speakRef = useRef<HTMLVideoElement | null>(null)
  const avatarRef = useRef<HTMLVideoElement | null>(null)
  const dragRef = useRef<{ startX: number; startWidth: number; current: number } | null>(null)

  useEffect(() => {
    registerSessionMount(true)
    return () => registerSessionMount(false)
  }, [registerSessionMount])

  // Load media lists from the bridge on mount, then re-poll every 30 s so
  // videos dropped into the folders are picked up without a page refresh.
  // Only list CHANGES update state (the playing video is not restarted when
  // nothing changed).
  const mediaJsonRef = useRef('')
  useEffect(() => {
    let cancelled = false
    const load = async () => {
      try {
        const base = bridgeBase()
        const [bg, task, avatar] = await Promise.all([
          fetch(`${base}/api/media/bg-images?character=${encodeURIComponent(characterId)}`).then((r) => r.json() as Promise<{ media: { name: string; type: string }[] }>),
          fetch(`${base}/api/media/task-videos?character=${encodeURIComponent(characterId)}`).then((r) => r.json() as Promise<{ videos: string[] }>),
          fetch(`${base}/api/avatar/${encodeURIComponent(characterId)}/idle`)
            .then((r) => r.ok ? r.json() as Promise<{ media: { name: string; type: string; state?: string }[] }> : { media: [] })
            .catch(() => ({ media: [] as { name: string; type: string; state?: string }[] })),
        ])
        if (cancelled) return
        const json = JSON.stringify([characterId, bg.media, task.videos, avatar.media])
        if (json === mediaJsonRef.current) return
        mediaJsonRef.current = json
        const bridgeBg = bg.media.filter((m) => m.type === 'video').map((m) => `${base}/media/bg-images/${encodeURIComponent(m.name)}`)
        const migratedBg = avatar.media.filter((m) => m.type === 'video').map((m) => `${base}/media/avatars/${encodeURIComponent(characterId)}/${encodeURIComponent(m.state ?? 'idle')}/${encodeURIComponent(m.name)}`)
        // Xiaoman's manifest-backed asset namespace wins over legacy default
        // media; default keeps the existing bridge list unchanged.
        setBgVideos(characterId === 'xiaoman' && migratedBg.length > 0 ? migratedBg : bridgeBg.length > 0 ? bridgeBg : migratedBg)
        setTaskVideos(characterId === 'xiaoman' ? [] : task.videos.map((name) => `${base}/media/task-videos/${encodeURIComponent(name)}`))
      } catch (err) {
        console.error('[ui-voice] companion media list failed:', err)
      }
    }
    void load()
    const timer = window.setInterval(load, 30000)
    return () => {
      cancelled = true
      window.clearInterval(timer)
    }
  }, [characterId])

  // Follow the speaker's speaking state.
  useEffect(() => speaker.onSpeakingChange(() => setSpeaking(speaker.speaking)), [speaker])

  useEffect(() => companionState.onStateChange(setLifecycleState), [companionState])

  // Xiaoman uses LiveTalking's WebRTC output when available.  The ordinary
  // idle clip remains underneath as a fail-soft fallback while the model is
  // loading, reconnecting, or unavailable.
  useEffect(() => {
    const video = avatarRef.current
    if (!visible || characterId !== 'xiaoman' || sessionId === undefined || video === null) {
      setAvatarConnected(false)
      setRemoteAudio(false)
      avatarAudio.setRemote(false)
      return
    }
    const controller = new AbortController()
    let connection: AvatarConnection | undefined
    let cancelled = false
    const enableRemoteAudio = async (): Promise<void> => {
      const current = connection
      if (current === undefined || cancelled) return
      const enabled = await current.enableAudio()
      if (cancelled || connection !== current) return
      setRemoteAudio(enabled)
      avatarAudio.setRemote(enabled)
    }
    const onGesture = () => { void enableRemoteAudio() }
    window.addEventListener('pointerdown', onGesture, { capture: true })
    window.addEventListener('keydown', onGesture, { capture: true })
    const run = async () => {
      while (!cancelled) {
        try {
          setRemoteAudio(false)
          avatarAudio.setRemote(false)
          connection = await connectAvatar(video, String(sessionId), controller.signal, () => {
            if (!cancelled) setAvatarConnected(true)
          }, () => { void enableRemoteAudio() })
          await enableRemoteAudio()
          let previousTime = video.currentTime
          while (!cancelled) {
            await new Promise(resolve => window.setTimeout(resolve, AVATAR_STALL_PROBE_MS))
            if (cancelled || controller.signal.aborted) return
            const currentTime = video.currentTime
            // Background tabs may throttle media callbacks. Reset the probe
            // baseline there instead of churning a healthy peer connection.
            if (document.visibilityState !== 'visible') {
              previousTime = currentTime
              continue
            }
            if (!avatarPlaybackIsStalled(previousTime, currentTime, video.paused, video.readyState)) {
              previousTime = currentTime
              continue
            }
            setAvatarConnected(false)
            setRemoteAudio(false)
            avatarAudio.setRemote(false)
            await connection.close()
            connection = undefined
            break
          }
        } catch (error) {
          if (cancelled || controller.signal.aborted) return
          console.warn('[ui-voice] Avatar unavailable; retrying:', error)
        }
        if (!cancelled) await new Promise(resolve => window.setTimeout(resolve, 1000))
      }
    }
    setAvatarConnected(false)
    void run()
    return () => {
      cancelled = true
      controller.abort()
      window.removeEventListener('pointerdown', onGesture, { capture: true })
      window.removeEventListener('keydown', onGesture, { capture: true })
      setAvatarConnected(false)
      setRemoteAudio(false)
      avatarAudio.setRemote(false)
      void connection?.close()
    }
  }, [avatarAudio, bgVideos.length, characterId, sessionId, visible])

  // If the bridge has no state-specific speaking clip, keep verified idle
  // media visible. The lifecycle state is still observable for future assets.
  const speakingWithFallback = speaking || lifecycleState === 'SPEAKING'

  // Idle layer: play bgVideos[bgIndex]; advance on ended.
  useEffect(() => {
    const vid = idleRef.current
    const src = bgVideos[bgIndex % bgVideos.length]
    if (vid === null || src === undefined) return
    vid.src = src
    void vid.play().catch(() => {})
  }, [bgIndex, bgVideos])

  // Rotate the speaking clip once per new reply (each speaking start).
  const wasSpeakingRef = useRef(false)
  useEffect(() => {
    if (speakingWithFallback && !wasSpeakingRef.current && taskVideos.length > 0) {
      setTaskIndex((i) => (i + 1) % taskVideos.length)
    }
    wasSpeakingRef.current = speakingWithFallback
  }, [speakingWithFallback, taskVideos.length])

  // Speaking layer: play taskVideos[taskIndex] while speaking; stop otherwise.
  useEffect(() => {
    const vid = speakRef.current
    const src = taskVideos[taskIndex % taskVideos.length]
    if (vid === null || src === undefined) return
    if (speakingWithFallback) {
      vid.src = src
      void vid.play().catch(() => {})
    } else {
      vid.pause()
      vid.currentTime = 0
    }
  }, [speakingWithFallback, taskIndex, taskVideos])

  const onIdleEnded = useCallback(() => {
    if (bgVideos.length > 1) setBgIndex((i) => (i + 1) % bgVideos.length)
  }, [bgVideos.length])

  const onSpeakEnded = useCallback(() => {
    // Keep looping the speaking clip while the reply is still playing.
    const vid = speakRef.current
    if (vid !== null && speakingWithFallback) {
      vid.currentTime = 0
      void vid.play().catch(() => {})
    }
  }, [speakingWithFallback])

  // Drag: resize on move (persist the live value), flip side on double-click.
  const beginDrag = useCallback((clientX: number) => {
    dragRef.current = { startX: clientX, startWidth: widthVw, current: widthVw }
    const onMove = (move: PointerEvent) => {
      const drag = dragRef.current
      if (drag === null) return
      const deltaVw = ((move.clientX - drag.startX) / window.innerWidth) * 100
      drag.current = Math.min(MAX_WIDTH_VW, Math.max(MIN_WIDTH_VW, drag.startWidth + (side === 'right' ? -deltaVw : deltaVw)))
      setWidthVw(drag.current)
    }
    const onUp = () => {
      const drag = dragRef.current
      dragRef.current = null
      window.removeEventListener('pointermove', onMove)
      window.removeEventListener('pointerup', onUp)
      if (drag !== null) {
        try {
          localStorage.setItem(WIDTH_KEY, String(drag.current))
        } catch {
          // ignore
        }
      }
    }
    window.addEventListener('pointermove', onMove)
    window.addEventListener('pointerup', onUp)
  }, [widthVw, side])

  const flipSide = useCallback(() => {
    setSide((previous) => {
      const next = previous === 'right' ? 'left' : 'right'
      try {
        localStorage.setItem(SIDE_KEY, next)
      } catch {
        // ignore
      }
      return next
    })
  }, [])

  if (!visible || (bgVideos.length === 0 && taskVideos.length === 0)) return null

  return (
    <div
      className={side === 'right' ? css.companion : `${css.companion} ${css.left}`}
      style={{ width: `${widthVw}vw`, right: side === 'right' ? 0 : undefined, left: side === 'left' ? 0 : undefined }}
      aria-hidden="true"
    >
      {bgVideos.length > 0 && (
        <video ref={idleRef} className={`${css.video}${speakingWithFallback ? '' : ` ${css.idleMotion}`}${avatarConnected || (speakingWithFallback && taskVideos.length > 0) ? ` ${css.hidden}` : ''}`} muted playsInline preload="auto" loop={shouldLoopIdleVideo(bgVideos.length)} onEnded={onIdleEnded} />
      )}
      {taskVideos.length > 0 && (
        <video ref={speakRef} className={!avatarConnected && speakingWithFallback ? css.video : `${css.video} ${css.hidden}`} muted playsInline preload="auto" onEnded={onSpeakEnded} />
      )}
      {characterId === 'xiaoman' && (
        <video ref={avatarRef} className={`${css.video}${avatarConnected ? '' : ` ${css.hidden}`}${speakingWithFallback ? '' : ` ${css.idleMotion}`}`} muted={!remoteAudio} playsInline autoPlay />
      )}
      <div
        className={css.handle}
        onPointerDown={(event) => {
          event.preventDefault()
          beginDrag(event.clientX)
        }}
        onDoubleClick={flipSide}
        title="拖动调宽,双击换边"
      />
    </div>
  )
})
