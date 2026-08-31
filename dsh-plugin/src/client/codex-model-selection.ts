/** Pure Codex catalog selection policy used by the resident model seat. */

import type { CodexModelCatalogResult, CodexModelOption, CodexModelSelection } from '../types.ts'

export const LOW_USAGE_CODEX_MODEL = 'gpt-5.4-mini'
export const LOW_USAGE_CODEX_EFFORT = 'low'

function modelFor(catalog: CodexModelCatalogResult, id: string): CodexModelOption | undefined {
  return catalog.models.find(model => model.id === id)
}

function lowestUsageModel(catalog: CodexModelCatalogResult): CodexModelOption {
  return modelFor(catalog, LOW_USAGE_CODEX_MODEL)
    ?? catalog.models.find(model => /cost-efficient|affordable/i.test(model.description))
    ?? catalog.models[0]!
}

function lowestEffort(model: CodexModelOption): string {
  return model.supportedReasoningEfforts.some(option => option.id === LOW_USAGE_CODEX_EFFORT)
    ? LOW_USAGE_CODEX_EFFORT
    : model.defaultReasoningEffort
}

/** Cheapest known catalog model + lightest advertised effort + ordinary tier. */
export function lowestUsageSelection(catalog: CodexModelCatalogResult): CodexModelSelection {
  const model = lowestUsageModel(catalog)
  return { model: model.id, reasoningEffort: lowestEffort(model), serviceTier: null }
}

/** Preserve a live choice only while every dimension remains catalog-valid. */
export function reconcileCodexSelection(
  catalog: CodexModelCatalogResult,
  current: CodexModelSelection | undefined,
): CodexModelSelection {
  if (current === undefined) return lowestUsageSelection(catalog)
  const model = modelFor(catalog, current.model)
  if (model === undefined) return lowestUsageSelection(catalog)
  const reasoningEffort = model.supportedReasoningEfforts.some(option => option.id === current.reasoningEffort)
    ? current.reasoningEffort
    : lowestEffort(model)
  const serviceTier = current.serviceTier !== null
    && model.serviceTiers.some(option => option.id === current.serviceTier)
    ? current.serviceTier
    : null
  return { model: model.id, reasoningEffort, serviceTier }
}

/** Changing model resets effort/tier to its lowest-usage posture. */
export function selectionForModel(model: CodexModelOption): CodexModelSelection {
  return { model: model.id, reasoningEffort: lowestEffort(model), serviceTier: null }
}
