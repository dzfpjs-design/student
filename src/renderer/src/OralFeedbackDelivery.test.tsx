import { beforeEach as beforeOwnedVoiceRuntimePolicyCase } from 'vitest'
import { ownedVoiceRuntimePolicyDesktop } from './test/ownedVoiceRuntimePolicy'

// This test host explicitly admits original epoch-bound capture policy.
beforeOwnedVoiceRuntimePolicyCase(() => { vi.stubGlobal('desktop', ownedVoiceRuntimePolicyDesktop()) })

import { act, cleanup, render, screen } from '@testing-library/react'
import type { ComponentProps } from 'react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { localApi } from './api'
import { SessionWorkspace } from './SessionWorkspace'
import * as inputs from './LocalInputs'
import { clearVoiceCaptureContext } from './voiceCaptureContext'
import type { AgentTurn, SessionWorkspaceState } from './types'
import packets from './test/writingCoreReceipts.json'

// Receiver contract tests use an existing synthetic session envelope with an
// explicit Oral projection. These are not an Oral backend or acoustic test.
let microphone: ComponentProps<typeof inputs.HandsFreeVoiceInput>
vi.mock('./LocalInputs', async original => ({ ...await original<typeof import('./LocalInputs')>(),
  HandsFreeVoiceInput: (props: typeof microphone) => { microphone = props; return null }, VoiceInputButton: () => null }))
vi.mock('./DocumentCameraCapture', () => ({ DocumentCameraCapture: () => null }))
vi.mock('./VoiceCalibrationPanel', () => ({ VoiceCalibrationPanel: () => null }))
const initial = packets.workspace as unknown as SessionWorkspaceState
const owner = initial.session
const feedback = '这次说出了地点。接下来补一句想去那里的理由，再试着说一遍。'

beforeEach(() => {
  vi.stubGlobal('desktop', ownedVoiceRuntimePolicyDesktop({
    getWakeWordSettings: vi.fn(async () => ({ childId: owner.child_id, wakeWord: '小知', enabled: true, inactivitySeconds: 60 })),
    getVoiceOnboarding: vi.fn(async () => ({ childId: owner.child_id, version: 1, step: 'completed', updatedAt: new Date().toISOString() })),
    saveVoiceOnboarding: vi.fn(), saveWakeWordSettings: vi.fn(),
  }))
  vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('NETWORK_FORBIDDEN')))
  for (const method of ['readVoiceInteractionHandoff', 'invalidateVoiceInteractionHandoff'] as const) {
    vi.spyOn(localApi, method).mockImplementation(async (session_id, child_id) => ({ session_id, child_id, status: 'none',
      handoff_id: null, agent_action_id: null, deferred_progress: null, request: null, receipt: null, reason: null }))
  }
  vi.spyOn(inputs, 'speakLocally').mockImplementation((_text, _session, done) => done?.('text_only'))
  vi.spyOn(localApi, 'agentTurn').mockResolvedValue(packets.confirmation.request as unknown as AgentTurn)
  vi.spyOn(localApi, 'completeAgentTurn')
  vi.spyOn(localApi, 'sessionWorkspace').mockResolvedValue(initial)
  vi.spyOn(localApi, 'clearSessionDraft').mockResolvedValue({ section: 'agent_command', deleted: true })
  vi.spyOn(localApi, 'agentRuntimeStatus').mockResolvedValue({} as never)
})
afterEach(() => { cleanup(); clearVoiceCaptureContext(); localStorage.clear(); vi.restoreAllMocks(); vi.unstubAllGlobals() })

it.each([{ recovery: false, allowed: true }, { recovery: true, allowed: true },
  { recovery: false, allowed: false }, { recovery: true, allowed: false }])(
  'current oral feedback delivery without mastery update %j', async ({ recovery, allowed }) => {
    const confirm = structuredClone(packets.confirmation.execution)
    if (recovery) confirm.turn.local_evaluation.status = 'feedback_ready'
    const envelope = structuredClone(packets.evaluated)
    envelope.turn.response_text = feedback
    envelope.execution.response_text = feedback
    Object.assign(envelope.execution.result, {
      objective_update_applied: false, feedback_delivery_allowed: allowed,
      feedback_status: allowed ? 'ready' : 'blocked', local_only: true, cloud_transmission_allowed: false,
      oral_feedback: { version: 'oral_teaching_v2', status: allowed ? 'content_self_check' : 'unavailable',
        local_only: true, cloud_transmission_allowed: false, mastery_claim_allowed: false,
        prompt_id: 'original-prompt', item_version: 'item-version',
        feedback_version: 'feedback-version', source_version: 'source-version' },
    })
    const execute = vi.spyOn(localApi, 'executeAgentTurn').mockImplementation(async id =>
      (id === packets.confirmation.request.agent_turn_id ? confirm : envelope) as never)
    const get = vi.spyOn(localApi, 'agentTurnExecutionStatus').mockResolvedValue({ status: 'completed', execution: envelope } as never)
    await act(async () => { render(<SessionWorkspace initial={initial} childName="合成孩子" childGrade="四年级" onBack={vi.fn()} />) })
    await act(async () => { await microphone.onTranscript('刚才那句是我的完整回答') })
    expect(vi.mocked(inputs.speakLocally).mock.calls.filter(call => call[0] === feedback)).toHaveLength(allowed ? 1 : 0)
    if (allowed) {
      expect(screen.getByText(feedback)).toBeTruthy()
      expect(inputs.speakLocally).toHaveBeenLastCalledWith(feedback, owner.session_id,
        expect.any(Function), expect.any(Function), expect.objectContaining({ localOnly: true }))
    }
    expect(execute).toHaveBeenCalledTimes(recovery ? 1 : 2)
    expect(get).toHaveBeenCalledTimes(recovery ? 1 : 0)
    expect(localApi.completeAgentTurn).not.toHaveBeenCalled()
  })
