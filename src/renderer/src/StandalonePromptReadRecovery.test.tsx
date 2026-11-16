import { beforeEach as beforeOwnedVoiceRuntimePolicyCase } from 'vitest'
import { ownedVoiceRuntimePolicyDesktop } from './test/ownedVoiceRuntimePolicy'

// This test host explicitly admits original epoch-bound capture policy.
beforeOwnedVoiceRuntimePolicyCase(() => { vi.stubGlobal('desktop', ownedVoiceRuntimePolicyDesktop()) })

import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import type { ComponentProps } from 'react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { ApiError, localApi } from './api'
import * as inputs from './LocalInputs'
import { SessionWorkspace } from './SessionWorkspace'
import { clearVoiceCaptureContext } from './voiceCaptureContext'
import type { AgentTurn, DiagnosticPromptResult, SessionWorkspaceState } from './types'
import packets from './test/writingCoreReceipts.json'

let microphone: ComponentProps<typeof inputs.HandsFreeVoiceInput>
vi.mock('./LocalInputs', async original => ({ ...await original<typeof import('./LocalInputs')>(),
  HandsFreeVoiceInput: (props: typeof microphone) => { microphone = props; return null }, VoiceInputButton: () => null }))
vi.mock('./ContinuousDocumentMonitor', () => ({ ContinuousDocumentMonitor: () => null }))
vi.mock('./DocumentCameraCapture', () => ({ DocumentCameraCapture: () => null }))
vi.mock('./VoiceCalibrationPanel', () => ({ VoiceCalibrationPanel: () => null }))
const workspace = structuredClone(packets.workspace) as unknown as SessionWorkspaceState
workspace.session.state = 'study'
workspace.plan = null
workspace.plan_items = []
const sid = workspace.session.session_id, child = workspace.session.child_id
const prompt = (): DiagnosticPromptResult => ({ status: 'waiting_answer', reason: null, attempt: null, prompt: {
  prompt_id: 'oral-a', item_version: 'version-a', child_id: child, origin_session_id: sid,
  diagnostic_item_id: 'picture-a', review_task_id: null, created_at: '2026-09-09T10:00:00Z',
  item: { diagnostic_item_id: 'picture-a', knowledge_node_id: 'node-picture', track: 'pet', subject: '英语',
    title: '看图描述', prompt: 'Describe these two people.', answer_type: 'speaking', assessment_mode: 'pet_reference_rubric',
    difficulty: 1, stimulus_asset: 'pet-study-room', pet_audio_modality: 'speaking' },
} })
const message = '这道题暂时没能核对，题目和图片已暂时收起。请说“再读一遍这道题”，我会重新核对，不会另出新题。'
const emit = (result = prompt()) => window.dispatchEvent(new CustomEvent('hsc-diagnostic-prompt-updated', { detail: { sessionId: sid, result } }))
const mount = async () => { await act(async () => { render(<SessionWorkspace initial={workspace} childName="合成孩子" childGrade="四年级" onBack={vi.fn()} />) }) }
const spoken = () => vi.mocked(inputs.speakLocally).mock.calls.map(([text]) => text)
beforeEach(() => {
  vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('NETWORK_FORBIDDEN')))
  for (const method of ['readVoiceInteractionHandoff', 'invalidateVoiceInteractionHandoff'] as const) {
    vi.spyOn(localApi, method).mockImplementation(async (session_id, child_id) => ({ session_id, child_id, status: 'none',
      handoff_id: null, agent_action_id: null, deferred_progress: null, request: null, receipt: null, reason: null }))
  }
  vi.stubGlobal('desktop', ownedVoiceRuntimePolicyDesktop({
    getWakeWordSettings: vi.fn(async () => ({ childId: child, wakeWord: '小知', enabled: true, inactivitySeconds: 60 })),
    getVoiceOnboarding: vi.fn(async () => ({ childId: child, version: 1, step: 'completed', updatedAt: new Date().toISOString() })),
    saveVoiceOnboarding: vi.fn(), saveWakeWordSettings: vi.fn(),
  }))
  vi.spyOn(inputs, 'speakLocally').mockImplementation((_text, _session, done) => done?.('text_only'))
  vi.spyOn(localApi, 'sessionWorkspace').mockResolvedValue(workspace)
  vi.spyOn(localApi, 'activeControlledContent').mockResolvedValue({playback:null})
  vi.spyOn(localApi, 'activeControlledGame').mockResolvedValue({game:null})
  vi.spyOn(localApi, 'agentRuntimeStatus').mockResolvedValue({} as never)
  vi.spyOn(localApi, 'diagnosticPrompt').mockResolvedValue(prompt())
  vi.spyOn(localApi, 'prepareDiagnosticPrompt')
  vi.spyOn(localApi, 'answerDiagnosticPrompt')
  vi.spyOn(localApi, 'clearSessionDraft').mockResolvedValue({ section: 'agent_command', deleted: true })
  vi.spyOn(localApi, 'completeAgentTurn').mockResolvedValue({} as AgentTurn)
  vi.spyOn(localApi, 'admitVoiceControlExecution').mockResolvedValue({ status: 'admitted' })
  vi.spyOn(localApi, 'agentTurn').mockResolvedValue({ agent_turn_id: 'recover-original', response_text: '再核对原题。',
    requires_control_admission: true, runtime_capability: 'diagnostic.replay', runtime_execution_location: 'client_device_bridge',
    action: { type: 'replay_diagnostic', capability: 'diagnostic.replay', execution_location: 'client_device_bridge',
      diagnostic_prompt_id: 'oral-a', diagnostic_item_version: 'version-a' } } as AgentTurn)
})
afterEach(() => { cleanup(); clearVoiceCaptureContext(); localStorage.clear(); vi.restoreAllMocks(); vi.unstubAllGlobals() })

