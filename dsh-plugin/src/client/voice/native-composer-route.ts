/** Route the resident DSH composer without replacing any of its UI. */

export interface ComposerInputSnapshot {
  readonly draft: string
  readonly imageIds: readonly unknown[]
}

export interface RoutableComposerInput {
  readonly state: { getSnapshot(): ComposerInputSnapshot }
  submit(mode?: unknown): void
  setDraft(text: string): void
  notify(level: 'info' | 'error', text: string): void
  /** Present on the resident DSH input shell; kept optional at the public seam. */
  commitSend?(imageIds: readonly unknown[]): void
}

export type ComposerSubmitRoute = (input: RoutableComposerInput, mode: unknown) => boolean

/**
 * Install one reversible submit router on the existing input-machine shell.
 * Returning false delegates to DSH unchanged; true means the alternate
 * backend accepted ownership of this submit gesture.
 */
export function installComposerSubmitRoute(
  input: RoutableComposerInput,
  route: ComposerSubmitRoute,
): () => void {
  const original = input.submit
  const routed = function routedSubmit(this: RoutableComposerInput, mode?: unknown): void {
    if (!route(input, mode)) original.call(input, mode)
  }
  input.submit = routed
  return () => {
    if (input.submit === routed) input.submit = original
  }
}
