import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError, localApi } from './api'
import { SessionWorkspace } from './SessionWorkspace'
import type { AgentTurn, OcrArtifact, SessionWorkspaceState, TutorTurn } from './types'

const sessionId = 'ocr-agent-session'
const question = '12加8等于多少？'

const clearArtifact: OcrArtifact = {
  artifact_id: 'ocr-clear-1', extracted_text: question, confidence: 0.92, local_path: null, status: 'completed',
  details: { lines: [], engine: 'local', ocr_confidence: 0.92, quality: { passed: true, issues: [], messages: [], metrics: {}, version: 'test' } }
}
const unclearArtifact: OcrArtifact = {
  ...clearArtifact, artifact_id: 'ocr-unclear-1', confidence: 0.41, status: 'quality_warning',
  details: { ...clearArtifact.details, ocr_confidence: 0.41, quality: { passed: false, issues: ['blurred'], messages: ['画面模糊。'], metrics: {}, version: 'test' } }
}

vi.mock('./DocumentCameraCapture', () => ({
  DocumentCameraCapture: ({ onArtifact, onText }: { onArtifact?: (artifact: OcrArtifact) => void; onText?: (text: string, artifact: OcrArtifact) => void }) => (
    <div aria-label="模拟高拍仪">
      <button onClick={() => { onArtifact?.(clearArtifact); onText?.(clearArtifact.extracted_text, clearArtifact) }}>模拟清晰识别</button>
      <button onClick={() => { onArtifact?.(unclearArtifact); onText?.(unclearArtifact.extracted_text, unclearArtifact) }}>模拟不清晰识别</button>
    </div>
  )
}))

function workspace(drafts: SessionWorkspaceState['drafts'] = {}): SessionWorkspaceState {
  return {
    session: { session_id: sessionId, child_id: 'child-primary-4', status: 'active', state: 'study', started_at: '2026-08-28T10:00:00Z', last_checkpoint_at: '2026-08-28T10:00:00Z', observation_revision: 0, observation_generation: 'a'.repeat(64), observation_status: 'paused', snooze_until: null, recall_declared_empty: 0, homework_declared_empty: 0 },
    family_rule: { child_id: 'child-primary-4', latest_end_time: '21:00', max_learning_minutes: 90, task_segment_minutes: 25, break_minutes: 5, pet_min_minutes: 0, competition_minutes: 0, observation_mode: 'shadow', reminder_budget_per_10m: 1, misclassification_shadow_threshold: 3, version: 1, updated_at: '2026-08-28T10:00:00Z' },
    planning_context: { energy_level: 'medium', fixed_commitment_minutes: 0, fixed_commitment_note: null, tomorrow_event_subject: null, tomorrow_event_title: null, tomorrow_event_minutes: 0, updated_at: '2026-08-28T10:00:00Z' },
    recalls: [], homework: [], carryover_candidates: [], carryover_decisions: [],
    plan: { plan_id: 'ocr-agent-plan', plan_date: '2026-08-28', status: 'active', total_minutes: 25, generator: 'local_rule_engine', version: 1 },
    plan_items: [{ plan_item_id: 'ocr-agent-plan-item', homework_id: null, subject: '数学', title: '数学练习', expected_minutes: 25, actual_minutes: null, priority: 1, status: 'pending', position: 1, started_at: null, completed_at: null, active_seconds: 0, active_segment_started_at: null, source_type: 'school_homework', source_ref: null, completion_standard: '完成并检查', cognitive_load: 'medium', task_type: 'written', completion_evidence_type: null, completion_note: null, completion_checklist: [], completion_artifact_id: null }],
    daily_report_ready: false,
    latest_tutor_turn: null,
    sensor_release: { opened_leases: 0, active_leases: 0, released_leases: 0, application_release_confirmed: false, evidence_level: 'not_opened', os_release_verified: false, release_verification: null, by_device: {} },
    drafts
  }
}

function agentCaptureTurn(): AgentTurn {
  return { agent_turn_id: 'agent-capture-1', transcript: '看一下这题', intent: 'capture_question', response_text: '好，把题目放到高拍仪下，我们先确认题干是完整的。', action: { type: 'request_capture' }, orchestrator_version: 'local_intent_v3', tool_status: 'pending', tool_error_code: null, completed_at: null, created_at: '2026-08-28T10:00:00Z' }
}