it('makes an initial GET failure visible and speaks a local recovery instruction without prepare', async () => {
  vi.mocked(localApi.diagnosticPrompt).mockRejectedValue(new Error('OFFLINE_INTERNAL'))
  await mount()
  expect(screen.getByRole('alert').textContent).toBe(message)
  expect(spoken()).toContain(message)
  expect(screen.queryByRole('heading', { name: '今天的计划已经收好' })).toBeNull()
  expect(screen.getByRole('heading', { name: '正在核对当前练习' })).toBeTruthy()
  expect(screen.queryByRole('img', { name: /两名孩子在书房学习/ })).toBeNull()
  expect(localApi.prepareDiagnosticPrompt).not.toHaveBeenCalled()
})

it('recovers from initial offline through the existing admitted voice replay, with no prepare or answer', async () => {
  vi.mocked(localApi.diagnosticPrompt).mockRejectedValue(new Error('OFFLINE'))
  await mount()
  expect(screen.getByRole('alert').textContent).toBe(message)
  vi.mocked(localApi.diagnosticPrompt).mockResolvedValue(prompt())
  await act(async () => { await microphone.onTranscript('可以再读一遍这道题吗') })
  expect(localApi.admitVoiceControlExecution).toHaveBeenCalledWith(sid, 'recover-original')
  expect(localApi.diagnosticPrompt).toHaveBeenCalledWith(sid, 'oral-a')
  expect(screen.getByRole('img', { name: /两名孩子在书房学习/ })).toBeTruthy()
  expect(screen.queryByText(message)).toBeNull()
  expect(localApi.prepareDiagnosticPrompt).not.toHaveBeenCalled()
  expect(localApi.answerDiagnosticPrompt).not.toHaveBeenCalled()
})

