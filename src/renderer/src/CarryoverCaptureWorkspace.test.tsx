import { beforeEach as beforeOwnedVoiceRuntimePolicyCase } from 'vitest'
import { ownedVoiceRuntimePolicyDesktop } from './test/ownedVoiceRuntimePolicy'

// This test host explicitly admits original epoch-bound capture policy.
beforeOwnedVoiceRuntimePolicyCase(() => { vi.stubGlobal('desktop', ownedVoiceRuntimePolicyDesktop()) })

import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { ApiError, localApi } from './api'
import { SessionWorkspace } from './SessionWorkspace'
import type { AgentTurn, SessionWorkspaceState } from './types'
import * as LocalInputs from './LocalInputs'
import { carryoverConfirmationAtArrival } from './pendingCarryoverReview'

beforeEach(() => {
  vi.spyOn(localApi, 'readVoiceInteractionHandoff').mockImplementation(async (session, child) => ({
    session_id: session, child_id: child, status: 'none', handoff_id: null, agent_action_id: null,
    deferred_progress: null, request: null, receipt: null, reason: null,
  }))
  vi.spyOn(localApi, 'invalidateVoiceInteractionHandoff').mockImplementation(async (session, child) => ({
    session_id: session, child_id: child, status: 'none', handoff_id: null, agent_action_id: null,
    deferred_progress: null, request: null, receipt: null, reason: null,
  }))
})


const workspace: SessionWorkspaceState = {
  session: {
    session_id: 'session-draft-1',
    child_id: 'child-junior-3',
    status: 'active',
    state: 'classroom_recall',
    started_at: '2026-08-26T10:00:00Z',
    last_checkpoint_at: '2026-08-26T10:00:00Z',
    observation_revision: 0, observation_generation: 'a'.repeat(64), observation_status: 'paused',
    snooze_until: null,
    recall_declared_empty: 0,
    homework_declared_empty: 0
  },
  family_rule: {
    child_id: 'child-junior-3',
    latest_end_time: '21:30',
    max_learning_minutes: 120,
    task_segment_minutes: 25,
    break_minutes: 5,
    pet_min_minutes: 0,
    competition_minutes: 0,
    observation_mode: 'shadow',
    reminder_budget_per_10m: 1,
    misclassification_shadow_threshold: 3,
    version: 1,
    updated_at: '2026-08-26T10:00:00Z'
  },
  planning_context: {
    energy_level: 'medium',
    fixed_commitment_minutes: 0,
    fixed_commitment_note: null,
    tomorrow_event_subject: null,
    tomorrow_event_title: null,
    tomorrow_event_minutes: 0,
    updated_at: '2026-08-26T10:00:00Z'
  },
  recalls: [],
  homework: [],
  carryover_candidates: [],
  carryover_decisions: [],
  plan: null,
  plan_items: [],
  daily_report_ready: false,
  latest_tutor_turn: null,
  sensor_release: {
    opened_leases: 0,
    active_leases: 0,
    released_leases: 0,
    application_release_confirmed: false,
    evidence_level: 'not_opened',
    os_release_verified: false,
    release_verification: null,
    by_device: {}
  },
  drafts: {
    classroom_recall: {
      payload: { subject: '数学', summary: '今天讲了相似三角形' },
      updated_at: '2026-08-26T10:03:00Z'
    }
  }
}

