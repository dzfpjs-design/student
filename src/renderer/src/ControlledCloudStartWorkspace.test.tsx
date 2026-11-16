import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, afterEach, expect, it, vi } from 'vitest'
import { localApi } from './api'
import { SessionWorkspace } from './SessionWorkspace'
import * as LocalInputs from './LocalInputs'
import type { AgentTurn, SessionWorkspaceState } from './types'

import type { ControlledLaunchScope, ControlledLaunchIntent } from '../../shared/controlledLaunch'

// Explicit per-host native contract fixture; never installed for missing-native cases.
beforeEach(() => {
  for (const method of ['readVoiceInteractionHandoff', 'invalidateVoiceInteractionHandoff'] as const) {
    vi.spyOn(localApi, method).mockImplementation(async (session_id, child_id) => ({ session_id, child_id, status: 'none',
      handoff_id: null, agent_action_id: null, deferred_progress: null, request: null, receipt: null, reason: null }))
  }
})

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
  session:{session_id:'semantic-session',child_id:'child-junior-3',state:'study',status:'active',
    started_at:'2026-09-10T10:00:00Z',last_checkpoint_at:'2026-09-10T10:00:00Z',observation_revision: 0, observation_generation: 'a'.repeat(64), observation_status:'paused',
    snooze_until:null,recall_declared_empty:0,homework_declared_empty:0},
  family_rule:{child_id:'child-junior-3',latest_end_time:'21:30',max_learning_minutes:120,task_segment_minutes:25,
    break_minutes:5,pet_min_minutes:0,competition_minutes:0,observation_mode:'shadow',reminder_budget_per_10m:1,
    misclassification_shadow_threshold:3,version:1,updated_at:'2026-09-10T10:00:00Z'},
  planning_context:{energy_level:'medium',fixed_commitment_minutes:0,fixed_commitment_note:null,
    tomorrow_event_subject:null,tomorrow_event_title:null,tomorrow_event_minutes:0,updated_at:'2026-09-10T10:00:00Z'},
  recalls:[],homework:[],carryover_candidates:[],carryover_decisions:[],plan:null,plan_items:[],
  daily_report_ready:false,latest_tutor_turn:null,drafts:{},
  sensor_release:{opened_leases:0,active_leases:0,released_leases:0,application_release_confirmed:false,
    evidence_level:'not_opened',os_release_verified:false,release_verification:null,by_device:{}}
}
afterEach(async()=>{ await act(async()=>{cleanup();await Promise.resolve()});localStorage.clear();vi.restoreAllMocks();vi.unstubAllGlobals() })

