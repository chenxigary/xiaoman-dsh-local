/**
 * Sentence splitting for streaming TTS (mirrors the original backend's
 * LMOutputProcessor: the reply is spoken sentence-by-sentence, so synthesis
 * pipelines with playback instead of waiting for the whole text).
 *
 * Chinese/English terminal punctuation: 。！？!?… (plus a closing quote/bracket
 * immediately after the terminator is absorbed into the sentence). The
 * trailing unterminated chunk is returned separately as `partial` — it is not
 * spoken until a terminator completes it.
 */

const TERMINATORS = new Set(['。', '！', '？', '!', '?', '…'])
export const MAX_SENTENCE_CHARS = 512

/** True when the sentence is trivial noise (only punctuation/whitespace, or
 *  a single character) — split fragments from JSON/fence content during
 *  streaming ("！" "？" "…" standalone) must never reach the TTS. */
function isTrivial(sentence: string): boolean {
  if (sentence.length <= 1) return true
  // No CJK, no ASCII alphanumerics => punctuation/whitespace only.
  return !/[\u4e00-\u9fff0-9a-zA-Z]/.test(sentence)
}

export interface SentenceSplit {
  /** Complete sentences, each ending with a terminator (trimmed). */
  sentences: string[]
  /** The trailing chunk without a terminator, or null when the text ends on a boundary. */
  partial: string | null
}

/** Split a complete or settled trailing fragment at the bridge TTS boundary. */
export function boundSpeechText(text: string, maxChars = MAX_SENTENCE_CHARS): string[] {
  const value = text.trim()
  if (value === '') return []
  const safeMax = Number.isFinite(maxChars) ? Math.max(1, Math.floor(maxChars)) : MAX_SENTENCE_CHARS
  const chunks: string[] = []
  let rest = value
  while (rest.length > safeMax) {
    let cut = safeMax
    for (let index = safeMax; index > Math.floor(safeMax / 2); index -= 1) {
      if (/\s/u.test(rest[index - 1] ?? '')) {
        cut = index
        break
      }
    }
    chunks.push(rest.slice(0, cut).trim())
    rest = rest.slice(cut).trim()
  }
  if (rest !== '') chunks.push(rest)
  return chunks
}

/** Split text into complete sentences + a trailing partial chunk. */
export function splitSentences(text: string): SentenceSplit {
  const sentences: string[] = []
  let buf = ''
  for (const ch of text) {
    buf += ch
    if (TERMINATORS.has(ch)) {
      const trimmed = buf.trim()
      if (trimmed !== '' && !isTrivial(trimmed)) sentences.push(...boundSpeechText(trimmed))
      buf = ''
    }
  }
  const partial = buf.trim()
  return { sentences, partial: partial === '' || isTrivial(partial) ? null : partial }
}
