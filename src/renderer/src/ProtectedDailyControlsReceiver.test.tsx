import { beforeEach as beforeOwnedVoiceRuntimePolicyCase } from 'vitest'
import { ownedVoiceRuntimePolicyDesktop } from './test/ownedVoiceRuntimePolicy'

// This test host explicitly admits original epoch-bound capture policy.
beforeOwnedVoiceRuntimePolicyCase(() => { vi.stubGlobal('desktop', ownedVoiceRuntimePolicyDesktop()) })

import { act, cleanup, render } from '@testing-library/react'
import type { ComponentProps } from 'react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { localApi } from './api'
import { SessionWorkspace } from './SessionWorkspace'
import * as inputs from './LocalInputs'
import { clearVoiceCaptureContext, setVoiceQuestionCapture, voiceCaptureReference } from './voiceCaptureContext'
import type { VoiceQuestionReference } from './voiceCaptureContext'
import type { AgentTurn, SessionWorkspaceState } from './types'
import wake from './fixtures/protected-controls/pending_question-voice.configure_wake_word.json'
import device from './fixtures/protected-controls/pending_question-device.status.json'
import parent from './fixtures/protected-controls/pending_question-parent.request.json'

let microphone: ComponentProps<typeof inputs.HandsFreeVoiceInput>
vi.mock('./LocalInputs', async original => ({ ...await original<typeof import('./LocalInputs')>(),
  HandsFreeVoiceInput: (props: typeof microphone) => { microphone = props; return null }, VoiceInputButton: () => null }))
vi.mock('./VoiceCalibrationPanel', () => ({ VoiceCalibrationPanel: () => null }))
let savedName = '小知'
const saveName = vi.fn(async (value: DesktopWakeWordSettings) => { savedName = value.wakeWord; return value })

beforeEach(() => {
  savedName = '小知'; saveName.mockClear()
  vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('NETWORK_FORBIDDEN')))
  for (const method of ['readVoiceInteractionHandoff', 'invalidateVoiceInteractionHandoff'] as const) {
    vi.spyOn(localApi, method).mockImplementation(async (session_id, child_id) => ({ session_id, child_id, status: 'none',
      handoff_id: null, agent_action_id: null, deferred_progress: null, request: null, receipt: null, reason: null }))
  }
  vi.stubGlobal('desktop', ownedVoiceRuntimePolicyDesktop({
    getWakeWordSettings: vi.fn(async (childId: string) => ({ childId, wakeWord: savedName, enabled: true, inactivitySeconds: 60 })),
    getVoiceOnboarding: vi.fn(async (childId: string) => ({ childId, version: 1, step: 'completed', updatedAt: new Date().toISOString() })),
    saveWakeWordSettings: saveName, saveVoiceOnboarding: vi.fn(),
  }))
  vi.spyOn(inputs, 'speakLocally').mockImplementation((_text, _session, done) => done?.('text_only'))
  vi.spyOn(localApi, 'agentTurn')
  vi.spyOn(localApi, 'completeAgentTurn').mockResolvedValue({} as AgentTurn)
  vi.spyOn(localApi, 'admitVoiceControlExecution').mockResolvedValue({ status: 'admitted' })
  vi.spyOn(localApi, 'clearSessionDraft').mockResolvedValue({ section: 'agent_command', deleted: true })
  vi.spyOn(localApi, 'agentRuntimeStatus').mockResolvedValue({} as never)
  vi.spyOn(localApi, 'answerDiagnosticPrompt')
  vi.spyOn(localApi, 'voiceInteraction')
})
afterEach(() => { cleanup(); clearVoiceCaptureContext(); localStorage.clear(); vi.restoreAllMocks(); vi.unstubAllGlobals() })

async function mount(packet: typeof wake | typeof device | typeof parent) {
  const initial = { session: { session_id: packet.session_id, child_id: packet.child_id, status: 'active', state: 'study', observation_status: 'paused' },
    plan_items: [], recalls: [], homework: [], carryover_candidates: [], carryover_decisions: [], drafts: {}, planning_context: {}, family_rule: {},
    sensor_release: { active_leases: 0, by_device: {} } } as unknown as SessionWorkspaceState
  vi.spyOn(localApi, 'sessionWorkspace').mockResolvedValue(initial)
  vi.mocked(localApi.agentTurn).mockResolvedValue(packet as unknown as AgentTurn)
  await act(async () => { render(<SessionWorkspace initial={initial} childName="合成孩子" childGrade="四年级" onBack={vi.fn()} />) })
  setVoiceQuestionCapture(packet.session_id, packet.child_id, packet.action.voice_input_binding.question_reference as VoiceQuestionReference)
  const reference = voiceCaptureReference(packet.session_id, packet.child_id)
  await act(async () => { await microphone.onTranscript(packet.transcript) })
  expect(localApi.admitVoiceControlExecution).toHaveBeenCalledExactlyOnceWith(packet.session_id, packet.agent_turn_id)
  expect(localApi.answerDiagnosticPrompt).not.toHaveBeenCalled()
  expect(voiceCaptureReference(packet.session_id, packet.child_id)).toEqual(reference)
  return reference
}

it.each([device, parent])('consumes actual HTTP $runtime_capability without scoring or granting parent authority', async packet => {
  await mount(packet)
  const call = vi.mocked(localApi.completeAgentTurn).mock.calls[0]
  expect(call[0]).toBe(packet.agent_turn_id)
  expect(call[2]).toBe('completed')
  const ack = call[4]?.client_bridge_receipt as { output: Record<string, unknown> }
  if (packet.action.type === 'find_parent') expect(ack.output).toEqual({ parent_contact_prompted: true })
  else expect(ack.output.devices).toHaveLength(2)
  expect(saveName).not.toHaveBeenCalled()
})

it('opens actual admitted naming, rejects the first name, confirms a new utterance and saves only that name', async () => {
  await mount(wake)
  expect(saveName).not.toHaveBeenCalled()
  const ack = vi.mocked(localApi.completeAgentTurn).mock.calls[0][4]?.client_bridge_receipt as { output: unknown }
  expect(ack.output).toEqual({ configuration_started: true, phase: 'confirm_name', wake_word_saved: false })
  vi.mocked(localApi.voiceInteraction).mockResolvedValueOnce({ route: 'onboarding', action: 'reject_name', source: 'cloud', response_text: '原名字保留。' })
  await act(async () => { await microphone.onTranscript('不是这个，我还没有决定') })
  expect(savedName).toBe('小知')
  vi.mocked(localApi.voiceInteraction).mockResolvedValueOnce({ route: 'onboarding', action: 'name_candidate', candidate: '小芽同学', source: 'cloud', response_text: '你想叫我小芽同学。' })
  await act(async () => { await microphone.onTranscript('叫小芽同学吧') })
  vi.mocked(localApi.voiceInteraction).mockResolvedValueOnce({ route: 'onboarding', action: 'confirm_name', source: 'cloud', response_text: '确认这个名字。' })
  await act(async () => { await microphone.onTranscript('就用这个名字') })
  expect(savedName).toBe('小芽同学')
  expect(saveName).toHaveBeenCalledTimes(1)
  expect(localApi.agentTurn).toHaveBeenCalledTimes(1)
  expect(localApi.answerDiagnosticPrompt).not.toHaveBeenCalled()
})