it.each(['delayed', 'explicit-null', 'restart-missing', 'fresh'] as const)('keeps capture identity through mounted carryover reconfirmation (%s)', async mode => {
  const first = {source_plan_item_id:'task-a', source_version:'a'.repeat(64), source_session_id:'old-session',
    source_date:'2026-09-08', subject:'数学', title:'原作业', expected_minutes:10, task_type:'written' as const,
    range_text:null, due_at:null, special_requirements:null, reschedule_reason:'时间不够'}
  const revised = {...first, source_version:'b'.repeat(64), title:'新的第六页作业'}
  const initial: SessionWorkspaceState = {...workspace, session:{...workspace.session, state:'homework_capture'},
    carryover_candidates:[first], drafts:{}}
  vi.spyOn(localApi, 'sessionWorkspace').mockResolvedValue({...initial, carryover_candidates:[revised]})
  const speech = vi.spyOn(LocalInputs, 'speakLocally').mockImplementation(() => undefined)
  let received!: Parameters<typeof LocalInputs.HandsFreeVoiceInput>[0]['onTranscript']
  vi.spyOn(LocalInputs, 'HandsFreeVoiceInput').mockImplementation(props => {
    received = props.onTranscript
    return <div data-testid="isolated-asr-boundary" />
  })
  let count = 0
  const agent = vi.spyOn(localApi, 'agentTurn').mockImplementation(async () => ({
    agent_turn_id:`late-asr-turn-${++count}`, transcript:'今天继续', intent:'carryover_continue_today',
    response_text:'好', action:{type:'review_first_carryover', decision:'continue_today'},
    orchestrator_version:'hybrid_companion_v1', tool_status:'pending', tool_error_code:null,
    completed_at:null, created_at:new Date().toISOString(),
  }))
  vi.spyOn(localApi, 'completeAgentTurn').mockResolvedValue({} as AgentTurn)
  const review = vi.spyOn(localApi, 'reviewDeferredTask')
    .mockRejectedValueOnce(new ApiError('原内容变更',409,'DEFERRED_TASK_SOURCE_CHANGED',false,undefined,
      {not_applied:true,session_id:initial.session.session_id,child_id:initial.session.child_id,
        client_action_id:'late-asr-turn-1:carryover',candidate:revised}))
    .mockResolvedValue({review_id:'new-review',source_plan_item_id:'task-a',source_session_id:'old-session',
      child_id:initial.session.child_id,target_session_id:initial.session.session_id,decision:'continue_today',
      reason:'孩子语音确认今天继续完成',target_homework_id:'new-homework',reviewed_at:new Date().toISOString()})
  const view = render(<SessionWorkspace initial={initial} childName="小辰" childGrade="初三" onBack={vi.fn()} />)
  // Only the ASR delivery seam is synthetic. The real submit/queue/action handler runs.
  let deliverOld!: () => void
  const oldAudioCompletion = new Promise<void>(resolve => { deliverOld = resolve })
  const oldAsr = oldAudioCompletion.then(() => received('今天继续', undefined, 'voice-captured-before-readback'))
  const input = screen.getByLabelText('对学习伙伴说')
  fireEvent.change(input, {target:{value:'今天继续'}})
  fireEvent.keyDown(input, {key:'Enter'})
  await waitFor(() => expect(speech.mock.calls.some(([text]) => text.includes('新的第六页作业'))).toBe(true))
  const readback = speech.mock.calls.find(([text]) => text.includes('新的第六页作业'))!
  await act(async () => { readback[4]?.onPlaybackEnd?.() })
  if (mode === 'restart-missing') {
    const oldReceiver = received
    view.unmount()
    render(<SessionWorkspace initial={initial} childName="小辰" childGrade="初三" onBack={vi.fn()} />)
    // Deliver only when the new mount has passed its real policy/readiness gate.
    await waitFor(() => expect(received).not.toBe(oldReceiver))
  }
  if (mode === 'fresh') {
    const capturedToken = carryoverConfirmationAtArrival(initial.session.session_id, initial.session.child_id)
    expect(capturedToken).toBeTruthy()
    await act(async () => { await received('今天继续', undefined, 'voice-captured-before-readback', undefined, false, capturedToken) })
  } else if (mode === 'explicit-null') {
    await act(async () => { await received('今天继续', undefined, 'voice-captured-before-readback', undefined, false, null) })
  } else await act(async () => { deliverOld(); await oldAsr })
  expect(agent).toHaveBeenCalledTimes(2)
  expect(agent.mock.calls[1]?.[3]).toBe('voice-captured-before-readback')
  expect(review).toHaveBeenCalledTimes(mode === 'fresh' ? 2 : 1)
  if (mode === 'fresh') {
    expect(review.mock.calls[1]?.slice(0, 2)).toEqual([initial.session.session_id, 'task-a'])
    expect(review.mock.calls[1]).toContain(revised.source_version)
    expect(review.mock.calls[1]).toContain('late-asr-turn-2:carryover')
  }
})

afterEach(() => {
  cleanup()
  localStorage.clear()
  vi.useRealTimers()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})
