import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { localApi } from './api'
import { SessionWorkspace } from './SessionWorkspace'
import * as LocalInputs from './LocalInputs'
import type { AgentTurn, SessionWorkspaceState, ControlledContentPlayback, ControlledGameSession } from './types'

import type { ControlledLaunchScope, ControlledLaunchIntent } from '../../shared/controlledLaunch'

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

// Explicit per-host native contract fixture; never installed for missing-native cases.
function nativeLaunchFixture() {
  const intents = new Map<string, ControlledLaunchIntent>()
  return vi.fn(async (scope: ControlledLaunchScope): Promise<ControlledLaunchIntent> => {
    const key = JSON.stringify([scope.childId, scope.sessionId, scope.kind, scope.requestId])
    let intent = intents.get(key)
    if (!intent) {
      intent = {...scope, intentId: crypto.randomUUID(), activityId: null, state: 'prepared'}
      intents.set(key, intent)
    }
    return intent
  })
}

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

const studyWorkspace: SessionWorkspaceState = {
  ...workspace,
  session: { ...workspace.session, state: 'study' },
  plan: {
    plan_id: 'plan-1', plan_date: '2026-08-26', status: 'active', total_minutes: 25,
    generator: 'local_rule_engine', version: 1
  },
  plan_items: [{
    plan_item_id: 'plan-item-1', homework_id: null, subject: '数学', title: '二次方程作业',
    expected_minutes: 25, actual_minutes: null, priority: 1, status: 'pending', position: 1,
    started_at: null, completed_at: null, active_seconds: 0, active_segment_started_at: null,
    source_type: 'school_homework', source_ref: null, completion_standard: '完成并检查',
    cognitive_load: 'medium', task_type: 'written', completion_evidence_type: null,
    completion_note: null, completion_checklist: [], completion_artifact_id: null
  }],
  drafts: {}
}


afterEach(() => { cleanup(); localStorage.clear(); vi.restoreAllMocks(); vi.unstubAllGlobals() })

function deferred<T>() {
  let resolve!: (v:T) => void
  let reject!: (v:unknown) => void
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}

async function host(kind: 'video' | 'game', restored = false) {
  vi.spyOn(LocalInputs, 'speakLocally').mockImplementation(() => undefined)
  vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('NETWORK_FORBIDDEN')))
  vi.spyOn(localApi, 'sessionWorkspace').mockResolvedValue(studyWorkspace)
  vi.spyOn(localApi, 'activeControlledContent').mockResolvedValue({playback:null})
  vi.spyOn(localApi, 'activeControlledGame').mockResolvedValue({game:null})
  const make = (id:string) => ({content_session_id:id, game_session_id:id, title:id,
    child_id:studyWorkspace.session.child_id, session_id:studyWorkspace.session.session_id,
    local_path:'/tmp/synthetic-approved.mp4', executable_path:'C:\\synthetic.exe', remaining_seconds:60, allowed_minutes:1,
    started_at:new Date().toISOString(), status:'active', launch_allowed:true, task_resumed:false}) as unknown as ControlledContentPlayback & ControlledGameSession
  const old = make('original-activity'), current = make('new-activity')
  const firstEnd = deferred<typeof old>(), secondEnd = deferred<typeof current>()
  const start = kind === 'video' ? vi.spyOn(localApi, 'startControlledContent') : vi.spyOn(localApi, 'startControlledGame')
  if (restored) {
    if (kind === 'video') vi.mocked(localApi.activeControlledContent).mockResolvedValue({playback:old})
    else vi.mocked(localApi.activeControlledGame).mockResolvedValue({game:old})
    start.mockResolvedValueOnce(current)
  } else start.mockResolvedValueOnce(old).mockResolvedValueOnce(current)
  vi.spyOn(localApi, 'bindControlledGameProcess').mockImplementation(async (_sid, id) => id === old.game_session_id ? old : current)
  const end = kind === 'video' ? vi.spyOn(localApi, 'endControlledContent') : vi.spyOn(localApi, 'endControlledGame')
  end.mockImplementation(async (_sid, _reason, _seconds, id) => id === 'original-activity' ? firstEnd.promise : secondEnd.promise)
  let closed!: (reason:string, id:string) => void
  const close = vi.fn(async (_reason:string, id:string) => ({closed:true, sessionId:id}))
  const open = vi.fn().mockResolvedValue({opened:true, processId:123})
  vi.stubGlobal('desktop', {
    prepareControlledLaunch: nativeLaunchFixture(),
    listControlledLaunches: vi.fn().mockResolvedValue([]),
    [kind === 'video' ? 'openControlledMedia' : 'openControlledGame']:open,
    [kind === 'video' ? 'closeControlledMedia' : 'closeControlledGame']:close,
    [kind === 'video' ? 'onControlledMediaClosed' : 'onControlledGameClosed']:vi.fn(callback => {closed=callback; return vi.fn()}),
  })
  let count = 0
  vi.spyOn(localApi, 'agentTurn').mockImplementation(async (_sid, transcript) => {
    const stop = transcript === '停止活动'
    const type = kind === 'video' ? stop ? 'stop_controlled_content' : 'start_controlled_content'
      : stop ? 'stop_controlled_game' : 'start_controlled_game'
    return {agent_turn_id:`media-turn-${++count}`, transcript, intent:type, action:{type, query:'math'},
      response_text:'已处理', orchestrator_version:'hybrid_companion_v1', tool_status:'pending',
      tool_error_code:null, completed_at:null, created_at:new Date().toISOString()} as AgentTurn
  })
  const complete = vi.spyOn(localApi, 'completeAgentTurn').mockResolvedValue({} as AgentTurn)
  const view = render(<SessionWorkspace initial={studyWorkspace} childName="小辰" childGrade="初三" onBack={vi.fn()} />)
  async function command(text:string, expected?:number) {
    fireEvent.change(screen.getByLabelText('对学习伙伴说'), {target:{value:text}})
    fireEvent.keyDown(screen.getByLabelText('对学习伙伴说'), {key:'Enter'})
    if (expected) await waitFor(() => expect(complete).toHaveBeenCalledTimes(expected))
  }
  if (restored) await waitFor(() => expect(end).toHaveBeenCalledTimes(1))
  else {
    await command('打开第一个活动', 1)
    await act(async () => { closed('window_closed', 'original-activity') })
    expect(end).toHaveBeenCalledTimes(1)
  }
  await command('打开第二个活动', restored ? 1 : 2)
  expect(open).toHaveBeenCalledTimes(restored ? 1 : 2)
  return {kind, old, current, firstEnd, secondEnd, end, close, closed, view, complete, command}
}

