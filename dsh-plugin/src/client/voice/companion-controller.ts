/** Renderer-only lifecycle port for the companion animation. */

import type { CompanionEvent, CompanionState } from '../companion-state.ts'

/** Narrow callbacks exposed to renderer components. */
export interface CompanionRendererPort {
  readonly state: CompanionState
  dispatch(event: CompanionEvent): CompanionState
  onStateChange(listener: (state: CompanionState) => void): () => void
}

/**
 * Small renderer hook source. Visibility is deliberately not kept here; the
 * session store owns that fact and components read it through `useStore`.
 */
export class CompanionRenderer implements CompanionRendererPort {
  private value: CompanionState = 'IDLE'
  private readonly listeners = new Set<(state: CompanionState) => void>()

  get state(): CompanionState {
    return this.value
  }

  dispatch(event: CompanionEvent): CompanionState {
    const previous = this.value
    switch (event.type) {
      case 'listen_start': this.value = 'LISTENING'; break
      case 'thinking': this.value = 'THINKING'; break
      case 'speech_start': this.value = 'SPEAKING'; break
      case 'speech_end': this.value = 'IDLE'; break
      case 'interrupted': this.value = 'INTERRUPTED'; break
      case 'reset': this.value = 'IDLE'; break
    }
    if (this.value !== previous) {
      for (const listener of this.listeners) listener(this.value)
    }
    return this.value
  }

  onStateChange(listener: (state: CompanionState) => void): () => void {
    this.listeners.add(listener)
    return () => { this.listeners.delete(listener) }
  }

  dispose(): void {
    this.listeners.clear()
  }
}