it('hides an unverified old picture on failed refresh, then a real voice replay restores only the original', async () => {
  await mount()
  expect(screen.getAllByText('Describe these two people.').length).toBeGreaterThan(0)
  fireEvent.change(screen.getByRole('textbox', { name: '输入挑战题答案' }), { target: { value: 'Unsubmitted child words' } })
  vi.mocked(localApi.diagnosticPrompt).mockRejectedValue(new Error('OFFLINE_INTERNAL'))
  await act(async () => { emit() })
  expect(screen.queryByRole('img', { name: /两名孩子在书房学习/ })).toBeNull()
  expect(screen.queryByRole('textbox', { name: '输入挑战题答案' })).toBeNull()
  expect(screen.getByRole('alert').textContent).toBe(message)
  vi.mocked(localApi.diagnosticPrompt).mockResolvedValue(prompt())
  await act(async () => { await microphone.onTranscript('再读一遍这道题') })
  expect(localApi.diagnosticPrompt).toHaveBeenCalledWith(sid, 'oral-a')
  expect(screen.getAllByText('Describe these two people.').length).toBeGreaterThan(0)
  expect(screen.queryByText(message)).toBeNull()
  expect((screen.getByRole('textbox', { name: '输入挑战题答案' }) as HTMLTextAreaElement).value).toBe('Unsubmitted child words')
  expect(localApi.prepareDiagnosticPrompt).not.toHaveBeenCalled()
  expect(localApi.answerDiagnosticPrompt).not.toHaveBeenCalled()
  expect(localApi.completeAgentTurn).toHaveBeenCalledWith('recover-original', expect.any(String), 'completed', undefined, expect.any(Object))
})

it.each(['other-child', 'item-mismatch', 'new-version'] as const)('a current read with %s removes the unverified picture without an earlier network error', async kind => {
  await mount()
  const changed = prompt()
  if (kind === 'other-child') changed.prompt!.child_id = 'other-child'
  else if (kind === 'item-mismatch') changed.prompt!.item.diagnostic_item_id = 'not-the-original'
  else changed.prompt!.item_version = 'version-new'
  vi.mocked(localApi.diagnosticPrompt).mockResolvedValue(changed)
  await act(async () => { emit() })
  expect(screen.queryByRole('img', { name: /两名孩子在书房学习/ })).toBeNull()
  expect(screen.getByRole('alert').textContent).toBe(message)
  expect(localApi.prepareDiagnosticPrompt).not.toHaveBeenCalled()
})

it.each(['cancelled', 'invalidated', 'other-child', 'new-prompt'] as const)('failed refresh cannot recover %s as the original', async kind => {
  await mount()
  vi.mocked(localApi.diagnosticPrompt).mockRejectedValue(new Error('OFFLINE'))
  await act(async () => { emit() })
  const changed = prompt()
  if (kind === 'other-child') changed.prompt!.child_id = 'other-child'
  else if (kind === 'new-prompt') { changed.prompt!.prompt_id = 'oral-b'; changed.prompt!.item_version = 'version-b' }
  else changed.status = kind
  vi.mocked(localApi.diagnosticPrompt).mockResolvedValue(changed)
  await act(async () => { emit() })
  expect(screen.queryByRole('img', { name: /两名孩子在书房学习/ })).toBeNull()
  expect(screen.queryByRole('textbox', { name: '输入挑战题答案' })).toBeNull()
  expect(localApi.prepareDiagnosticPrompt).not.toHaveBeenCalled()
  expect(localApi.answerDiagnosticPrompt).not.toHaveBeenCalled()
})

it('late failed reads cannot clear a newly confirmed prompt', async () => {
  await mount()
  let reject!: (error: Error) => void
  vi.mocked(localApi.diagnosticPrompt).mockReturnValueOnce(new Promise((_resolve, fail) => { reject = fail }))
  await act(async () => { emit() })
  const next = prompt(); next.prompt!.prompt_id = 'oral-b'; next.prompt!.item_version = 'version-b'
  next.prompt!.item.prompt = 'Describe the new picture.'
  vi.mocked(localApi.diagnosticPrompt).mockResolvedValue(next)
  await act(async () => { emit(next) })
  await act(async () => { reject(new Error('OLD_OFFLINE')) })
  expect(screen.getByText('Describe the new picture.')).toBeTruthy()
  expect(screen.queryByText(message)).toBeNull()
})

