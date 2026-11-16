import { beforeEach as beforeOwnedVoiceRuntimePolicyCase } from 'vitest'
import { ownedVoiceRuntimePolicyDesktop } from './test/ownedVoiceRuntimePolicy'

// This test host explicitly admits original epoch-bound capture policy.
beforeOwnedVoiceRuntimePolicyCase(() => { vi.stubGlobal('desktop', ownedVoiceRuntimePolicyDesktop()) })

import { act, cleanup, render, screen } from '@testing-library/react'
import type { ComponentProps } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { localApi } from './api'
import { SessionWorkspace } from './SessionWorkspace'
import * as inputs from './LocalInputs'
import { clearVoiceCaptureContext } from './voiceCaptureContext'
import type { AgentTurn, SessionWorkspaceState } from './types'
import packets from './test/writingCoreReceipts.json'

// These synthetic packets were produced by actual COPY HTTP/dispatcher and
// restart GET, not handwritten evaluator response or source-authority mocks.
let microphone: ComponentProps<typeof inputs.HandsFreeVoiceInput>
vi.mock('./LocalInputs', async original => ({ ...await original<typeof import('./LocalInputs')>(),
  HandsFreeVoiceInput: (props: typeof microphone) => { microphone = props; return null }, VoiceInputButton: () => null }))
vi.mock('./DocumentCameraCapture', () => ({ DocumentCameraCapture: () => null }))
vi.mock('./VoiceCalibrationPanel', () => ({ VoiceCalibrationPanel: () => null }))

describe('real writing HTTP packets reach the mounted speech receiver', () => {
  const initial = packets.workspace as unknown as SessionWorkspaceState
  const owner = initial.session
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

  it.each([false, true])('speaks the specific candidate once using local TTS policy (GET recovery=%s)', async recovery => {
    const confirm = structuredClone(packets.confirmation.execution)
    if (recovery) confirm.turn.local_evaluation.status = 'feedback_ready'
    const execute = vi.spyOn(localApi, 'executeAgentTurn').mockImplementation(async id =>
      (id === packets.confirmation.request.agent_turn_id ? confirm : packets.evaluated) as never)
    const get = vi.spyOn(localApi, 'agentTurnExecutionStatus').mockResolvedValue({ status: 'completed', execution: packets.recovered } as never)
    await act(async () => { render(<SessionWorkspace initial={initial} childName="合成孩子" childGrade="四年级" onBack={vi.fn()} />) })
    await act(async () => { await microphone.onTranscript('对，是我写的，识别没有错') })
    const response = packets.evaluated.execution.response_text
    expect(screen.getByText(response)).toBeTruthy()
    expect(vi.mocked(inputs.speakLocally).mock.calls.filter(call => call[0] === response)).toHaveLength(1)
    expect(inputs.speakLocally).toHaveBeenLastCalledWith(response, owner.session_id,
      expect.any(Function), expect.any(Function), expect.objectContaining({ localOnly: true }))
    expect(response).toContain('Make the meeting time easier to find.')
    expect(response).not.toContain('Revise the meeting time sentence.')
    expect(execute.mock.calls.map(call => call[0])).toEqual(recovery
      ? [packets.confirmation.request.agent_turn_id]
      : [packets.confirmation.request.agent_turn_id, packets.evaluated.turn.agent_turn_id])
    expect(get).toHaveBeenCalledTimes(recovery ? 1 : 0)
    expect(localApi.completeAgentTurn).not.toHaveBeenCalled()
  })
})
