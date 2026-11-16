import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError, localApi } from './api'
import { SessionWorkspace } from './SessionWorkspace'
import { pendingAgentRequests, saveAgentRequest, AGENT_REQUEST_UNCONFIRMED } from './agentRequestJournal'
import { webcrypto } from 'node:crypto'
import { submitNativeWriting } from './nativeWritingFlow'
import type { AgentTurn, SessionWorkspaceState, TutorTurn } from './types'

const sessionId = 'agent-recovery-session'
const recoveryQuestion = '这道数学题为什么要先列式？'

beforeEach(() => {
  vi.spyOn(localApi, 'readVoiceInteractionHandoff').mockImplementation(async (session, child) => ({
    session_id: session, child_id: child, status: 'none', handoff_id: null, agent_action_id: null,
    deferred_progress: null, request: null, receipt: null, reason: null,
  }))
  vi.spyOn(localApi, 'invalidateVoiceInteractionHandoff').mockImplementation(async (session, child) => ({
    session_id: session, child_id: child, status: 'none', handoff_id: null, agent_action_id: null,
    deferred_progress: null, request: null, receipt: null, reason: null,
  }))
  vi.spyOn(localApi, 'agentExecutionRecoveryIndex').mockImplementation(async (session, child) => ({
    session_id: session, child_id: child, local_only: true, cloud_transmission_allowed: false,
    replay_allowed: false, has_more: false, entries: [] }))
})