async function host(kind:'content'|'games', mismatch?:'child'|'query'|'source', nativeAvailable=true) {
  vi.spyOn(LocalInputs,'speakLocally').mockImplementation(()=>undefined)
  vi.stubGlobal('fetch',vi.fn().mockRejectedValue(new Error('NO_NETWORK')))
  vi.spyOn(localApi,'sessionWorkspace').mockResolvedValue(workspace)
  vi.spyOn(localApi,'activeControlledContent').mockResolvedValue({playback:null})
  vi.spyOn(localApi,'activeControlledGame').mockResolvedValue({game:null})
  const selection={resource_id:'original-source',source_version:'a'.repeat(64)}
  const packet={child_id:workspace.session.child_id,session_id:workspace.session.session_id,
    kind,source_version:'b'.repeat(64),query:'图形练习',selection_binding:selection,
    ...(mismatch==='child'?{child_id:'other-child'}:{}),...(mismatch==='query'?{query:'另一个'}:{})}
  const scope={session_id:workspace.session.session_id,child_id:workspace.session.child_id}
  const record={...scope,content_session_id:'original-video',game_session_id:'original-game',
    curriculum_row_id:selection.resource_id,approval_id:selection.resource_id,
    title:'图形练习',status:'active',launch_allowed:true,allowed_minutes:5,
    local_path:'/synthetic/inert.mp4',executable_path:'/synthetic/inert.exe',
    selection_binding:selection,started_at:new Date().toISOString()}
  const start=vi.spyOn(localApi,kind==='content'?'startControlledContent':'startControlledGame').mockResolvedValue(record as never)
  vi.spyOn(localApi,'bindControlledGameProcess').mockResolvedValue(record as never)
  const open=vi.fn().mockResolvedValue({opened:true,processId:123})
  const prepare = nativeLaunchFixture()
  vi.stubGlobal('desktop',{...(nativeAvailable?{prepareControlledLaunch:prepare}:{}),listControlledLaunches:vi.fn().mockResolvedValue([]),
    [kind==='content'?'openControlledMedia':'openControlledGame']:open})
  const admission=vi.spyOn(localApi,'admitVoiceControlExecution').mockResolvedValue({status:'admitted'})
  const turn={...scope,agent_turn_id:'semantic-original',transcript:'我想看看图形练习',intent:'control',
    requires_control_admission:true,action:{type:kind==='content'?'start_controlled_content':'start_controlled_game',
      source:'content_free_voice_control',query:'图形练习',controlled_voice_binding:packet,
      selection_binding:mismatch==='source'?{...selection,resource_id:'different'}:selection},
    response_text:'核对原资源后再打开。',tool_status:'pending',orchestrator_version:'synthetic',
    tool_error_code:null,completed_at:null,created_at:new Date().toISOString()} as unknown as AgentTurn
  vi.spyOn(localApi,'agentTurn').mockResolvedValue(turn)
  const complete=vi.spyOn(localApi,'completeAgentTurn').mockResolvedValue({} as AgentTurn)
  render(<SessionWorkspace initial={workspace} childName="合成孩子" childGrade="初三" onBack={vi.fn()}/>)
  fireEvent.change(screen.getByLabelText('对学习伙伴说'),{target:{value:turn.transcript}})
  fireEvent.keyDown(screen.getByLabelText('对学习伙伴说'),{key:'Enter'})
  await waitFor(()=>expect(complete).toHaveBeenCalled())
  return {start,open,complete,admission,selection,prepare}
}
it.each(['content','games'] as const)('%s forwards original semantic source binding after admission',async kind=>{
  const h=await host(kind)
  expect(h.admission).toHaveBeenCalledWith(workspace.session.session_id,'semantic-original')
  expect(h.prepare).toHaveBeenCalledWith({childId:workspace.session.child_id,sessionId:workspace.session.session_id,
    kind:kind==='content'?'video':'game',requestId:'semantic-original'})
  const intent = await h.prepare.mock.results[0].value
  expect(h.start).toHaveBeenCalledWith(workspace.session.session_id,'图形练习','semantic-original',h.selection,intent.intentId,undefined)
  expect(h.open).toHaveBeenCalledWith(expect.objectContaining({nativeIntentId:intent.intentId}))
  expect(h.open).toHaveBeenCalledTimes(1)
  expect(h.prepare.mock.invocationCallOrder[0]).toBeLessThan(h.start.mock.invocationCallOrder[0])
})
it.each(['content','games'] as const)('%s without native launch preparation cannot create a backend activity',async kind=>{
  const h=await host(kind,undefined,false)
  expect(h.prepare).not.toHaveBeenCalled()
  expect(h.start).not.toHaveBeenCalled()
  expect(h.open).not.toHaveBeenCalled()
  expect(h.complete.mock.calls.at(-1)?.[2]).toBe('failed')
  expect(h.complete.mock.calls.at(-1)?.[1]).toContain('没有确认保存')
})
it.each(['child','query','source'] as const)('mismatched semantic %s never starts or opens resource',async mismatch=>{
  const h=await host('content',mismatch)
  expect(h.start).not.toHaveBeenCalled()
  expect(h.open).not.toHaveBeenCalled()
  expect(h.complete.mock.calls.at(-1)?.[2]).toBe('failed')
})