it.each(['video', 'game'] as const)('a pending old %s end cannot swallow the new native closed event', async kind => {
  const h = await host(kind)
  await act(async () => {h.closed('window_closed', 'new-activity')})
  expect(h.end).toHaveBeenCalledTimes(2)
  expect(h.end.mock.calls.map(call => call[3])).toEqual(['original-activity','new-activity'])
  await act(async () => {
    h.firstEnd.resolve({...h.old,status:'stopped'})
    h.secondEnd.resolve({...h.current,status:'stopped'})
  })
  expect(fetch).not.toHaveBeenCalled()
})

it.each(['video', 'game'] as const)('old %s finally must not release the new stop lock', async kind => {
  const h = await host(kind)
  await h.command('停止活动')
  await waitFor(() => expect(h.end).toHaveBeenCalledTimes(2))
  await act(async () => { h.firstEnd.resolve({...h.old,status:'stopped'}) })
  await act(async () => { h.closed('window_closed', 'new-activity'); h.closed('window_closed', 'new-activity') })
  expect(h.end).toHaveBeenCalledTimes(2)
  await act(async () => { h.secondEnd.resolve({...h.current,status:'stopped'}) })
  await waitFor(() => expect(h.complete).toHaveBeenCalledTimes(3))
})

it.each(['video', 'game'] as const)('unmount closes the current %s while a different activity end is pending', async kind => {
  const h = await host(kind)
  h.view.unmount()
  await waitFor(() => expect(h.close).toHaveBeenCalledWith('workspace_closed', 'new-activity'))
  await waitFor(() => expect(h.end).toHaveBeenCalledTimes(2))
  await act(async () => {
    h.firstEnd.resolve({...h.old,status:'stopped'})
    h.secondEnd.resolve({...h.current,status:'stopped'})
  })
})

it.each(['video', 'game'] as const)('an old %s end failure cannot label the new activity as unsaved', async kind => {
  const h = await host(kind)
  await act(async () => { h.firstEnd.reject(new Error('original response lost')) })
  expect(screen.queryByText(/结束记录尚未保存/)).not.toBeInTheDocument()
  await act(async () => { h.closed('window_closed','new-activity'); h.secondEnd.resolve({...h.current,status:'stopped'}) })
  expect(h.end).toHaveBeenCalledTimes(2)
})

it.each(['video', 'game'] as const)('same-ID %s native/voice duplicates are blocked until the exact failure releases its lock', async kind => {
  const h = await host(kind)
  await act(async () => { h.closed('window_closed', 'new-activity'); h.closed('window_closed', 'new-activity') })
  expect(h.end).toHaveBeenCalledTimes(2)
  await h.command('停止活动', 3)
  expect(h.end).toHaveBeenCalledTimes(2)
  expect(h.complete.mock.calls[2]?.[2]).toBe('failed')
  await act(async () => { h.firstEnd.resolve({...h.old,status:'stopped'}) })
  await act(async () => { h.closed('window_closed', 'new-activity') })
  expect(h.end).toHaveBeenCalledTimes(2)
  await act(async () => { h.secondEnd.reject(new Error('new receipt unavailable')) })
  h.end.mockResolvedValueOnce({...h.current,status:'stopped'})
  await h.command('停止活动', 4)
  expect(h.end).toHaveBeenCalledTimes(3)
  expect(h.end.mock.calls[2]?.[3]).toBe('new-activity')
  expect(h.close).not.toHaveBeenCalled()
  expect(h.end.mock.calls[2]).toEqual(h.end.mock.calls[1])
  expect(h.complete.mock.calls[3]?.[2]).toBe('completed')
})

it.each(['video', 'game'] as const)('late restored %s failure cannot overwrite the newly started activity state', async kind => {
  const h = await host(kind, true)
  await act(async () => { h.firstEnd.reject(new Error('old restore receipt unavailable')) })
  expect(screen.queryByText(/恢复状态尚未确认/)).not.toBeInTheDocument()
  await act(async () => { h.closed('window_closed', 'new-activity'); h.secondEnd.resolve({...h.current,status:'stopped'}) })
  expect(h.end).toHaveBeenCalledTimes(2)
})