function tutorAnswer(): TutorTurn {
  return { turn_id: 'tutor-ocr-1', session_id: sessionId, child_id: 'child-primary-4', plan_item_id: 'ocr-agent-plan-item', subject: '数学', question, answer: '先看看这道题问的是总数还是其中一部分。', provider: 'test', model: 'test', status: 'completed', hint_level: 1, parent_turn_id: null, response_kind: 'hint', verification_status: 'local_only', safety_status: 'passed', strategy_experiment_id: null, strategy_version: null, output_envelope: null, created_at: '2026-08-28T10:00:00Z' }
}

function installSpeech(): void {
  class FakeUtterance { constructor(readonly text: string) {} voice: SpeechSynthesisVoice | null = null }
  vi.stubGlobal('SpeechSynthesisUtterance', FakeUtterance)
  vi.stubGlobal('speechSynthesis', { cancel: vi.fn(), getVoices: () => [], speak: vi.fn() })
}

beforeEach(() => {
  vi.spyOn(localApi, 'readVoiceInteractionHandoff').mockImplementation(async (session, child) => ({
    session_id: session, child_id: child, status: 'none', handoff_id: null, agent_action_id: null,
    deferred_progress: null, request: null, receipt: null, reason: null,
  }))
  vi.spyOn(localApi, 'invalidateVoiceInteractionHandoff').mockImplementation(async (session, child) => ({
    session_id: session, child_id: child, status: 'none', handoff_id: null, agent_action_id: null,
    deferred_progress: null, request: null, receipt: null, reason: null,
  }))
  vi.spyOn(localApi, 'activeControlledContent').mockResolvedValue({ playback: null })
  vi.spyOn(localApi, 'activeControlledGame').mockResolvedValue({ game: null })
})
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals() })