it('unmount discards a late failed read and does not speak to another screen', async () => {
  await mount()
  let reject!: (error: Error) => void
  vi.mocked(localApi.diagnosticPrompt).mockReturnValueOnce(new Promise((_resolve, fail) => { reject = fail }))
  await act(async () => { emit() })
  cleanup()
  vi.mocked(inputs.speakLocally).mockClear()
  await act(async () => { reject(new Error('OLD_OFFLINE')) })
  expect(spoken()).not.toContain(message)
  expect(localApi.prepareDiagnosticPrompt).not.toHaveBeenCalled()
})

it('actual replay GET failure removes the displayed original and preserves unsent text for a new voice retry', async () => {
  await mount()
  fireEvent.change(screen.getByRole('textbox', { name: '输入挑战题答案' }), { target: { value: 'My unsent words' } })
  vi.mocked(localApi.diagnosticPrompt).mockRejectedValue(new ApiError('Read failed', 503, 'LOCAL_SERVICE_OFFLINE'))
  vi.mocked(inputs.speakLocally).mockClear()
  await act(async () => { await microphone.onTranscript('请再读一下刚才那道题') })
  expect(localApi.diagnosticPrompt).toHaveBeenCalledWith(sid, 'oral-a')
  expect(screen.queryByRole('img', { name: /两名孩子在书房学习/ })).toBeNull()
  expect(screen.getByRole('alert').textContent).toBe(message)
  expect(spoken().filter(text => text === message)).toHaveLength(1)
  expect(localApi.completeAgentTurn).toHaveBeenCalledWith('recover-original', message, 'failed', 'LOCAL_SERVICE_OFFLINE')
  vi.mocked(localApi.diagnosticPrompt).mockResolvedValue(prompt())
  vi.mocked(localApi.agentTurn).mockResolvedValue({ ...await vi.mocked(localApi.agentTurn).mock.results[0].value,
    agent_turn_id: 'fresh-original-replay' })
  await act(async () => { await microphone.onTranscript('现在再读一遍这道题') })
  expect(screen.getByRole('img', { name: /两名孩子在书房学习/ })).toBeTruthy()
  expect((screen.getByRole('textbox', { name: '输入挑战题答案' }) as HTMLTextAreaElement).value).toBe('My unsent words')
  expect(localApi.prepareDiagnosticPrompt).not.toHaveBeenCalled()
  expect(localApi.answerDiagnosticPrompt).not.toHaveBeenCalled()
})

it.each(['new-prompt', 'new-version', 'same-prompt-fresh-read', 'cancelled'] as const)('actual replay late failure cannot clear %s confirmed while the GET was pending', async kind => {
  await mount()
  let reject!: (error: Error) => void
  vi.mocked(localApi.diagnosticPrompt).mockReturnValueOnce(new Promise((_resolve, fail) => { reject = fail }))
  let pending!: Promise<void>
  await act(async () => { pending = Promise.resolve(microphone.onTranscript('请再读一下刚才那道题')) })
  expect(localApi.diagnosticPrompt).toHaveBeenCalledWith(sid, 'oral-a')
  const current = prompt()
  if (kind === 'new-prompt') current.prompt!.prompt_id = 'oral-new'
  if (kind === 'new-version') current.prompt!.item_version = 'version-new'
  if (kind === 'cancelled') current.status = 'cancelled'
  vi.mocked(localApi.diagnosticPrompt).mockResolvedValue(current)
  await act(async () => { emit(current) })
  await act(async () => { reject(new ApiError('Read failed', 503, 'LOCAL_SERVICE_OFFLINE')); await pending })
  expect(screen.queryByText(message)).toBeNull()
  expect(Boolean(screen.queryByRole('img', { name: /两名孩子在书房学习/ }))).toBe(kind !== 'cancelled')
  expect(localApi.answerDiagnosticPrompt).not.toHaveBeenCalled()
})