describe('ordinary voice request delivery recovery', () => {
  it('discovers a crash-lost saved answer from SQLite metadata before read-only receipt checking', async () => {
    installSpeech()
    vi.spyOn(localApi, 'agentExecutionRecoveryIndex').mockResolvedValue({ child_id: 'child-primary-4', session_id: sessionId,
      local_only: true, cloud_transmission_allowed: false, replay_allowed: false, has_more: false,
      entries: [{ agent_turn_id: 'saved-original-turn', client_action_id: 'saved-original-action', created_at: '2026-10-07T13:58:13.421Z' }] })
    const interpret = vi.spyOn(localApi, 'agentTurn'), execute = vi.spyOn(localApi, 'executeAgentTurn')
    const read = vi.spyOn(localApi, 'agentRequestReceipt').mockResolvedValue({ session_id: sessionId,
      client_action_id: 'saved-original-action', status: 'interpreted', response_text: '原答案已经保存，后续操作还没有完成回执。',
      local_only: true, cloud_transmission_allowed: false, replay_allowed: false, mastery_update_allowed: false })
    render(<SessionWorkspace initial={studyWorkspace()} childName="小辰" childGrade="四年级" onBack={vi.fn()} />)
    await screen.findAllByText(/上次请求已被理解/)
    const input = screen.getByLabelText('对学习伙伴说')
    fireEvent.change(input, { target: { value: '核对刚才请求' } }); fireEvent.keyDown(input, { key: 'Enter' })
    await screen.findAllByText('原答案已经保存，后续操作还没有完成回执。')
    expect(read).toHaveBeenCalledWith(sessionId, 'saved-original-action', expect.any(AbortSignal))
    expect(interpret).not.toHaveBeenCalled(); expect(execute).not.toHaveBeenCalled()
  })
  it('dispatches a semantic original-receipt inquiry through the real voice handler without capture, answer or evaluation POST', async () => {
    installSpeech(); vi.stubGlobal('crypto',webcrypto)
    const scope={child_id:'child-primary-4',session_id:sessionId,prompt_id:'native-original-prompt',item_version:'original-v1',diagnostic_item_id:'writing-item'}
    const capture=vi.fn(async()=>{throw Error('lost capture receipt')}), answer=vi.fn()
    await submitNativeWriting({scope,text:'Saved native draft.',isCurrent:()=>true,capture,answer}).catch(()=>undefined)
    const hash=Buffer.from(await webcrypto.subtle.digest('SHA-256',Buffer.from('Saved native draft.'))).toString('hex')
    const interpret=vi.spyOn(localApi,'agentTurn').mockResolvedValue({...agentTurn(),requires_control_admission:true,
      runtime_capability:'native_writing.submission_status',runtime_execution_location:'client_device_bridge',after_execution:'stop',
      action:{type:'check_native_writing_submission',source:'content_free_voice_control',native_writing_target:{...scope,text_sha256:hash}}})
    vi.spyOn(localApi,'admitVoiceControlExecution').mockResolvedValue({status:'admitted'})
    vi.spyOn(localApi,'completeAgentTurn').mockImplementation(acknowledgeTurn)
    const execute=vi.spyOn(localApi,'executeAgentTurn')
    const read=vi.spyOn(localApi,'nativeWritingReceipt').mockImplementation(async (_sid,query)=>({...query,state:'capture_unknown',
      original:null,result:null,local_only:true,cloud_transmission_allowed:false}))
    render(<SessionWorkspace initial={studyWorkspace()} childName="小辰" childGrade="四年级" onBack={vi.fn()} />)
    const input=screen.getByLabelText('对学习伙伴说')
    fireEvent.change(input,{target:{value:'刚才作文交上了吗'}});fireEvent.keyDown(input,{key:'Enter'})
    await waitFor(()=>expect(read).toHaveBeenCalledTimes(1))
    expect((await screen.findAllByText('原稿提交的结果仍未确认，原编号已保留；没有重新采集、提交或评价。')).length).toBeGreaterThan(0)
    expect(capture).toHaveBeenCalledTimes(1);expect(answer).not.toHaveBeenCalled()
    expect(interpret).toHaveBeenCalledTimes(1);expect(execute).not.toHaveBeenCalled()
  })
  it('restores pending delivery and checks by voice input without planning or executing again', async () => {
    installSpeech()
    const original = saveAgentRequest(sessionId, 'lost-original-request')
    const interpret = vi.spyOn(localApi, 'agentTurn')
    const execute = vi.spyOn(localApi, 'executeAgentTurn')
    const read = vi.spyOn(localApi, 'agentRequestReceipt').mockResolvedValue({
      session_id: sessionId, client_action_id: 'lost-original-request', status: 'interpreted',
      response_text: '原请求的理解结果已保存，但后续操作还没有完成回执。',
      local_only: true, cloud_transmission_allowed: false, replay_allowed: false, mastery_update_allowed: false,
    })
    const first = render(<SessionWorkspace initial={studyWorkspace()} childName="小辰" childGrade="四年级" onBack={vi.fn()} />)
    expect(screen.getAllByText(AGENT_REQUEST_UNCONFIRMED).length).toBeGreaterThan(0)
    const input = screen.getByLabelText('对学习伙伴说')
    fireEvent.change(input, { target: { value: '核对刚才请求' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() => expect(read).toHaveBeenCalledWith(sessionId, 'lost-original-request', expect.any(AbortSignal)))
    expect((await screen.findAllByText('原请求的理解结果已保存，但后续操作还没有完成回执。')).length).toBeGreaterThan(0)
    expect(interpret).not.toHaveBeenCalled()
    expect(execute).not.toHaveBeenCalled()
    expect(pendingAgentRequests(sessionId)).toEqual([original])
    first.unmount()
    render(<SessionWorkspace initial={studyWorkspace()} childName="小辰" childGrade="四年级" onBack={vi.fn()} />)
    expect(screen.getAllByText(AGENT_REQUEST_UNCONFIRMED).length).toBeGreaterThan(0)
    read.mockResolvedValueOnce({ session_id: sessionId, client_action_id: original.actionId, status: 'recorded_completed',
      response_text: '原操作已完成，没有重新执行。', local_only: true, cloud_transmission_allowed: false,
      replay_allowed: false, mastery_update_allowed: false })
    const resumed = screen.getByLabelText('对学习伙伴说')
    fireEvent.change(resumed, { target: { value: '核对刚才请求' } })
    fireEvent.keyDown(resumed, { key: 'Enter' })
    await waitFor(() => expect(pendingAgentRequests(sessionId)).toEqual([]))
    expect(read).toHaveBeenCalledTimes(2)
    expect(interpret).not.toHaveBeenCalled()
    expect(execute).not.toHaveBeenCalled()
  })

  it('does not tell the child to repeat an answer whose submission outcome is unknown', async () => {
    installSpeech()
    vi.spyOn(localApi, 'agentTurn').mockRejectedValue(new ApiError(AGENT_REQUEST_UNCONFIRMED, undefined, 'AGENT_REQUEST_UNCONFIRMED'))
    const execute = vi.spyOn(localApi, 'executeAgentTurn')
    render(<SessionWorkspace initial={studyWorkspace()} childName="小辰" childGrade="四年级" onBack={vi.fn()} />)
    const input = screen.getByLabelText('对学习伙伴说')
    fireEvent.change(input, { target: { value: '我算出的答案是十二' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    expect((await screen.findAllByText(AGENT_REQUEST_UNCONFIRMED)).length).toBeGreaterThan(0)
    expect(screen.queryByText('我刚才没有处理成功，请再说一次。')).not.toBeInTheDocument()
    expect(execute).not.toHaveBeenCalled()
  })
})

function studyWorkspace(drafts: SessionWorkspaceState['drafts'] = {}): SessionWorkspaceState {
  return {
    session: {
      session_id: sessionId, child_id: 'child-primary-4', status: 'active', state: 'study',
      started_at: '2026-08-28T10:00:00Z', last_checkpoint_at: '2026-08-28T10:00:00Z',
      observation_revision: 0, observation_generation: 'a'.repeat(64), observation_status: 'paused', snooze_until: null, recall_declared_empty: 0, homework_declared_empty: 0
    },
    family_rule: {
      child_id: 'child-primary-4', latest_end_time: '21:00', max_learning_minutes: 90,
      task_segment_minutes: 25, break_minutes: 5, pet_min_minutes: 0, competition_minutes: 0,
      observation_mode: 'shadow', reminder_budget_per_10m: 1, misclassification_shadow_threshold: 3,
      version: 1, updated_at: '2026-08-28T10:00:00Z'
    },
    planning_context: {
      energy_level: 'medium', fixed_commitment_minutes: 0, fixed_commitment_note: null,
      tomorrow_event_subject: null, tomorrow_event_title: null, tomorrow_event_minutes: 0,
      updated_at: '2026-08-28T10:00:00Z'
    },
    recalls: [], homework: [], carryover_candidates: [], carryover_decisions: [],
    plan: { plan_id: 'plan-agent-recovery', plan_date: '2026-08-28', status: 'active', total_minutes: 25, generator: 'local_rule_engine', version: 1 },
    plan_items: [{
      plan_item_id: 'plan-item-agent-recovery', homework_id: null, subject: '数学', title: '数学练习',
      expected_minutes: 25, actual_minutes: null, priority: 1, status: 'pending', position: 1,
      started_at: null, completed_at: null, active_seconds: 0, active_segment_started_at: null,
      source_type: 'school_homework', source_ref: null, completion_standard: '完成并检查',
      cognitive_load: 'medium', task_type: 'written', completion_evidence_type: null,
      completion_note: null, completion_checklist: [], completion_artifact_id: null
    }],
    daily_report_ready: false,
    latest_tutor_turn: null,
    sensor_release: {
      opened_leases: 0, active_leases: 0, released_leases: 0,
      application_release_confirmed: false, evidence_level: 'not_opened',
      os_release_verified: false, release_verification: null, by_device: {}
    },
    drafts
  }
}

function tutorTurn(answer = '先找出题目给了什么，再把数量关系写成式子。'): TutorTurn {
  return {
    turn_id: 'tutor-turn-retry', session_id: sessionId, child_id: 'child-primary-4',
    plan_item_id: 'plan-item-agent-recovery', subject: '数学', question: recoveryQuestion,
    answer, provider: 'local', model: 'test', status: 'completed', hint_level: 1,
    parent_turn_id: null, response_kind: 'hint', verification_status: 'local_only',
    safety_status: 'passed', strategy_experiment_id: null, strategy_version: null,
    output_envelope: null, created_at: '2026-08-28T10:00:00Z'
  }
}

function agentTurn(): AgentTurn {
  return {
    agent_turn_id: 'agent-turn-recovery', transcript: recoveryQuestion, intent: 'tutor_question',
    response_text: '我先给你一个小提示，不直接替你做完。',
    action: { type: 'ask_tutor', question: recoveryQuestion }, orchestrator_version: 'local_intent_v3',
    tool_status: 'pending', tool_error_code: null, completed_at: null, created_at: '2026-08-28T10:00:00Z'
  }
}

const acknowledgeTurn: typeof localApi.completeAgentTurn = async (id, response, status = 'completed', error) => ({
  ...agentTurn(), agent_turn_id: id, response_text: response, tool_status: status,
  tool_error_code: error ?? null, completed_at: new Date().toISOString(),
})

function installSpeech(): void {
  class FakeUtterance { constructor(readonly text: string) {} voice: SpeechSynthesisVoice | null = null }
  vi.stubGlobal('SpeechSynthesisUtterance', FakeUtterance)
  vi.stubGlobal('speechSynthesis', { cancel: vi.fn(), getVoices: () => [], speak: vi.fn() })
}

afterEach(() => {
  cleanup()
  localStorage.clear()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('SessionWorkspace Agent tutor recovery', () => {
  beforeEach(() => {
    vi.spyOn(localApi, 'saveSessionDraft').mockImplementation(async (_session, section, payload) => ({ section, payload, updated_at: new Date().toISOString() }))
  })
  it('keeps and restores the newer unsent input when the previous request receipt finishes', async () => {
    installSpeech()
    let release!: (value: Awaited<ReturnType<typeof localApi.completeAgentTurn>>) => void
    const receipt = new Promise<Awaited<ReturnType<typeof localApi.completeAgentTurn>>>(resolve => { release = resolve })
    vi.spyOn(localApi, 'agentTurn').mockResolvedValue({ ...agentTurn(), action: { type: 'none' } })
    const complete = vi.spyOn(localApi, 'completeAgentTurn').mockImplementation(() => receipt)
    const clear = vi.spyOn(localApi, 'clearSessionDraft').mockResolvedValue({ section: 'agent_command', deleted: true })
    const first = render(<SessionWorkspace initial={studyWorkspace()} childName="小辰" childGrade="四年级" onBack={vi.fn()} />)
    const input = screen.getByLabelText('对学习伙伴说')
    fireEvent.change(input, { target: { value: recoveryQuestion } })
    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() => expect(complete).toHaveBeenCalledTimes(1))
    const newer = '来一道校内题，先保留这句，还没有提交。'
    fireEvent.change(input, { target: { value: newer } })
    await act(async () => { release(await acknowledgeTurn(...complete.mock.calls[0])) })
    await waitFor(() => expect(document.querySelector('.agent-dialogue span')?.textContent).toBe(agentTurn().response_text))
    expect(input).toHaveValue(newer)
    await waitFor(() => expect(localApi.saveSessionDraft).toHaveBeenCalledWith(sessionId, 'agent_command', expect.objectContaining({ text: newer })))
    expect(clear).not.toHaveBeenCalled()
    const saved = vi.mocked(localApi.saveSessionDraft).mock.calls.filter(([, section, payload]) => section === 'agent_command' && payload.text === newer).at(-1)![2]
    first.unmount()
    render(<SessionWorkspace initial={studyWorkspace({agent_command:{payload:saved,updated_at:new Date().toISOString()}})} childName="小辰" childGrade="四年级" onBack={vi.fn()} />)
    expect(screen.getByLabelText('对学习伙伴说')).toHaveValue(newer)
  })

  it('keeps and saves input typed while the previous draft clear is in flight', async () => {
    installSpeech()
    let release!: (value: { section: 'agent_command'; deleted: boolean }) => void
    const pending = new Promise<{ section: 'agent_command'; deleted: boolean }>(resolve => { release = resolve })
    vi.spyOn(localApi, 'agentTurn').mockResolvedValue({ ...agentTurn(), action: { type: 'none' } })
    vi.spyOn(localApi, 'completeAgentTurn').mockImplementation(acknowledgeTurn)
    const clear = vi.spyOn(localApi, 'clearSessionDraft').mockImplementation(() => pending)
    render(<SessionWorkspace initial={studyWorkspace()} childName="小辰" childGrade="四年级" onBack={vi.fn()} />)
    const input = screen.getByLabelText('对学习伙伴说')
    fireEvent.change(input, { target: { value: recoveryQuestion } })
    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() => expect(clear).toHaveBeenCalledTimes(1))
    const newer = '刚输入的新问题，不能被上一条清理删掉。'
    fireEvent.change(input, { target: { value: newer } })
    await act(async () => { release({section:'agent_command',deleted:true}) })
    expect(input).toHaveValue(newer)
    await waitFor(() => expect(localApi.saveSessionDraft).toHaveBeenCalledWith(sessionId, 'agent_command', expect.objectContaining({ text: newer })))
    expect(clear).toHaveBeenCalledTimes(1)
  })

  it('shows a child-safe recovery card and persists its retry metadata after a real tool failure', async () => {
    installSpeech()
    vi.spyOn(localApi, 'agentTurn').mockResolvedValue(agentTurn())
    vi.spyOn(localApi, 'askTutor').mockRejectedValue(new ApiError('讲解服务暂不可用', 503, 'MODEL_NOT_CONFIGURED'))
    const complete = vi.spyOn(localApi, 'completeAgentTurn').mockImplementation(acknowledgeTurn)
    const saveDraft = vi.spyOn(localApi, 'saveSessionDraft').mockResolvedValue({ section: 'agent_command', payload: {}, updated_at: '2026-08-28T10:01:00Z' })

    render(<SessionWorkspace initial={studyWorkspace()} childName="小辰" childGrade="四年级" onBack={vi.fn()} />)
    fireEvent.change(screen.getByLabelText('对学习伙伴说'), { target: { value: recoveryQuestion } })
    fireEvent.click(screen.getByRole('button', { name: '发送' }))

    expect(await screen.findByRole('button', { name: '重试讲解' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '请家长帮忙' })).toBeInTheDocument()
    expect(screen.queryByText(/内部错误|MODEL_NOT_CONFIGURED|JSON/)).not.toBeInTheDocument()
    await waitFor(() => expect(complete).toHaveBeenCalledWith(
      'agent-turn-recovery', expect.stringContaining('重试讲解'), 'failed', 'CLIENT_TOOL_ACTION_FAILED'
    ))
    await waitFor(() => expect(saveDraft).toHaveBeenCalledWith(sessionId, 'agent_command', expect.objectContaining({
      text: recoveryQuestion,
      tutor_recovery: expect.objectContaining({ turnId: 'agent-turn-recovery', actionId: expect.any(String), subject: '数学', question: recoveryQuestion, attempts: 1 })
    })))
  })

  it('restores the persisted recovery card after a component refresh', () => {
    const drafts = {
      agent_command: {
        payload: {
          text: recoveryQuestion,
          tutor_recovery: { version: 1, turnId: 'agent-turn-recovery', actionId: 'agent-tutor-action-1', subject: '数学', question: recoveryQuestion, createdAt: new Date().toISOString(), attempts: 1 }
        }, updated_at: new Date().toISOString()
      }
    }
    const { unmount } = render(<SessionWorkspace initial={studyWorkspace(drafts)} childName="小辰" childGrade="四年级" onBack={vi.fn()} />)
    expect(screen.getByRole('button', { name: '重试讲解' })).toBeInTheDocument()
    expect(screen.getByLabelText('对学习伙伴说')).toHaveValue(recoveryQuestion)

    unmount()
    render(<SessionWorkspace initial={studyWorkspace(drafts)} childName="小辰" childGrade="四年级" onBack={vi.fn()} />)
    expect(screen.getByRole('button', { name: '重试讲解' })).toBeInTheDocument()
    expect(screen.getAllByText(/讲解暂时没有完成/).length).toBeGreaterThan(0)
  })

  it('clears the durable draft only after a successful retry', async () => {
    installSpeech()
    const drafts = {
      agent_command: {
        payload: {
          text: recoveryQuestion,
          tutor_recovery: { version: 1, turnId: 'agent-turn-recovery', actionId: 'agent-tutor-action-1', subject: '数学', question: recoveryQuestion, createdAt: new Date().toISOString(), attempts: 1 }
        }, updated_at: new Date().toISOString()
      }
    }
    const tutor = vi.spyOn(localApi, 'askTutor').mockResolvedValue(tutorTurn())
    const complete = vi.spyOn(localApi, 'completeAgentTurn').mockImplementation(acknowledgeTurn)
    const clearDraft = vi.spyOn(localApi, 'clearSessionDraft').mockResolvedValue({ section: 'agent_command', deleted: true })

    render(<SessionWorkspace initial={studyWorkspace(drafts)} childName="小辰" childGrade="四年级" onBack={vi.fn()} />)
    fireEvent.click(screen.getByRole('button', { name: '重试讲解' }))

    await waitFor(() => expect(tutor).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(complete).toHaveBeenCalledWith(
      'agent-turn-recovery', expect.any(String), 'completed', undefined,
      expect.objectContaining({
        execution_receipt: expect.objectContaining({
          capability: 'learning.explain', execution_location: 'local_model', action_type: 'ask_tutor'
        })
      })
    ))
    await waitFor(() => expect(clearDraft).toHaveBeenCalledWith(sessionId, 'agent_command'))
    expect(document.querySelector('.agent-dialogue span')?.textContent).toBe(tutorTurn().answer)
    expect((await screen.findAllByText('先找出题目给了什么，再把数量关系写成式子。')).length).toBeGreaterThan(0)
    expect(screen.queryByRole('button', { name: '重试讲解' })).not.toBeInTheDocument()
    expect(screen.getByLabelText('对学习伙伴说')).toHaveValue('')
  })

  it('lets the child retry a persisted tutor failure entirely by voice', async () => {
    installSpeech()
    const drafts = {
      agent_command: {
        payload: {
          text: recoveryQuestion,
          tutor_recovery: { version: 1, turnId: 'agent-turn-recovery', actionId: 'agent-tutor-action-1', subject: '数学', question: recoveryQuestion, createdAt: new Date().toISOString(), attempts: 1 }
        }, updated_at: new Date().toISOString()
      }
    }
    const tutor = vi.spyOn(localApi, 'askTutor').mockResolvedValue(tutorTurn())
    vi.spyOn(localApi, 'completeAgentTurn').mockImplementation(acknowledgeTurn)
    vi.spyOn(localApi, 'clearSessionDraft').mockResolvedValue({ section: 'agent_command', deleted: true })
    const route = vi.spyOn(localApi, 'agentTurn')

    render(<SessionWorkspace initial={studyWorkspace(drafts)} childName="小辰" childGrade="四年级" onBack={vi.fn()} />)
    expect(await screen.findByText(/你可以说“重试讲解”/, { selector: '.agent-dialogue span' })).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('对学习伙伴说'), { target: { value: '重试讲解' } })
    fireEvent.click(screen.getByRole('button', { name: '发送' }))

    expect((await screen.findAllByText('先找出题目给了什么，再把数量关系写成式子。')).length).toBeGreaterThan(0)
    expect(tutor).toHaveBeenCalledTimes(1)
    expect(route).not.toHaveBeenCalled()
    expect(screen.queryByRole('button', { name: '重试讲解' })).not.toBeInTheDocument()
  })

  it('requires an explicit child confirmation before an agent-command retry can call the tutor again', async () => {
    installSpeech()
    vi.spyOn(localApi, 'agentTurn').mockResolvedValue(agentTurn())
    const tutor = vi.spyOn(localApi, 'askTutor')
      .mockRejectedValueOnce(new ApiError('上次讲解结果没有确认保存。', 409, 'TUTOR_RESULT_UNCONFIRMED', true, 'confirm_retry_may_repeat_model'))
      .mockResolvedValueOnce(tutorTurn())
    vi.spyOn(localApi, 'completeAgentTurn').mockImplementation(acknowledgeTurn)
    const saveDraft = vi.spyOn(localApi, 'saveSessionDraft').mockResolvedValue({ section: 'agent_command', payload: {}, updated_at: new Date().toISOString() })
    vi.spyOn(localApi, 'clearSessionDraft').mockResolvedValue({ section: 'agent_command', deleted: true })

    render(<SessionWorkspace initial={studyWorkspace()} childName="小辰" childGrade="四年级" onBack={vi.fn()} />)
    fireEvent.change(screen.getByLabelText('对学习伙伴说'), { target: { value: recoveryQuestion } })
    fireEvent.click(screen.getByRole('button', { name: '发送' }))

    expect(await screen.findByRole('button', { name: '确认再次请求' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '重试讲解' })).not.toBeInTheDocument()
    expect(tutor).toHaveBeenCalledTimes(1)
    await waitFor(() => expect(saveDraft).toHaveBeenCalledWith(sessionId, 'agent_command', expect.objectContaining({
      tutor_recovery: expect.objectContaining({ requiresExplicitRetryConfirmation: true })
    })))

    fireEvent.click(screen.getByRole('button', { name: '确认再次请求' }))
    expect((await screen.findAllByText('先找出题目给了什么，再把数量关系写成式子。')).length).toBeGreaterThan(0)
    expect(tutor).toHaveBeenCalledTimes(2)
    expect(tutor.mock.calls[1].slice(0, 6)).toEqual(tutor.mock.calls[0].slice(0, 6))
    expect(tutor.mock.calls[1][6]).toBe(true)
  })

  it('restores the unknown-outcome recovery card and can abandon its old action identity', async () => {
    const drafts = {
      agent_command: {
        payload: {
          text: recoveryQuestion,
          tutor_recovery: {
            version: 1, turnId: 'agent-turn-recovery', actionId: 'agent-tutor-action-unknown', subject: '数学', question: recoveryQuestion,
            createdAt: new Date().toISOString(), attempts: 1, requiresExplicitRetryConfirmation: true,
          }
        }, updated_at: new Date().toISOString()
      }
    }
    const clearDraft = vi.spyOn(localApi, 'clearSessionDraft').mockResolvedValue({ section: 'agent_command', deleted: true })
    const { unmount } = render(<SessionWorkspace initial={studyWorkspace(drafts)} childName="小辰" childGrade="四年级" onBack={vi.fn()} />)
    expect(screen.getByRole('button', { name: '确认再次请求' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '重试讲解' })).not.toBeInTheDocument()
    unmount()

    render(<SessionWorkspace initial={studyWorkspace(drafts)} childName="小辰" childGrade="四年级" onBack={vi.fn()} />)
    fireEvent.click(screen.getByRole('button', { name: '保留问题，改为新请求' }))
    await waitFor(() => expect(localApi.saveSessionDraft).toHaveBeenCalledWith(sessionId, 'agent_command', { text: recoveryQuestion, tutor_recovery: null }))
    expect(clearDraft).not.toHaveBeenCalled()
    expect(screen.queryByRole('button', { name: '确认再次请求' })).not.toBeInTheDocument()
    expect(screen.getByLabelText('对学习伙伴说')).toHaveValue(recoveryQuestion)
  })
})
