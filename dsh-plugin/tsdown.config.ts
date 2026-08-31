import { clientBundle } from '../tsdown.client.ts'

/** Ordinary Client package; Host Codex ownership is a separate package. */
export default clientBundle(
  '@deepseek-ai/dsh-client-ui-voice',
  ['lib/types/index.js', 'lib/types/invariant.js'],
)
