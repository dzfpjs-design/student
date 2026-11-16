// @vitest-environment node
import { expect, it, vi } from 'vitest'
import type { NativeRealtimeForward } from '../../shared/nativeRealtimeCapability'
import { createNativeRuntimeBridge } from './nativeRuntimeBridge'
import { QwenNativeConversation } from './qwenNativeConversation'

// This exercises the real native source admission and bridge, with a synthetic
// host receipt. It does not claim a cloud model, microphone or device test.
it('keeps a spoken request bound through native admission, a learning tool and dialogue', async () => {
  const catalog = { version: 1 as const, protocol: 'qwen_runtime_capability_v1' as const, capabilities: [{
    name: 'learning.prepare_request', tool_name: 'learning_prepare_request', description: 'Prepare learning.',
    required_arguments: ['subject'], arguments_schema: { type: 'object', additionalProperties: false,
      required: ['subject'], properties: { subject: { type: 'string' } } },
  }] }
  const forward = vi.fn(async (request: NativeRealtimeForward) => {
    if (request.operation === 'input') return { voice_input_id: 'voice-original' }
    if (request.operation === 'proposal') return { version: 1, proposal_status: 'admitted', action_id: 'action-original',
      execution: { status: 'completed', location: 'server' }, public_result: { status: 'saved' } }
    if (request.operation === 'dialogue') return {}
    throw Error('UNEXPECTED_SYNTHETIC_OPERATION')
  })
  let current = true
  const bridge = createNativeRuntimeBridge({ sessionId: 'session-original', childId: 'child-original', current: () => current, forward })
  const ledger = new QwenNativeConversation('supplier-original', { ...bridge, catalog },
    capture => current && capture.sessionId === 'session-original' && capture.reference.child_id === 'child-original', 'host-original')
  const capture = { sessionId: 'session-original', reference: { child_id: 'child-original' },
    carryoverConfirmationId: null, localInputId: 'input-original', capturedAt: 123, source: 'accepted_onset' as const }
  expect(ledger.startInput('item-original', capture)).toBe(true)
  await ledger.completeInput('item-original', '帮我复习初三地理第一课')
  expect(ledger.bindResponse('response-original', 'item-original')).toBe(true)
  const result = await ledger.propose('response-original', 'call-original', 'learning_prepare_request',
    JSON.stringify({ args: { subject: 'geography' } }))
  expect(result.execution.status).toBe('completed')
  expect(ledger.appendDialogue('response-original', '先从第一课的地图开始。')).toBe(true)
  expect(await ledger.finishDialogue('response-original', 'completed')).toBe(true)
  expect(forward.mock.calls.map(([request]) => request.operation)).toEqual(['input', 'proposal', 'dialogue'])
  expect(forward.mock.calls[0][0]).toMatchObject({ session_id: 'session-original', host_connection_id: 'host-original',
    body: { local_input_id: 'input-original', capture_reference: { child_id: 'child-original' } } })
  expect(forward.mock.calls[1][0]).toMatchObject({ body: { voice_input_id: 'voice-original', input_item_id: 'item-original',
    response_id: 'response-original', call_id: 'call-original', capability_name: 'learning.prepare_request' } })
  expect(forward.mock.calls[2][0]).toMatchObject({ body: { voice_input_id: 'voice-original', status: 'unverified', terminal: 'completed' } })
  current = false
  await expect(bridge.admit('另一句', capture, 'host-original')).rejects.toThrow('OWNER_CHANGED')
  expect(forward).toHaveBeenCalledTimes(3)
})

const syntheticCatalog = { version: 1 as const, protocol: 'qwen_runtime_capability_v1' as const, capabilities: [{
  name: 'workspace.navigate', tool_name: 'workspace_navigate', description: 'Navigate.',
  required_arguments: [], arguments_schema: { type: 'object', properties: {} },
}] }

it('admits the full wake-prefixed original once with its onset page even if the live page changes', async () => {
  const catalog = syntheticCatalog
  const forward = vi.fn(async (request: NativeRealtimeForward) => {
    if (request.operation === 'input') return { voice_input_id: 'canonical-original' }
    throw Error('UNEXPECTED_SYNTHETIC_OPERATION')
  })
  const bridge = createNativeRuntimeBridge({ sessionId: 'session', childId: 'child', current: () => true, forward })
  const ledger = new QwenNativeConversation('supplier', { ...bridge, catalog }, () => true, 'host')
  const capture = { sessionId: 'session', reference: { child_id: 'child', question_reference: {
    kind: 'diagnostic' as const, binding: { prompt_id: 'original-page', item_version: 'v1' },
  } }, carryoverConfirmationId: null, localInputId: 'input', capturedAt: 123, source: 'accepted_onset' as const }
  expect(ledger.startInput('item', capture)).toBe(true)
  capture.reference.question_reference.binding.prompt_id = 'later-page'
  const first = ledger.completeInput('item', '小知，原题我选B。')
  const repeated = ledger.completeInput('item', '小知，原题我选B。')
  expect(repeated).toBe(first)
  expect(ledger.completeInput('item', '被替换的原话')).toBeNull()
  await first
  expect(forward).toHaveBeenCalledTimes(1)
  expect(forward.mock.calls[0][0]).toMatchObject({ operation: 'input', body: {
    transcript: '小知，原题我选B。', capture_reference: { question_reference: { binding: { prompt_id: 'original-page' } } },
  } })
})

it('rejects a late admission receipt after its owning native conversation is retired', async () => {
  const catalog = syntheticCatalog
  let settle!: (value: unknown) => void
  const forward = vi.fn(() => new Promise<unknown>(resolve => { settle = resolve }))
  const bridge = createNativeRuntimeBridge({ sessionId: 'session', childId: 'child', current: () => true, forward })
  const ledger = new QwenNativeConversation('supplier', { ...bridge, catalog }, () => true, 'host')
  const capture = { sessionId: 'session', reference: { child_id: 'child' }, carryoverConfirmationId: null,
    localInputId: 'input', capturedAt: 123, source: 'accepted_onset' as const }
  expect(ledger.startInput('item', capture)).toBe(true)
  const pending = ledger.completeInput('item', '原话')!
  const denied = expect(pending).rejects.toThrow('QWEN_NATIVE_INPUT_ADMISSION_UNCONFIRMED')
  await Promise.resolve(); await Promise.resolve()
  expect(forward).toHaveBeenCalledTimes(1)
  ledger.close()
  settle({ voice_input_id: 'canonical-original' })
  await denied
  expect(ledger.bindResponse('late-response', 'item')).toBe(false)
  expect(forward).toHaveBeenCalledTimes(1)
})
