/**
 * Companion state machine.  The state is event-driven but deliberately small:
 * media fallback remains owned by CompanionWindow.
 */

export type CompanionState = 'IDLE' | 'LISTENING' | 'THINKING' | 'SPEAKING' | 'INTERRUPTED'

export type CompanionEvent =
  | { type: 'listen_start' }
  | { type: 'thinking' }
  | { type: 'speech_start' }
  | { type: 'speech_end' }
  | { type: 'interrupted' }
  | { type: 'reset' }

export class CompanionStateMachine {
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

  subscribe(listener: (state: CompanionState) => void): () => void {
    this.listeners.add(listener)
    return () => { this.listeners.delete(listener) }
  }

  dispose(): void {
    this.listeners.clear()
  }
}
