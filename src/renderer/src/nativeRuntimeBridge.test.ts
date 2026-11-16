import { describe, expect, it, vi } from 'vitest'
import { createNativeRuntimeBridge } from './nativeRuntimeBridge'
import type { QwenRealtimeInputCapture } from './qwenRealtimeVoice'
import type { QwenNativeCapabilityProposal } from './qwenNativeProtocol'
const capture: QwenRealtimeInputCapture = { localInputId: 'input-public', sessionId: 'session-public', capturedAt: 12345, source: 'accepted_onset',
  reference: { child_id: 'child-public' }, carryoverConfirmationId: null }
const proposal: QwenNativeCapabilityProposal = { version: 1, host_connection_id: 'host-public', voice_input_id: 'voice-public',
  supplier_session_id: 'supplier-public', input_item_id: 'item-public', response_id: 'response-public',
  call_id: 'call-public', capability_name: 'learning.prepare_request', args: { subject: '地理' } }
const completed = { version: 1, proposal_status: 'admitted', action_id: 'action-public', execution: { status: 'completed', location: 'server' }, public_result: { request_saved: true } }
describe('native runtime workspace bridge', () => {
  it('saves arbitrary current speech through the signed native input path, without the legacy classifier', async () => {
    const forward = vi.fn().mockResolvedValue({ voice_input_id: 'voice-public' })
    const bridge = createNativeRuntimeBridge({ sessionId: 'session-public', childId: 'child-public', current: () => true, forward })
    await expect(bridge.admit('帮我复习初三地理第一课', capture, 'host-public')).resolves.toEqual({ voice_input_id: 'voice-public' })
    expect(forward).toHaveBeenCalledExactlyOnceWith({ session_id: 'session-public', host_connection_id: 'host-public', operation: 'input',
      body: { version: 1, local_input_id: 'input-public', transcript: '帮我复习初三地理第一课', captured_at_ms: 12345, capture_reference: capture.reference } })
    await expect(bridge.admit('继续说', { ...capture, reference: { child_id: 'child-public', local_recovery_only: true } }, 'host-public')).rejects.toThrow('SOURCE_INVALID')
    await expect(bridge.admit('继续说', { ...capture, source: 'unbound' }, 'host-public')).rejects.toThrow('SOURCE_INVALID')
    expect(forward).toHaveBeenCalledTimes(1)
  })
  it('does not lose a real execution receipt when refreshing the visible workspace fails', async () => {
    const forward = vi.fn().mockResolvedValue(completed)
    const onReceipt = vi.fn().mockRejectedValue(Error('screen refresh failed'))
    const bridge = createNativeRuntimeBridge({ sessionId: 'session-public', childId: 'child-public', current: () => true, forward, onReceipt })
    await expect(bridge.propose(proposal)).resolves.toEqual(completed)
    expect(forward).toHaveBeenCalledTimes(1)
    expect(onReceipt).toHaveBeenCalledTimes(1)
    await bridge.readReceipt(proposal)
    expect(onReceipt).toHaveBeenCalledTimes(1)
    expect(forward.mock.calls[1][0].operation).toBe('receipt')
  })
  it('refuses old-owner results after a child switch instead of exposing them to the new cloud session', async () => {
    let current = true
    const forward = vi.fn(async () => { current = false; return completed })
    const bridge = createNativeRuntimeBridge({ sessionId: 'session-public', childId: 'child-public', current: () => current, forward })
    await expect(bridge.propose(proposal)).rejects.toThrow('OWNER_CHANGED')
    await expect(bridge.propose(proposal)).rejects.toThrow('OWNER_CHANGED')
    expect(forward).toHaveBeenCalledTimes(1)
  })
})


it('keeps a validated local safety receipt before any native response can be requested', async () => {
 const control={kind:'child_safety' as const,intent:'urgent_safety_support' as const,response_text:'请先去找身边可信任的大人。',agent_turn_id:'turn-public',safety_event_id:'safety-public'}
 const forward=vi.fn().mockResolvedValue({voice_input_id:'voice-public',local_control:control})
 const bridge=createNativeRuntimeBridge({sessionId:'session-public',childId:'child-public',current:()=>true,forward})
 await expect(bridge.admit('公开合成安全测试',capture,'host-public')).resolves.toEqual({voice_input_id:'voice-public',local_control:control})
 forward.mockResolvedValueOnce({voice_input_id:'voice-public',local_control:{...control,intent:'unknown'}})
 await expect(bridge.admit('公开合成测试',capture,'host-public')).rejects.toThrow('LOCAL_CONTROL_UNCONFIRMED')
})
