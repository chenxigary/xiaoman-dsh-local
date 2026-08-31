import { bridgeBase } from '../bridge.ts'

const DEFAULT_AVATAR = 'http://127.0.0.1:8010'
const AVATAR_KEY = 's2s.voice.avatar'

export interface AvatarConnection {
  readonly avatarSessionId: string
  /** Attempt to make the combined remote A/V stream the audible authority. */
  enableAudio: () => Promise<boolean>
  close: () => Promise<void>
}

export function isAllowedAvatarBase(value: unknown): value is string {
  if (typeof value !== 'string' || value.length > 256) return false
  try {
    const url = new URL(value)
    return url.protocol === 'http:'
      && ['127.0.0.1', 'localhost', '[::1]'].includes(url.hostname)
      && url.port !== ''
      && (url.pathname === '' || url.pathname === '/')
      && url.search === ''
      && url.hash === ''
      && url.username === ''
      && url.password === ''
  } catch {
    return false
  }
}

export function avatarBase(): string {
  try {
    const configured = localStorage.getItem(AVATAR_KEY)?.trim()
    if (configured !== undefined && isAllowedAvatarBase(configured)) return new URL(configured).origin
  } catch {
    // Browser privacy settings can deny localStorage; use the fixed default.
  }
  return DEFAULT_AVATAR
}

async function waitForIce(pc: RTCPeerConnection, signal: AbortSignal): Promise<void> {
  if (pc.iceGatheringState === 'complete') return
  await new Promise<void>((resolve, reject) => {
    const timeout = window.setTimeout(() => finish(new Error('Avatar WebRTC ICE gathering timed out')), 5000)
    const onAbort = () => finish(new DOMException('Aborted', 'AbortError'))
    const onChange = () => {
      if (pc.iceGatheringState === 'complete') finish()
    }
    const finish = (error?: Error) => {
      window.clearTimeout(timeout)
      pc.removeEventListener('icegatheringstatechange', onChange)
      signal.removeEventListener('abort', onAbort)
      if (error === undefined) resolve()
      else reject(error)
    }
    pc.addEventListener('icegatheringstatechange', onChange)
    signal.addEventListener('abort', onAbort, { once: true })
  })
}

export function avatarRegistrationRequestInit(
  method: 'PUT' | 'DELETE',
  dshSessionId: string,
  avatarSessionId: string,
): RequestInit {
  return {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ dsh_session_id: dshSessionId, avatar_session_id: avatarSessionId }),
    // React cleanup runs during page teardown.  A normal fetch is cancelled by
    // navigation/close and leaves a stale Runtime binding; keepalive lets this
    // small compare-and-delete request finish after the document is gone.
    keepalive: method === 'DELETE',
  }
}

async function updateRegistration(method: 'PUT' | 'DELETE', dshSessionId: string, avatarSessionId: string): Promise<void> {
  const response = await fetch(
    `${bridgeBase()}/api/avatar/session`,
    avatarRegistrationRequestInit(method, dshSessionId, avatarSessionId),
  )
  if (!response.ok) throw new Error(`Avatar session registration failed: ${response.status}`)
}

/** Establish a local LiveTalking receive-only session and register its WAV sink. */
export async function connectAvatar(
  video: HTMLVideoElement,
  dshSessionId: string,
  signal: AbortSignal,
  onConnected: () => void,
  onAudioAvailable?: (() => void) | undefined,
): Promise<AvatarConnection> {
  const id = dshSessionId.trim()
  if (id === '' || id.length > 256) throw new Error('Avatar requires a bounded DSH session id')
  const pc = new RTCPeerConnection()
  let avatarSessionId = ''
  let closed = false
  let audioAvailable = false
  const remote = new MediaStream()

  const enableAudio = async (): Promise<boolean> => {
    if (closed || !audioAvailable) return false
    video.muted = false
    try {
      await video.play()
      return !video.muted && !video.paused
    } catch {
      video.muted = true
      return false
    }
  }

  const close = async (): Promise<void> => {
    if (closed) return
    closed = true
    signal.removeEventListener('abort', onAbort)
    window.removeEventListener('pagehide', onPageHide)
    pc.close()
    if (video.srcObject instanceof MediaStream) {
      for (const track of video.srcObject.getTracks()) track.stop()
    }
    video.srcObject = null
    if (avatarSessionId !== '') {
      await updateRegistration('DELETE', id, avatarSessionId).catch(() => {})
    }
  }

  const onAbort = () => { void close() }
  const onPageHide = () => { void close() }
  signal.addEventListener('abort', onAbort, { once: true })
  // Closing or navigating a tab does not unmount React reliably.  pagehide is
  // the document-lifecycle signal that initiates the keepalive DELETE above.
  window.addEventListener('pagehide', onPageHide, { once: true })
  try {
    pc.addTransceiver('video', { direction: 'recvonly' })
    pc.addTransceiver('audio', { direction: 'recvonly' })
    pc.addEventListener('track', (event) => {
      if (closed) return
      if (!remote.getTracks().some(track => track.id === event.track.id)) remote.addTrack(event.track)
      video.srcObject = remote
      if (event.track.kind === 'audio') {
        audioAvailable = true
        onAudioAvailable?.()
      }
      if (event.track.kind === 'video') void video.play().then(onConnected).catch(() => {})
    })
    const offer = await pc.createOffer()
    await pc.setLocalDescription(offer)
    await waitForIce(pc, signal)
    if (signal.aborted) throw new DOMException('Aborted', 'AbortError')
    const local = pc.localDescription
    if (local === null) throw new Error('Avatar WebRTC local description is missing')
    const response = await fetch(`${avatarBase()}/offer`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ sdp: local.sdp, type: local.type, avatar: 'xiaoman-v3-original-idle' }),
      signal,
    })
    if (!response.ok) throw new Error(`Avatar WebRTC offer failed: ${response.status}`)
    const answer = await response.json() as { sdp?: unknown; type?: unknown; sessionid?: unknown; code?: unknown; msg?: unknown }
    if (answer.code !== undefined && answer.code !== 0) throw new Error(typeof answer.msg === 'string' ? answer.msg : 'Avatar rejected WebRTC offer')
    if (typeof answer.sdp !== 'string' || answer.sdp.length > 1_000_000 || answer.type !== 'answer') {
      throw new Error('Avatar WebRTC answer is invalid')
    }
    if (typeof answer.sessionid !== 'string' && typeof answer.sessionid !== 'number') {
      throw new Error('Avatar WebRTC session id is missing')
    }
    avatarSessionId = String(answer.sessionid)
    if (!/^[A-Za-z0-9_-]{1,64}$/.test(avatarSessionId)) throw new Error('Avatar WebRTC session id is invalid')
    await pc.setRemoteDescription({ type: 'answer', sdp: answer.sdp })
    await updateRegistration('PUT', id, avatarSessionId)
    return { avatarSessionId, enableAudio, close }
  } catch (error) {
    await close()
    throw error
  }
}
