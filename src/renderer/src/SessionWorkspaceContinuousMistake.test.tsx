import { act, cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { ComponentProps } from 'react'
import { SessionWorkspace } from './SessionWorkspace'
import { localApi } from './api'
import * as inputs from './LocalInputs'
import type { ContinuousDocumentMonitor } from './ContinuousDocumentMonitor'
import type { DocumentPageRevision, SessionWorkspaceState } from './types'

let monitor: ComponentProps<typeof ContinuousDocumentMonitor> | undefined
vi.mock('./ContinuousDocumentMonitor', () => ({ ContinuousDocumentMonitor: (props: NonNullable<typeof monitor>) => { monitor = props; return null } }))
vi.mock('./LocalInputs', async original => ({ ...await original<typeof import('./LocalInputs')>(),
  HandsFreeVoiceInput: () => null, VoiceInputButton: () => null }))
vi.mock('./VoiceCalibrationPanel', () => ({ VoiceCalibrationPanel: () => null }))

beforeEach(() => {
  monitor = undefined
  vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('NETWORK_FORBIDDEN')))
  vi.spyOn(inputs, 'speakLocally').mockImplementation((_text, _session, done) => done?.('text_only'))
  vi.spyOn(localApi, 'saveSessionDraft').mockImplementation(async (_session, section, payload) => ({ section, payload, updated_at: new Date().toISOString() }))
  vi.spyOn(localApi, 'agentRuntimeStatus').mockResolvedValue({} as never)
  vi.spyOn(localApi, 'mistakeDialogue').mockResolvedValue({ dialogue: null })
  vi.spyOn(localApi, 'mistakeCaptureQueues').mockResolvedValue([])
  vi.spyOn(localApi, 'activeControlledContent').mockResolvedValue({ playback: null })
  vi.spyOn(localApi, 'activeControlledGame').mockResolvedValue({ game: null })
})
afterEach(() => { cleanup(); localStorage.clear(); vi.restoreAllMocks(); vi.unstubAllGlobals() })

it('mounts one active document-camera runtime from check-in so automatic and spoken connection are available', async () => {
  const initial = { session: { session_id: 'check-in-device-session', child_id: 'child-primary-4', status: 'active', state: 'check_in', observation_status: 'enabled' },
    plan_items: [], recalls: [], homework: [], carryover_candidates: [], carryover_decisions: [], drafts: {}, planning_context: {}, family_rule: {},
    latest_tutor_turn: null, sensor_release: { active_leases: 0, by_device: {} } } as unknown as SessionWorkspaceState
  vi.spyOn(localApi, 'sessionWorkspace').mockResolvedValue(initial)
  await act(async () => { render(<SessionWorkspace initial={initial} childName="合成测试" childGrade="四年级" onBack={vi.fn()} />) })
  await waitFor(() => expect(monitor).toBeDefined())
  expect(monitor?.sessionId).toBe(initial.session.session_id)
  expect(monitor?.active).toBe(true)
})

it.each(['child-primary-4', 'child-junior-3'])('opens marked-page confirmation without a child button for %s', async child => {
  const session = 'marked-' + child
  const initial = { session: { session_id: session, child_id: child, status: 'active', state: 'study', observation_status: 'paused' },
    plan_items: [{ plan_item_id: 'written-task', status: 'active', subject: '英语', title: '本页作业', source_type: 'school_homework',
      task_type: 'written', completion_checklist: [], expected_minutes: 20, active_seconds: 0, active_segment_started_at: null }],
    recalls: [], homework: [], carryover_candidates: [], carryover_decisions: [], drafts: {}, planning_context: {}, family_rule: {},
    latest_tutor_turn: null, sensor_release: { active_leases: 0, by_device: {} } } as unknown as SessionWorkspaceState
  vi.spyOn(localApi, 'sessionWorkspace').mockResolvedValue(initial)
  const opened = { session_id: session, child_id: child, candidate_id: 'marked-candidate', mistake_id: null, phase: 'candidate',
    state: { artifact_id: 'marked-artifact', question_text: '2. A.grape B.make C.like', submitted_answer: '' },
    response_text: '这里有个批改标记，但不一定是错题。先核对第二题，可以吗？' }
  const begin = vi.spyOn(localApi, 'beginMistakeDialogue').mockImplementation(async () => {
    vi.mocked(localApi.mistakeDialogue).mockResolvedValue({ dialogue: opened })
    return opened
  })
  const tutor = vi.spyOn(localApi, 'askTutor')
  const revision = { artifact_id: 'marked-artifact', revision_id: 'marked-version', marked_question_count: 1,
    extracted_text: '1. Other question\n2. A.grape B.make C.like', ocr_confidence: .91, review_required: true,
    revision_index: 1, previous_revision_id: null, created_at: new Date().toISOString(), raw_image_transmitted: false,
    change_summary: { similarity: 0, added_lines: [], removed_lines: [], changed: true } } as DocumentPageRevision
  await act(async () => { render(<SessionWorkspace initial={initial} childName="合成测试" childGrade="四年级" onBack={vi.fn()} />) })
  await waitFor(() => expect(monitor?.active).toBe(true))
  await act(async () => { expect(monitor?.onReviewRevision?.(revision)).toBe('mistake') })
  expect(await screen.findByRole('region', { name: '错题语音订正' })).toBeInTheDocument()
  expect(begin).toHaveBeenCalledExactlyOnceWith(session, 'marked-artifact', expect.any(String))
  expect(screen.getByText('2. A.grape B.make C.like')).toBeInTheDocument()
  expect(screen.queryByRole('region', { name: '题干核对' })).not.toBeInTheDocument()
  expect(tutor).not.toHaveBeenCalled()
})
