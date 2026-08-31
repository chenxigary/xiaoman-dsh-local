/**
 * Browser-side latency events for the voice turn.
 *
 * Events are deliberately console-only and contain no audio or user text.
 * Set localStorage `s2s.voice.latency` to `0` to disable them (the bridge has
 * its own `latency.enabled` switch).  Keeping this in a tiny module means the
 * UI can be instrumented without adding a telemetry service or changing the
 * DSH session contract.
 */

const LATENCY_KEY = 's2s.voice.latency'

export function latencyEnabled(): boolean {
  try {
    return localStorage.getItem(LATENCY_KEY) !== '0'
  } catch {
    return true
  }
}

export function newTraceId(): string {
  try {
    const randomUuid = (globalThis.crypto as { randomUUID?: () => string } | undefined)?.randomUUID
    if (typeof randomUuid === 'function') return randomUuid.call(globalThis.crypto)
  } catch {
    // Older embedded browsers may not expose randomUUID.
  }
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`
}

export function monotonicNow(): number {
  return typeof performance !== 'undefined' ? performance.now() : Date.now()
}

/** Emit one structured event as a single console record. */
export function latencyEvent(
  stage: string,
  fields: Record<string, string | number | boolean | undefined> = {},
): void {
  if (!latencyEnabled()) return
  const payload = {
    event: 'voice.latency',
    source: 'ui-voice',
    stage,
    timestamp: new Date().toISOString(),
    ...fields,
  }
  // console.debug keeps normal DSH logs readable while still being visible in
  // browser devtools and capturable by automation.
  console.debug(`[ui-voice] ${JSON.stringify(payload)}`)
}
