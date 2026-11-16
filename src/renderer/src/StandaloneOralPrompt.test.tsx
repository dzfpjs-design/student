import { act, cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { localApi } from './api'
import { SessionWorkspace } from './SessionWorkspace'
import { clearVoiceCaptureContext, trackDiagnosticCapture, voiceCaptureReference } from './voiceCaptureContext'
import type { DiagnosticPromptResult, SessionWorkspaceState } from './types'
import packets from './test/writingCoreReceipts.json'

vi.mock('./LocalInputs', async original => ({ ...await original<typeof import('./LocalInputs')>(),
  HandsFreeVoiceInput: () => null, VoiceInputButton: () => null, speakLocally: vi.fn() }))
vi.mock('./ContinuousDocumentMonitor', () => ({ ContinuousDocumentMonitor: () => null }))
vi.mock('./DocumentCameraCapture', () => ({ DocumentCameraCapture: () => null }))
vi.mock('./VoiceCalibrationPanel', () => ({ VoiceCalibrationPanel: () => null }))
const workspace = structuredClone(packets.workspace) as unknown as SessionWorkspaceState
workspace.session.state = 'study'
workspace.plan = null
workspace.plan_items = []
const sid = workspace.session.session_id, child = workspace.session.child_id
const current = (): DiagnosticPromptResult => ({ status: 'waiting_answer', reason: null, attempt: null, prompt: {
  prompt_id: 'oral-a', item_version: 'version-a', child_id: child, origin_session_id: sid,
  diagnostic_item_id: 'picture-a', review_task_id: null, created_at: '2026-09-09T10:00:00Z',
  item: { diagnostic_item_id: 'picture-a', knowledge_node_id: 'node-picture', track: 'pet', subject: '英语',
    title: '看图描述', prompt: 'Describe these two people.', answer_type: 'speaking', assessment_mode: 'pet_reference_rubric',
    difficulty: 1, stimulus_asset: 'pet-study-room', pet_audio_modality: 'speaking' },
} })
const answered = (): DiagnosticPromptResult => ({ ...current(), status: 'answered', attempt: {
  attempt_id: 'answer-a', evidence_id: 'evidence-a', diagnostic_item_id: 'picture-a', child_id: child,
  session_id: sid, submitted_answer: 'The girl is writing.', correctness: .5, score_status: 'needs_review',
  created_at: '2026-09-09T10:01:00Z', feedback: 'Reference only.',
} as DiagnosticPromptResult['attempt'] })
beforeEach(() => {
  vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('NETWORK_FORBIDDEN')))
  vi.spyOn(localApi, 'sessionWorkspace').mockResolvedValue(workspace)
  vi.spyOn(localApi, 'agentRuntimeStatus').mockResolvedValue({} as never)
  vi.spyOn(localApi, 'diagnosticPrompt').mockResolvedValue(current())
})
afterEach(() => { cleanup(); clearVoiceCaptureContext(); localStorage.clear(); vi.restoreAllMocks(); vi.unstubAllGlobals() })

it('restores the actual waiting picture without a daily task or a new prepare', async () => {
  const prepare = vi.spyOn(localApi, 'prepareDiagnosticPrompt')
  render(<SessionWorkspace initial={workspace} childName="合成孩子" childGrade="四年级" onBack={vi.fn()} />)
  expect(await screen.findByText('Describe these two people.')).toBeTruthy()
  expect(screen.getByRole('img', { name: /两名孩子在书房学习/ })).toBeTruthy()
  expect(screen.queryByRole('heading', { name: '今天的计划已经收好' })).toBeNull()
  expect(screen.getByRole('heading', { name: '继续眼前这道题' })).toBeTruthy()
  expect(prepare).not.toHaveBeenCalled()
})

it.each(['cancelled', 'invalidated', 'no_item'] as const)('does not display %s source', async status => {
  vi.mocked(localApi.diagnosticPrompt).mockResolvedValue({ ...current(), status })
  render(<SessionWorkspace initial={workspace} childName="合成孩子" childGrade="四年级" onBack={vi.fn()} />)
  await waitFor(() => expect(localApi.diagnosticPrompt).toHaveBeenCalled())
  expect(screen.queryByText('Describe these two people.')).toBeNull()
})

