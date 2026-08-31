import assert from 'node:assert/strict'
import test from 'node:test'
import type { CodexModelCatalogResult } from '../src/types.ts'
import {
  lowestUsageSelection,
  reconcileCodexSelection,
  selectionForModel,
} from '../src/client/codex-model-selection.ts'

const catalog: CodexModelCatalogResult = {
  models: [
    {
      id: 'gpt-5.6-sol', displayName: 'Sol', description: 'Frontier', defaultReasoningEffort: 'low',
      supportedReasoningEfforts: [{ id: 'low', description: 'Low' }, { id: 'high', description: 'High' }],
      serviceTiers: [{ id: 'priority', name: 'Fast', description: 'Increased usage' }],
    },
    {
      id: 'gpt-5.4-mini', displayName: 'Mini', description: 'Cost-efficient', defaultReasoningEffort: 'medium',
      supportedReasoningEfforts: [{ id: 'low', description: 'Low' }, { id: 'medium', description: 'Medium' }],
      serviceTiers: [],
    },
  ],
}

test('lowest usage defaults to mini + low + ordinary tier', () => {
  assert.deepEqual(lowestUsageSelection(catalog), {
    model: 'gpt-5.4-mini', reasoningEffort: 'low', serviceTier: null,
  })
})

test('model changes reset effort and Fast', () => {
  assert.deepEqual(selectionForModel(catalog.models[0]!), {
    model: 'gpt-5.6-sol', reasoningEffort: 'low', serviceTier: null,
  })
})

test('catalog reconciliation rejects stale dimensions', () => {
  assert.deepEqual(reconcileCodexSelection(catalog, {
    model: 'gpt-5.4-mini', reasoningEffort: 'ultra', serviceTier: 'priority',
  }), {
    model: 'gpt-5.4-mini', reasoningEffort: 'low', serviceTier: null,
  })
})
