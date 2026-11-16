import { act, cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { localApi } from './api'
import { SessionWorkspace } from './SessionWorkspace'
import * as inputs from './LocalInputs'
import { clearVoiceCaptureContext } from './voiceCaptureContext'
import { readPendingEvaluation, savePendingEvaluation } from './pendingEvaluationReceipt'
import type { SessionWorkspaceState } from './types'
import original from './test/oralCoreReceipts.json'
import withdrawn from './test/oralSourceChangedReceipts.json'
import shell from './test/writingCoreReceipts.json'

vi.mock('./LocalInputs', async load => ({ ...await load<typeof import('./LocalInputs')>(),
  HandsFreeVoiceInput: () => null, VoiceInputButton: () => null }))
vi.mock('./DocumentCameraCapture', () => ({ DocumentCameraCapture: () => null }))
vi.mock('./VoiceCalibrationPanel', () => ({ VoiceCalibrationPanel: () => null }))

type Recovery = Awaited<ReturnType<typeof localApi.agentTurnExecutionStatus>>
function receipt(fixture: typeof original | typeof withdrawn, index: number): Recovery {
  const packet = fixture.packets[index]
  expect(packet.method).toBe('GET')
  expect(packet.path.endsWith('/execution-status')).toBe(true)
  expect(packet.status_code).toBe(200)
  return packet.response as unknown as Recovery
}
afterEach(() => { cleanup(); clearVoiceCaptureContext(); localStorage.clear(); vi.restoreAllMocks(); vi.unstubAllGlobals() })

it.each([
  ['original', 6, true], ['restart', 8, true], ['reviewed', 11, true], ['withdrawn', 13, false],
  ['awaiting_receipt', 6, true],
  ['receipt_source_changed', 6, false], ['receipt_work_failed', 6, false],
] as const)('actual Oral HTTP %s packet reaches the mounted local speech receiver', async (stage, index, allowed) => {
  const recovered = receipt(stage === 'withdrawn' ? withdrawn : original, index)
  const envelope = recovered.execution!
  const owner = envelope.turn
  const initial = structuredClone(shell.workspace) as unknown as SessionWorkspaceState
  // Synthetic UI shell only; the original response, ownership, source flags and
  // evaluator result below are unmodified real HTTP packets from isolated DBs.
  initial.session.session_id = owner.session_id!
  initial.session.child_id = owner.child_id!
  initial.plan = null
  initial.plan_items = []
  vi.stubGlobal('desktop', {
    getWakeWordSettings: vi.fn(async () => ({ childId: owner.child_id, wakeWord: '小知', enabled: true, inactivitySeconds: 60 })),
    getVoiceOnboarding: vi.fn(async () => ({ childId: owner.child_id, version: 1, step: 'completed', updatedAt: new Date().toISOString() })),
    saveVoiceOnboarding: vi.fn(), saveWakeWordSettings: vi.fn(),
  })
  vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('NETWORK_FORBIDDEN')))
  for (const method of ['readVoiceInteractionHandoff', 'invalidateVoiceInteractionHandoff'] as const) {
    vi.spyOn(localApi, method).mockImplementation(async (session_id, child_id) => ({ session_id, child_id, status: 'none',
      handoff_id: null, agent_action_id: null, deferred_progress: null, request: null, receipt: null, reason: null }))
  }
  vi.spyOn(inputs, 'speakLocally').mockImplementation((_text, _session, done) => done?.('text_only'))
  const execute = vi.spyOn(localApi, 'executeAgentTurn')
  const get = vi.spyOn(localApi, 'agentTurnExecutionStatus').mockResolvedValue(recovered)
  const awaiting = { status: 'unknown', original_audio_teaching: { work_id: 'saved-original-work',
    state: 'response_saved', generation: 0, delivery_status: 'awaiting_receipt',
    source_verification: 'current', error_code: 'SAVED_DELIVERY_UNCONFIRMED', retry_after_seconds: 5 } }
  if (stage === 'awaiting_receipt') get.mockResolvedValueOnce(awaiting as never)
  if (stage === 'receipt_source_changed' || stage === 'receipt_work_failed') {
    get.mockResolvedValue({ ...awaiting, original_audio_teaching: { ...awaiting.original_audio_teaching,
      ...(stage === 'receipt_source_changed' ? { source_verification: 'changed' } : { state: 'failed' }) } } as never)
  }
  vi.spyOn(localApi, 'sessionWorkspace').mockResolvedValue(initial)
  vi.spyOn(localApi, 'agentRuntimeStatus').mockResolvedValue({} as never)
  savePendingEvaluation({ version: 1, sessionId: owner.session_id!, childId: owner.child_id!,
    turnId: owner.agent_turn_id, status: 'unknown', silent: false })
  await act(async () => { render(<SessionWorkspace initial={initial} childName="合成孩子" childGrade="四年级" onBack={vi.fn()} />) })
  const spoken = envelope.execution.response_text
  if (stage === 'receipt_source_changed' || stage === 'receipt_work_failed') {
    await waitFor(() => expect(get).toHaveBeenCalledOnce(), { timeout: 2500 })
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 5100)) })
    expect(get).toHaveBeenCalledOnce()
    expect(execute).not.toHaveBeenCalled()
    expect(readPendingEvaluation(owner.session_id!, owner.child_id!)?.status).toBe('unknown')
    expect(vi.mocked(inputs.speakLocally).mock.calls.filter(call => call[0] === spoken)).toHaveLength(0)
    return
  }
  await waitFor(() => expect(readPendingEvaluation(owner.session_id!, owner.child_id!)).toBeNull(),
    { timeout: stage === 'awaiting_receipt' ? 8500 : 2500 })
  expect(vi.mocked(inputs.speakLocally).mock.calls.filter(call => call[0] === spoken)).toHaveLength(allowed ? 1 : 0)
  if (allowed) {
    expect(screen.getByText(spoken)).toBeTruthy()
    expect(inputs.speakLocally).toHaveBeenLastCalledWith(spoken, owner.session_id,
      expect.any(Function), expect.any(Function), expect.objectContaining({ localOnly: true }))
  }
  expect(get).toHaveBeenCalledWith(owner.agent_turn_id)
  if (stage === 'awaiting_receipt') expect(get).toHaveBeenCalledTimes(2)
  expect(execute).not.toHaveBeenCalled()
  expect(readPendingEvaluation(owner.session_id!, owner.child_id!)).toBeNull()
}, 10000)