describe('OCR-backed Agent tutor flow', () => {
  it('routes “看一下这题” to capture and blocks uncertain OCR from tutoring', async () => {
    installSpeech()
    vi.spyOn(localApi, 'mistakeCaptureQueues').mockResolvedValue([])
    vi.spyOn(localApi, 'agentTurn').mockResolvedValue(agentCaptureTurn())
    vi.spyOn(localApi, 'completeAgentTurn').mockResolvedValue(agentCaptureTurn())
    const tutor = vi.spyOn(localApi, 'askTutor')
    render(<SessionWorkspace initial={workspace()} childName="小辰" childGrade="四年级" onBack={vi.fn()} />)

    fireEvent.change(screen.getByLabelText('对学习伙伴说'), { target: { value: '看一下这题' } })
    fireEvent.click(screen.getByRole('button', { name: '发送' }))
    expect(await screen.findByRole('button', { name: '模拟不清晰识别' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '模拟不清晰识别' }))
    expect(screen.getAllByText(/题干需要人工修正/).length).toBeGreaterThan(0)

    fireEvent.change(screen.getByLabelText('输入学习问题'), { target: { value: question } })
    fireEvent.click(screen.getByRole('button', { name: '提问' }))
    expect(tutor).not.toHaveBeenCalled()
    expect(screen.getByRole('alert')).toHaveTextContent(/题干需要人工修正/)
  })

  it('persists confirmation, then sends only its ID to the tutor endpoint', async () => {
    installSpeech()
    vi.spyOn(localApi, 'mistakeCaptureQueues').mockResolvedValue([])
    const fetchSpy = vi.fn().mockImplementation((url: string) => Promise.resolve(url.includes('tutor-ocr-confirmations')
      ? { ok: true, json: async () => ({ confirmation_id: 'confirmation-clear-1', confirmed_text: question, confirmation_kind: 'ocr_verified', confirmed_at: new Date().toISOString(), expires_at: new Date(Date.now() + 60 * 60_000).toISOString() }) }
      : { ok: true, json: async () => tutorAnswer() }))
    vi.stubGlobal('fetch', fetchSpy)
    render(<SessionWorkspace initial={workspace()} childName="小辰" childGrade="四年级" onBack={vi.fn()} />)

    fireEvent.click(screen.getByRole('button', { name: /用高拍仪拍题/ }))
    fireEvent.click(await screen.findByRole('button', { name: '模拟清晰识别' }))
    expect(screen.getByText(/请先核对题干、数字和选项/)).toBeInTheDocument()
    expect(screen.getByLabelText('输入学习问题')).toHaveValue('')
    fireEvent.click(screen.getByRole('button', { name: '我已核对题干' }))
    expect(await screen.findByText('题干已确认')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '提问' }))

    expect(await screen.findByText('先看看这道题问的是总数还是其中一部分。')).toBeInTheDocument()
    const confirmationRequest = fetchSpy.mock.calls.map((call) => call[1] as RequestInit).find((candidate) =>
      String(candidate.body).includes('confirmed_text')
    )
    expect(confirmationRequest).toBeDefined()
    expect(JSON.parse(String(confirmationRequest!.body))).toEqual(expect.objectContaining({
      confirmed_text: question, artifact_id: 'ocr-clear-1', confirmation_kind: 'ocr_verified'
    }))
    const tutorRequest = fetchSpy.mock.calls.find((call) => String(call[0]).endsWith('/tutor'))?.[1] as RequestInit | undefined
    expect(tutorRequest).toBeDefined()
    expect(JSON.parse(String(tutorRequest!.body))).toEqual(expect.objectContaining({ ocr_confirmation_id: 'confirmation-clear-1', question: '已确认的本地题干' }))
    expect(String(tutorRequest!.body)).not.toContain('image')
  })

  it('confirms the OCR text and starts tutoring from voice without a child button click', async () => {
    installSpeech()
    vi.spyOn(localApi, 'mistakeCaptureQueues').mockResolvedValue([])
    vi.spyOn(localApi, 'agentTurn').mockResolvedValue({
      ...agentCaptureTurn(), transcript: '题干是对的', intent: 'confirm_ocr_text',
      action: { type: 'confirm_ocr_text' }
    })
    vi.spyOn(localApi, 'completeAgentTurn').mockResolvedValue(agentCaptureTurn())
    const fetchSpy = vi.fn().mockImplementation((url: string) => Promise.resolve(url.includes('tutor-ocr-confirmations')
      ? { ok: true, json: async () => ({ confirmation_id: 'confirmation-voice-1', confirmed_text: question, confirmation_kind: 'ocr_verified', confirmed_at: new Date().toISOString(), expires_at: new Date(Date.now() + 60 * 60_000).toISOString() }) }
      : { ok: true, json: async () => tutorAnswer() }))
    vi.stubGlobal('fetch', fetchSpy)
    render(<SessionWorkspace initial={workspace()} childName="小辰" childGrade="四年级" onBack={vi.fn()} />)

    fireEvent.click(screen.getByRole('button', { name: /用高拍仪拍题/ }))
    fireEvent.click(await screen.findByRole('button', { name: '模拟清晰识别' }))
    fireEvent.change(screen.getByLabelText('对学习伙伴说'), { target: { value: '题干是对的' } })
    fireEvent.click(screen.getByRole('button', { name: '发送' }))

    expect(await screen.findByText('先看看这道题问的是总数还是其中一部分。')).toBeInTheDocument()
    expect(fetchSpy.mock.calls.some((call) => String(call[0]).includes('tutor-ocr-confirmations'))).toBe(true)
    expect(fetchSpy.mock.calls.some((call) => String(call[0]).endsWith('/tutor'))).toBe(true)
  })

  it('restores a confirmed local OCR review after a component restart', () => {
    vi.spyOn(localApi, 'mistakeCaptureQueues').mockResolvedValue([])
    const drafts = { study: { payload: { question, tutor_ocr_review: { artifactId: 'ocr-clear-1', text: question, status: 'confirmed', confirmationId: 'confirmation-clear-1', confirmationKind: 'ocr_verified', createdAt: new Date().toISOString(), confirmedAt: new Date().toISOString(), expiresAt: new Date(Date.now() + 60 * 60_000).toISOString() }, tutor_ocr_review_text: question }, updated_at: new Date().toISOString() } }
    render(<SessionWorkspace initial={workspace(drafts)} childName="小辰" childGrade="四年级" onBack={vi.fn()} />)
    expect(screen.getByText('题干已确认')).toBeInTheDocument()
    expect(screen.getByLabelText('输入学习问题')).toHaveValue(question)
  })

  it('cancels an in-flight OCR-backed tutor request while keeping the confirmed question', async () => {
    installSpeech()
    vi.spyOn(localApi, 'mistakeCaptureQueues').mockResolvedValue([])
    vi.stubGlobal('fetch', vi.fn().mockImplementation((url: string, init: RequestInit) => {
      if (url.includes('tutor-ocr-confirmations')) return Promise.resolve({ ok: true, json: async () => ({ confirmation_id: 'confirmation-clear-1', confirmed_text: question, confirmation_kind: 'ocr_verified', confirmed_at: new Date().toISOString(), expires_at: new Date(Date.now() + 60 * 60_000).toISOString() }) })
      if (url.includes('/drafts/study')) return Promise.resolve({ ok: true, json: async () => ({ section: 'study', payload: {}, updated_at: new Date().toISOString() }) })
      return new Promise((_resolve, reject) => {
      init.signal?.addEventListener('abort', () => reject(new DOMException('cancelled', 'AbortError')), { once: true })
      })
    }))
    render(<SessionWorkspace initial={workspace()} childName="小辰" childGrade="四年级" onBack={vi.fn()} />)
    fireEvent.click(screen.getByRole('button', { name: /用高拍仪拍题/ }))
    fireEvent.click(await screen.findByRole('button', { name: '模拟清晰识别' }))
    fireEvent.click(screen.getByRole('button', { name: '我已核对题干' }))
    await screen.findByText('题干已确认')
    fireEvent.click(screen.getByRole('button', { name: '提问' }))
    fireEvent.click(await screen.findByRole('button', { name: '取消本次讲解' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('已取消本次讲解，题干仍保留')
    expect(screen.getByLabelText('输入学习问题')).toHaveValue(question)
    expect(screen.getByText('题干已确认')).toBeInTheDocument()
  })

  it('does not restore a confirmation draft older than 24 hours', () => {
    vi.spyOn(localApi, 'mistakeCaptureQueues').mockResolvedValue([])
    const old = new Date(Date.now() - 25 * 60 * 60_000).toISOString()
    const drafts = { study: { payload: { question, tutor_ocr_review: { artifactId: 'ocr-clear-1', text: question, status: 'confirmed', confirmationId: 'old-confirmation', confirmationKind: 'ocr_verified', createdAt: old, confirmedAt: old, expiresAt: new Date(Date.now() + 60 * 60_000).toISOString() } }, updated_at: old } }
    render(<SessionWorkspace initial={workspace(drafts)} childName="小辰" childGrade="四年级" onBack={vi.fn()} />)
    expect(screen.queryByText('题干已确认')).not.toBeInTheDocument()
  })

  it('requires an explicit child confirmation before retrying an unknown tutor outcome', async () => {
    installSpeech()
    vi.spyOn(localApi, 'mistakeCaptureQueues').mockResolvedValue([])
    vi.spyOn(localApi, 'saveSessionDraft').mockResolvedValue({ section: 'study', payload: {}, updated_at: new Date().toISOString() } as never)
    const tutor = vi.spyOn(localApi, 'askTutor')
      .mockResolvedValueOnce({ ...tutorAnswer(), turn_id: 'parent-turn', hint_level: 1 })
      .mockRejectedValueOnce(new ApiError('上次讲解结果没有确认保存。再次重试可能会再次请求模型；请确认后再继续。', 409, 'TUTOR_RESULT_UNCONFIRMED', true, 'confirm_retry_may_repeat_model'))
      .mockResolvedValueOnce({ ...tutorAnswer(), turn_id: 'retry-turn', hint_level: 2, parent_turn_id: 'parent-turn' })
    render(<SessionWorkspace initial={workspace()} childName="小辰" childGrade="四年级" onBack={vi.fn()} />)

    fireEvent.change(screen.getByLabelText('输入学习问题'), { target: { value: question } })
    fireEvent.click(screen.getByRole('button', { name: '提问' }))
    await screen.findByText('先看看这道题问的是总数还是其中一部分。')
    fireEvent.click(screen.getByRole('button', { name: '再给一点提示' }))
    expect(await screen.findByRole('button', { name: '确认再次请求' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '重试本题' })).not.toBeInTheDocument()
    expect(tutor).toHaveBeenCalledTimes(2)

    fireEvent.click(screen.getByRole('button', { name: '确认再次请求' }))
    expect(await screen.findByText('先看看这道题问的是总数还是其中一部分。')).toBeInTheDocument()
    expect(tutor).toHaveBeenCalledTimes(3)
    expect(tutor.mock.calls[2].slice(0, 6)).toEqual(tutor.mock.calls[1].slice(0, 6))
    expect(tutor.mock.calls[2][6]).toBe(true)
  })
})
