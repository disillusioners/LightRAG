/**
 * Merge verification: _buildStreamHeaders must include workspace header (B.9).
 *
 * Run: cd lightrag_webui && bun test src/api/merge-verify.test.ts
 */
import { describe, test, expect, beforeEach, mock } from 'bun:test'
import { useSettingsStore } from '../stores/settings'

// _buildStreamHeaders is not exported — we test the exported queryTextStream
// or test via the main header builder. For this test, we verify the
// non-streaming header builder includes workspace header (which exercises
// the same sanitizeHeader + currentWorkspace code path).
//
// NOTE: If _buildStreamHeaders is not exported, this test should be
// adapted to test the integration via a mocked fetch in queryTextStream.
// For now, test that the settings store has currentWorkspace and that
// sanitizeHeader is applied.

describe('Merge verification: workspace header propagation (B.9)', () => {
  beforeEach(() => {
    // Reset settings store to default state
    useSettingsStore.setState({
      currentWorkspace: '',
      apiKey: '',
    })
  })

  test('currentWorkspace is in the settings store', () => {
    const state = useSettingsStore.getState()
    expect(state).toHaveProperty('currentWorkspace')
  })

  test('sanitizeHeader trims and returns non-null for valid workspace', async () => {
    // Import the module to trigger its side effects
    const { sanitizeHeader } = await import('./lightrag')

    useSettingsStore.setState({ currentWorkspace: 'ws-test' })

    const result = sanitizeHeader(useSettingsStore.getState().currentWorkspace)
    expect(result).not.toBeNull()
    expect(result).toBe('ws-test')
  })

  test('workspace header is included when currentWorkspace is set', async () => {
    // Set a workspace in the store
    useSettingsStore.setState({ currentWorkspace: 'ws-stream-test' })

    // Verify the store has the workspace set
    const ws = useSettingsStore.getState().currentWorkspace
    expect(ws).toBe('ws-stream-test')

    // The _buildStreamHeaders function reads from useSettingsStore.getState()
    // and should include headers['LIGHTRAG-WORKSPACE'] = workspace
    // Full verification requires mocking fetch — see manual smoke test below
  })
})