it('does not display a response for another child', async () => {
  const other = current(); other.prompt!.child_id = 'other-child'
  vi.mocked(localApi.diagnosticPrompt).mockResolvedValue(other)
  render(<SessionWorkspace initial={workspace} childName="合成孩子" childGrade="四年级" onBack={vi.fn()} />)
  await waitFor(() => expect(localApi.diagnosticPrompt).toHaveBeenCalled())
  expect(screen.queryByText('Describe these two people.')).toBeNull()
})

it('a cancelled or replaced prompt cannot be revived by its late UI receipt', async () => {
  render(<SessionWorkspace initial={workspace} childName="合成孩子" childGrade="四年级" onBack={vi.fn()} />)
  await screen.findByText('Describe these two people.')
  vi.mocked(localApi.diagnosticPrompt).mockResolvedValue({ ...current(), status: 'cancelled' })
  const emit = (result: DiagnosticPromptResult) => window.dispatchEvent(new CustomEvent('hsc-diagnostic-prompt-updated', { detail: { sessionId: sid, result } }))
  await act(async () => { emit(current()) })
  expect(screen.queryByText('Describe these two people.')).toBeNull()
  const next = current(); next.prompt!.prompt_id = 'oral-b'; next.prompt!.item_version = 'version-b'
  next.prompt!.item.prompt = 'Describe the new picture.'
  vi.mocked(localApi.diagnosticPrompt).mockResolvedValue(next)
  await act(async () => { emit(next) })
  expect(await screen.findByText('Describe the new picture.')).toBeTruthy()
  await act(async () => { emit(current()) })
  expect(screen.queryByText('Describe these two people.')).toBeNull()
  expect(screen.getByText('Describe the new picture.')).toBeTruthy()
})

it('keeps exact answered oral identity for the next captured control, not answer permission', async () => {
  await trackDiagnosticCapture(sid, async () => current())
  await trackDiagnosticCapture(sid, async () => answered(), true)
  expect(voiceCaptureReference(sid, child).question_reference).toEqual({ kind: 'diagnostic', binding: { prompt_id: 'oral-a', item_version: 'version-a' } })
  expect(voiceCaptureReference(sid, 'other-child').question_reference).toBeNull()
  await trackDiagnosticCapture(sid, async () => ({ ...answered(), status: 'invalidated' }))
  expect(voiceCaptureReference(sid, child).question_reference).toBeNull()
})

it('new prompt replaces captured control identity, late old answered read cannot change it', async () => {
  await trackDiagnosticCapture(sid, async () => answered(), true)
  const first = voiceCaptureReference(sid, child)
  const next = current(); next.prompt!.prompt_id = 'oral-b'; next.prompt!.item_version = 'version-b'
  await trackDiagnosticCapture(sid, async () => next, true)
  await trackDiagnosticCapture(sid, async () => answered())
  expect(voiceCaptureReference(sid, child).question_reference).toMatchObject({ binding: { prompt_id: 'oral-b' } })
  expect(first.question_reference).toMatchObject({ binding: { prompt_id: 'oral-a' } })
})

it('rehydrates scored school help identity after restart during a generic active task', async () => {
  const state = structuredClone(workspace)
  state.plan_items = [{ plan_item_id: 'generic-active', status: 'active', source_type: 'school_homework',
    source_ref: 'generic', subject: '数学', title: '当前作业', expected_minutes: 20, position: 1,
    active_seconds: 10, active_segment_started_at: null,
  }] as SessionWorkspaceState['plan_items']
  const saved = answered(); saved.prompt!.item.track = 'school'; saved.prompt!.item.answer_type = 'text'
  vi.mocked(localApi.diagnosticPrompt).mockImplementation(async () => trackDiagnosticCapture(sid, async () => saved))
  const prepare = vi.spyOn(localApi, 'prepareDiagnosticPrompt')
  render(<SessionWorkspace initial={state} childName="合成孩子" childGrade="四年级" onBack={vi.fn()} />)
  await waitFor(() => expect(voiceCaptureReference(sid, child).question_reference).toEqual({
    kind: 'diagnostic_help', binding: { prompt_id: 'oral-a', item_version: 'version-a' },
  }))
  expect(prepare).not.toHaveBeenCalled()
  expect(screen.queryByRole('heading', { name: '继续眼前这道题' })).toBeNull()
})