it.each(['sessionId', 'childId', 'promptId', 'itemVersion'] as const)('replay failure notification with a different %s cannot clear the displayed original', async field => {
  await mount()
  const detail = { sessionId: sid, childId: child, promptId: 'oral-a', itemVersion: 'version-a', requestId: 'not-this-read', [field]: 'other' }
  await act(async () => {
    window.dispatchEvent(new CustomEvent('hsc-diagnostic-replay-read', { detail: { ...detail, phase: 'begin' } }))
    window.dispatchEvent(new CustomEvent('hsc-diagnostic-replay-read', { detail: { ...detail, phase: 'failed' } }))
  })
  expect(screen.getByRole('img', { name: /两名孩子在书房学习/ })).toBeTruthy()
  expect(screen.queryByText(message)).toBeNull()
  expect(localApi.prepareDiagnosticPrompt).not.toHaveBeenCalled()
  expect(localApi.answerDiagnosticPrompt).not.toHaveBeenCalled()
})

it('actual pending replay failure survives a same-session workspace refresh with a fresh plan_items array', async () => {
  await mount()
  fireEvent.change(screen.getByRole('textbox', { name: '输入挑战题答案' }), { target: { value: 'Unsent before refresh' } })
  let reject!: (error: Error) => void
  vi.mocked(localApi.diagnosticPrompt).mockReturnValueOnce(new Promise((_resolve, fail) => { reject = fail }))
  let pending!: Promise<void>
  await act(async () => { pending = Promise.resolve(microphone.onTranscript('请再读一下刚才那道题')) })
  expect(localApi.diagnosticPrompt).toHaveBeenCalledWith(sid, 'oral-a')
  const readCount = vi.mocked(localApi.diagnosticPrompt).mock.calls.length
  const fresh = structuredClone(workspace)
  expect(fresh.plan_items).not.toBe(workspace.plan_items)
  vi.mocked(localApi.sessionWorkspace).mockClear().mockResolvedValue(fresh)
  vi.spyOn(localApi, 'activityVoiceFocus').mockResolvedValue({ activity_focus: null } as never)
  // Use the existing run -> refresh -> setWorkspace path. The controlled GET
  // returns the same study snapshot; this tests projection refresh, not a
  // successful backend phase transition or permission to finish learning.
  vi.spyOn(localApi, 'transition').mockResolvedValue({} as never)
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: '回顾一下今天' })) })
  await waitFor(() => expect(localApi.sessionWorkspace).toHaveBeenCalledWith(sid))
  expect(localApi.diagnosticPrompt).toHaveBeenCalledTimes(readCount)
  expect(screen.getByRole('img', { name: /两名孩子在书房学习/ })).toBeTruthy()
  await act(async () => { reject(new ApiError('Read failed', 503, 'LOCAL_SERVICE_OFFLINE')); await pending })
  expect(screen.queryByRole('img', { name: /两名孩子在书房学习/ })).toBeNull()
  expect(screen.getByRole('alert').textContent).toBe(message)
  expect(localApi.completeAgentTurn).toHaveBeenCalledWith('recover-original', message, 'failed', 'LOCAL_SERVICE_OFFLINE')
  vi.mocked(localApi.diagnosticPrompt).mockResolvedValue(prompt())
  vi.mocked(localApi.agentTurn).mockResolvedValue({ ...await vi.mocked(localApi.agentTurn).mock.results[0].value, agent_turn_id: 'after-refresh-replay' })
  await act(async () => { await microphone.onTranscript('现在再读一遍') })
  expect((screen.getByRole('textbox', { name: '输入挑战题答案' }) as HTMLTextAreaElement).value).toBe('Unsent before refresh')
  expect(localApi.prepareDiagnosticPrompt).not.toHaveBeenCalled()
  expect(localApi.answerDiagnosticPrompt).not.toHaveBeenCalled()
})
