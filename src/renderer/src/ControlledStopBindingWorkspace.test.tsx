import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, afterEach, expect, it, vi } from 'vitest'
import { localApi } from './api'
import { SessionWorkspace } from './SessionWorkspace'
import * as LocalInputs from './LocalInputs'
import { preparePendingControlledEnd, readPendingControlledEnds } from './pendingControlledEnd'
import type { AgentTurn, SessionWorkspaceState } from './types'
beforeEach(() => {
  for (const method of ['readVoiceInteractionHandoff', 'invalidateVoiceInteractionHandoff'] as const) {
    vi.spyOn(localApi, method).mockImplementation(async (session_id, child_id) => ({ session_id, child_id, status: 'none',
      handoff_id: null, agent_action_id: null, deferred_progress: null, request: null, receipt: null, reason: null }))
  }
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

afterEach(()=>{cleanup();localStorage.clear();vi.restoreAllMocks();vi.unstubAllGlobals()})
async function host(kind:'video'|'game', activeId:string|null, boundId='original', missingReadback=false, foreign=false){
  vi.spyOn(LocalInputs,'speakLocally').mockImplementation(()=>undefined)
  vi.stubGlobal('fetch',vi.fn().mockRejectedValue(new Error('NO_NETWORK')))
  vi.spyOn(localApi,'sessionWorkspace').mockResolvedValue(studyWorkspace)
  const scope={child_id:studyWorkspace.session.child_id,session_id:studyWorkspace.session.session_id}
  const field=kind==='video'?'content_session_id':'game_session_id'
  const binding={...scope,...(foreign?{child_id:'another-child'}:{}),kind:kind==='video'?'content':'games',activity_id:boundId}
  const record={...scope,[field]:boundId,status:'interrupted',task_resumed:false}
  const active={...scope,[field]:activeId,status:'active',started_at:new Date().toISOString()}
  const read=kind==='video'?vi.spyOn(localApi,'activeControlledContent'):vi.spyOn(localApi,'activeControlledGame')
  read.mockResolvedValueOnce((kind==='video'?{playback:null}:{game:null}) as never)
    .mockResolvedValue((kind==='video'?{playback:activeId?active:null}:{game:activeId?active:null}) as never)
  vi.spyOn(localApi,kind==='video'?'activeControlledGame':'activeControlledContent').mockResolvedValue((kind==='video'?{game:null}:{playback:null}) as never)
  vi.spyOn(localApi,'agentTurn').mockResolvedValue({
    agent_turn_id:'stop-original',transcript:'停止活动',intent:'stop',action:{
      type:kind==='video'?'stop_controlled_content':'stop_controlled_game',source:'local_controlled_stop_v1',
      controlled_stop_binding:binding,[field]:boundId,
      ...(!missingReadback?{controlled_stop_readback:{...binding,status:'interrupted',started_at:'2026-09-01T00:00:00+00:00'}}:{})
    },response_text:'先核对原活动',tool_status:'pending',orchestrator_version:'local_controlled_stop_v1',
    tool_error_code:null,completed_at:null,created_at:new Date().toISOString(),
    runtime_execution_location:'client_device_bridge',runtime_capability:kind==='video'?'controlled_content.stop':'controlled_game.stop'
  } as unknown as AgentTurn)
  const complete=vi.spyOn(localApi,'completeAgentTurn').mockResolvedValue({} as AgentTurn)
  const close=vi.fn(async (_reason:string,id:string)=>({closed:true,sessionId:id}))
  vi.stubGlobal('desktop',{[kind==='video'?'closeControlledMedia':'closeControlledGame']:close})
  const end=vi.spyOn(localApi,kind==='video'?'endControlledContent':'endControlledGame').mockResolvedValue(record as never)
  const view=render(<SessionWorkspace initial={studyWorkspace} childName="小辰" childGrade="初三" onBack={vi.fn()}/>)
  await waitFor(()=>expect(read).toHaveBeenCalledTimes(1))
  async function command(){
    fireEvent.change(screen.getByLabelText('对学习伙伴说'),{target:{value:'停止活动'}})
    fireEvent.keyDown(screen.getByLabelText('对学习伙伴说'),{key:'Enter'})
    await waitFor(()=>expect(complete).toHaveBeenCalled())
  }
  return {command,close,end,complete,view,read}
}
it.each(['video','game'] as const)('%s old bound stop cannot close a different new active resource',async kind=>{
  const h=await host(kind,'new-resource')
  await h.command()
  expect(h.close).not.toHaveBeenCalled()
  expect(h.end).not.toHaveBeenCalled()
  expect(h.complete.mock.calls[0][2]).toBe('failed')
})
it.each(['video','game'] as const)('%s original readback recovers exact ID when active GET is empty',async kind=>{
  const h=await host(kind,null)
  await h.command()
  expect(h.close).toHaveBeenCalledWith('child_requested','original')
  expect(h.end).toHaveBeenCalledWith(studyWorkspace.session.session_id,'child_requested',0,'original')
  expect(h.complete.mock.calls[0][2]).toBe('completed')
})
it.each(['video','game'] as const)('%s bound stop only retries its own pending entry',async kind=>{
  const h=await host(kind,null)
  const scope={childId:studyWorkspace.session.child_id,sessionId:studyWorkspace.session.session_id,kind}
  preparePendingControlledEnd({...scope,activityId:'unrelated'},{reason:'child_requested',actualSeconds:0})
  preparePendingControlledEnd({...scope,activityId:'original'},{reason:'child_requested',actualSeconds:0})
  await h.command()
  expect(h.close).toHaveBeenCalledTimes(1)
  expect(h.close).toHaveBeenCalledWith('child_requested','original')
  expect(readPendingControlledEnds(scope.sessionId,scope.childId,kind).map(p=>p.activityId)).toEqual(['unrelated'])
})
it('missing readback and no original pending does not infer native closure',async()=>{
  const h=await host('video',null,'original',true)
  await h.command()
  expect(h.close).not.toHaveBeenCalled();expect(h.end).not.toHaveBeenCalled()
})
it.each(['video','game'] as const)('%s foreign response cannot create a pending or close',async kind=>{
  const h=await host(kind,null,'original',false,true)
  await h.command()
  expect(h.close).not.toHaveBeenCalled();expect(h.end).not.toHaveBeenCalled()
  expect(readPendingControlledEnds(studyWorkspace.session.session_id,studyWorkspace.session.child_id,kind)).toEqual([])
})
it.each(['video','game'] as const)('%s unknown native proof keeps the exact original pending, not a successful end',async kind=>{
  const h=await host(kind,null)
  h.close.mockResolvedValue({closed:false,sessionId:'original'})
  await h.command()
  expect(h.end).not.toHaveBeenCalled()
  expect(h.complete.mock.calls[0][2]).toBe('failed')
  const pending=readPendingControlledEnds(studyWorkspace.session.session_id,studyWorkspace.session.child_id,kind)
  expect(pending.map(p=>[p.activityId,p.phase])).toEqual([['original','closure_required']])
})
it('late original active lookup after unmount cannot close or settle',async()=>{
  const h=await host('video',null)
  let resolve!: (value:{playback:null})=>void
  vi.mocked(localApi.activeControlledContent).mockImplementationOnce(()=>new Promise(r=>{resolve=r}))
  fireEvent.change(screen.getByLabelText('对学习伙伴说'),{target:{value:'停止活动'}})
  fireEvent.keyDown(screen.getByLabelText('对学习伙伴说'),{key:'Enter'})
  await waitFor(()=>expect(resolve).toBeTypeOf('function'))
  h.view.unmount()
  await act(async()=>{resolve({playback:null})})
  expect(h.close).not.toHaveBeenCalled();expect(h.end).not.toHaveBeenCalled()
})
