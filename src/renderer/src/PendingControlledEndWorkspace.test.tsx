import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { localApi } from './api'
import { SessionWorkspace } from './SessionWorkspace'
import * as LocalInputs from './LocalInputs'
import { readPendingControlledEnds, clearPendingControlledEnds } from './pendingControlledEnd'
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

async function host(kind:'video'|'game', loseEnd:boolean, stop = true, suffix = '') {
  vi.spyOn(LocalInputs,'speakLocally').mockImplementation(() => undefined)
  vi.stubGlobal('fetch',vi.fn().mockRejectedValue(new Error('NETWORK_FORBIDDEN')))
  vi.spyOn(localApi,'sessionWorkspace').mockResolvedValue(studyWorkspace)
  const original = {title:'synthetic title',
    child_id:studyWorkspace.session.child_id, session_id:studyWorkspace.session.session_id,
    ...(kind === 'video' ? {content_session_id:'original-video'+suffix,local_path:'/private/synthetic-approved.mp4'}
      : {game_session_id:'original-game'+suffix,executable_path:'C:\\synthetic-parent\\approved.exe',os_process_id:987}),
    remaining_seconds:60, allowed_minutes:1, paused_plan_item_id:'original-task',
    started_at:new Date().toISOString(), status:'active', launch_allowed:true,
    local_only:true, cloud_transmission_allowed:false} as unknown as ControlledContentPlayback & ControlledGameSession
  let started = false, ended = false, offline = false
  vi.spyOn(localApi,'activeControlledContent').mockImplementation(async () => ({playback:kind==='video'&&started&&!ended?original:null}))
  vi.spyOn(localApi,'activeControlledGame').mockImplementation(async () => ({game:kind==='game'&&started&&!ended?original:null}))
  const start = kind==='video'?vi.spyOn(localApi,'startControlledContent'):vi.spyOn(localApi,'startControlledGame')
  start.mockImplementation(async () => {started=true;return original})
  vi.spyOn(localApi,'bindControlledGameProcess').mockResolvedValue(original)
  const end = kind==='video'?vi.spyOn(localApi,'endControlledContent'):vi.spyOn(localApi,'endControlledGame')
  end.mockImplementation(async (_sid,_reason,_seconds,id) => {
    expect(id).toBe(kind==='video'?original.content_session_id:original.game_session_id)
    if (offline) throw new Error('connection remains unavailable')
    ended=true
    if(loseEnd){loseEnd=false;offline=true;throw new Error('response lost after synthetic server commit')}
    return {...original,status:'stopped',task_resumed:false}
  })
  const close = vi.fn(async (_reason:string,id:string) => ({closed:true,sessionId:id}))
  let closed!:(reason:string,id:string)=>void
  vi.stubGlobal('desktop',{
    prepareControlledLaunch: nativeLaunchFixture(),
    listControlledLaunches: vi.fn().mockResolvedValue([]),
    [kind==='video'?'openControlledMedia':'openControlledGame']:vi.fn().mockResolvedValue({opened:true,processId:987}),
    [kind==='video'?'closeControlledMedia':'closeControlledGame']:close,
    [kind==='video'?'onControlledMediaClosed':'onControlledGameClosed']:vi.fn(callback => {closed=callback;return vi.fn()}),
  })
  let turn = 0
  vi.spyOn(localApi,'agentTurn').mockImplementation(async (_sid,transcript) => {
    const stop=transcript==='停止活动'
    const type=kind==='video'?stop?'stop_controlled_content':'start_controlled_content':stop?'stop_controlled_game':'start_controlled_game'
    return {agent_turn_id:`end-audit-${++turn}`,transcript,intent:type,action:{type,query:'math'},response_text:'受控活动',
      runtime_capability:`controlled_${kind==='video'?'content':'game'}.${stop?'stop':'play'}`,
      runtime_execution_location:'client_device_bridge',orchestrator_version:'hybrid_companion_v1',tool_status:'pending',
      tool_error_code:null,completed_at:null,created_at:new Date().toISOString()} as AgentTurn
  })
  const complete=vi.spyOn(localApi,'completeAgentTurn').mockResolvedValue({} as AgentTurn)
  function mount(initial = studyWorkspace){return render(<SessionWorkspace initial={initial} childName="小辰" childGrade="初三" onBack={vi.fn()}/>)}
  async function command(text:string, expected:number){
    fireEvent.change(screen.getByLabelText('对学习伙伴说'),{target:{value:text}})
    fireEvent.keyDown(screen.getByLabelText('对学习伙伴说'),{key:'Enter'})
    await waitFor(() => expect(complete).toHaveBeenCalledTimes(expected))
  }
  const view=mount()
  await command('打开活动',1)
  expect(complete.mock.calls[0]?.[2]).toBe('completed')
  if (stop) await command('停止活动',2)
  return {original,start,end,complete,close,view,mount,command,closed:(reason:string,id:string)=>closed(reason,id),reconnect:()=>{offline=false},isEnded:()=>ended}
}

it.each(['video','game'] as const)('%s stop result omits source paths and process details from the generic runtime receipt',async kind=>{
  const h=await host(kind,false)
  expect(h.complete.mock.calls[1]?.[2]).toBe('completed')
  const result=h.complete.mock.calls[1]?.[4] as {execution_receipt:{output:Record<string,unknown>}}
  const output=result.execution_receipt.output
  expect(output[kind==='video'?'content_session_id':'game_session_id']).toBe(kind==='video'?h.original.content_session_id:h.original.game_session_id)
  expect(output.status).toBe('stopped')
  expect(output.task_resumed).toBe(false)
  expect(output).not.toHaveProperty(kind === 'video' ? 'local_path' : 'executable_path')
  expect(output).not.toHaveProperty('os_process_id')
})

it.each(['video','game'] as const)('%s storage failure before close performs neither native close nor end',async kind=>{
  const h=await host(kind,false,false)
  const setItem=Storage.prototype.setItem
  const storage=vi.spyOn(Storage.prototype,'setItem').mockImplementation(function(this:Storage,key,value){
    if(key.startsWith('hsc:controlled-end:'))throw new Error('QUOTA')
    setItem.call(this,key,value)
  })
  await h.command('停止活动',2)
  expect(h.complete.mock.calls[1]?.[2]).toBe('failed')
  expect(h.close).not.toHaveBeenCalled()
  expect(h.end).not.toHaveBeenCalled()
  storage.mockRestore()
  await h.command('停止活动',3)
  expect(h.complete.mock.calls[2]?.[2]).toBe('completed')
})

it.each(['video','game'] as const)('%s failed closed-phase save does not POST or claim completion',async kind=>{
  const h=await host(kind,false,false)
  const setItem=Storage.prototype.setItem
  const storage=vi.spyOn(Storage.prototype,'setItem').mockImplementation(function(this:Storage,key,value){
    if(key.startsWith('hsc:controlled-end:')&&JSON.parse(value).phase==='closed')throw new Error('DISK')
    setItem.call(this,key,value)
  })
  await h.command('停止活动',2)
  expect(h.close).toHaveBeenCalledTimes(1)
  expect(h.end).not.toHaveBeenCalled()
  expect(readPendingControlledEnds(studyWorkspace.session.session_id,studyWorkspace.session.child_id,kind)[0].phase).toBe('closure_required')
  expect(h.complete.mock.calls[1]?.[2]).toBe('failed')
  storage.mockRestore()
  await h.command('停止活动',3)
  expect(h.complete.mock.calls[2]?.[2]).toBe('completed')
})

it.each(['video','game'] as const)('%s wrong native ID retains unknown closure and never ends',async kind=>{
  const h=await host(kind,false,false)
  h.close.mockResolvedValue({closed:true,sessionId:'new-other-activity'})
  await h.command('停止活动',2)
  expect(h.end).not.toHaveBeenCalled()
  expect(h.complete.mock.calls[1]?.[2]).toBe('failed')
  expect(readPendingControlledEnds(studyWorkspace.session.session_id,studyWorkspace.session.child_id,kind)[0].phase).toBe('closure_required')
})

it.each(['video','game'] as const)('%s wrong backend receipt retains closed original and retries without closing again',async kind=>{
  const h=await host(kind,false,false)
  h.end.mockResolvedValueOnce({...h.original,status:'stopped',child_id:'other-child'})
  await h.command('停止活动',2)
  expect(h.complete.mock.calls[1]?.[2]).toBe('failed')
  const pending=readPendingControlledEnds(studyWorkspace.session.session_id,studyWorkspace.session.child_id,kind)[0]
  expect(pending.phase).toBe('closed')
  expect(h.close).toHaveBeenCalledTimes(1)
  await h.command('停止活动',3)
  expect(h.complete.mock.calls[2]?.[2]).toBe('completed')
  expect(h.close).toHaveBeenCalledTimes(1)
  expect(h.end.mock.calls[1]).toEqual(h.end.mock.calls[0])
})

it.each((['video','game'] as const).flatMap(kind=>[undefined,true,'false'].map(taskResumed=>({kind,taskResumed}))))(
  '$kind non-false task_resumed=$taskResumed is not a successful end receipt',async({kind,taskResumed})=>{
  const h=await host(kind,false,false)
  h.end.mockResolvedValueOnce({...h.original,status:'stopped',task_resumed:taskResumed} as never)
  await h.command('停止活动',2)
  expect(h.complete.mock.calls[1]?.[2]).toBe('failed')
  expect(h.complete.mock.calls[1]?.[4]).toBeUndefined()
  expect(readPendingControlledEnds(studyWorkspace.session.session_id,studyWorkspace.session.child_id,kind)[0].phase).toBe('closed')
  expect(h.close).toHaveBeenCalledTimes(1)
  await h.command('停止活动',3)
  expect(h.complete.mock.calls[2]?.[2]).toBe('completed')
  expect(h.close).toHaveBeenCalledTimes(1)
})

it.each(['video','game'] as const)('%s closed pending recovery does not close a new active device or another child',async kind=>{
  const h=await host(kind,true)
  h.view.unmount()
  await act(async()=>{for(let i=0;i<8;i++)await Promise.resolve()})
  const idField=kind==='video'?'content_session_id':'game_session_id'
  const newActivity={...h.original,[idField]:'new-active'}
  if(kind==='video')vi.mocked(localApi.activeControlledContent).mockResolvedValue({playback:newActivity})
  else vi.mocked(localApi.activeControlledGame).mockResolvedValue({game:newActivity})
  const calls=h.end.mock.calls.length, closes=h.close.mock.calls.length
  const foreign={...studyWorkspace,session:{...studyWorkspace.session,child_id:'other-child',session_id:'other-session'}}
  // The other child's actual active lookup has no relation to the original child's pending journal.
  if(kind==='video')vi.mocked(localApi.activeControlledContent).mockResolvedValueOnce({playback:null})
  else vi.mocked(localApi.activeControlledGame).mockResolvedValueOnce({game:null})
  const other=h.mount(foreign)
  await act(async()=>{for(let i=0;i<8;i++)await Promise.resolve()})
  expect(h.end).toHaveBeenCalledTimes(calls)
  expect(h.close).toHaveBeenCalledTimes(closes)
  other.unmount()
  h.reconnect()
  h.mount()
  await waitFor(()=>expect(h.end).toHaveBeenCalledTimes(calls+1))
  expect(h.close).toHaveBeenCalledTimes(closes)
  expect(h.end.mock.calls.at(-1)?.[3]).toBe(kind==='video'?'original-video':'original-game')
})

it.each(['video','game'] as const)('%s explicit deletion prevents a pending close continuation and stale unmount recreation',async kind=>{
  const h=await host(kind,false,false,'-deleted')
  let release!:(value:{closed:boolean;sessionId:string})=>void
  h.close.mockImplementation(()=>new Promise(resolve=>{release=resolve}))
  fireEvent.change(screen.getByLabelText('对学习伙伴说'),{target:{value:'停止活动'}})
  fireEvent.keyDown(screen.getByLabelText('对学习伙伴说'),{key:'Enter'})
  await waitFor(()=>expect(h.close).toHaveBeenCalledTimes(1))
  clearPendingControlledEnds(studyWorkspace.session.child_id,studyWorkspace.session.session_id)
  await act(async()=>release({closed:true,sessionId:kind==='video'?h.original.content_session_id:h.original.game_session_id}))
  await waitFor(()=>expect(h.complete).toHaveBeenCalledTimes(2))
  expect(h.complete.mock.calls[1]?.[2]).toBe('failed')
  expect(h.end).not.toHaveBeenCalled()
  h.view.unmount()
  await act(async()=>{for(let i=0;i<8;i++)await Promise.resolve()})
  expect(readPendingControlledEnds(studyWorkspace.session.session_id,studyWorkspace.session.child_id,kind)).toEqual([])
  expect(h.close).toHaveBeenCalledTimes(1)
})

it.each(['video','game'] as const)('%s stop response lost after commit remains recoverable after renderer remount',async kind=>{
  const h=await host(kind,true)
  expect(h.isEnded()).toBe(true)
  expect(h.complete.mock.calls[1]?.[2]).toBe('failed')
  h.view.unmount()
  // Keep the connection unavailable through unmount cleanup; no UI packet or ID is restored by the fixture.
  await act(async()=>{for(let i=0;i<8;i++)await Promise.resolve()})
  h.reconnect()
  const attempts = h.end.mock.calls.length
  const closes = h.close.mock.calls.length
  h.mount()
  await waitFor(() => expect(h.end).toHaveBeenCalledTimes(attempts + 1))
  await waitFor(() => expect(screen.getByText(/上次.*结束记录已经核对/)).toBeTruthy())
  expect(h.close).toHaveBeenCalledTimes(closes)
  expect(h.end.mock.calls.at(-1)?.[3]).toBe(kind==='video'?'original-video':'original-game')
})

it.each(['video','game'] as const)('%s two independent lost ends survive remount and each replays its own frozen payload',async kind=>{
  const h=await host(kind,true)
  const field=kind==='video'?'content_session_id':'game_session_id'
  const second={...h.original,[field]:'second-'+kind}
  h.start.mockResolvedValue(second)
  vi.mocked(localApi.bindControlledGameProcess).mockResolvedValue(second)
  h.end.mockRejectedValue(new Error('both original ends committed but transport unavailable'))
  await h.command('打开活动',3)
  await act(async()=>h.closed('window_closed',second[field]))
  const pending=readPendingControlledEnds(studyWorkspace.session.session_id,studyWorkspace.session.child_id,kind)
  expect(pending).toHaveLength(2)
  expect(pending.every(value=>value.phase==='closed')).toBe(true)
  const originalCalls=pending.map(value=>[value.sessionId,value.reason,value.actualSeconds,value.activityId])
  h.view.unmount()
  await act(async()=>{for(let i=0;i<8;i++)await Promise.resolve()})
  const attempts=h.end.mock.calls.length, closes=h.close.mock.calls.length
  h.end.mockImplementation(async(_s,_r,_t,id)=>({...(id===second[field]?second:h.original),status:'stopped',replayed:true,task_resumed:false}))
  h.mount()
  await waitFor(()=>expect(h.end).toHaveBeenCalledTimes(attempts+2))
  expect(h.end.mock.calls.slice(attempts)).toEqual(originalCalls)
  expect(h.close).toHaveBeenCalledTimes(closes)
  await waitFor(()=>expect(readPendingControlledEnds(studyWorkspace.session.session_id,studyWorkspace.session.child_id,kind)).toEqual([]))
})

it.each((['video','game'] as const).flatMap(kind=>[false,true].map(partial=>({kind,partial}))))(
  '$kind voice retries every persisted original end without re-entry (partial=$partial)',async({kind,partial})=>{
  const h=await host(kind,true)
  const field=kind==='video'?'content_session_id':'game_session_id'
  const second={...h.original,[field]:'second-'+kind}
  h.start.mockResolvedValue(second)
  vi.mocked(localApi.bindControlledGameProcess).mockResolvedValue(second)
  h.end.mockRejectedValue(new Error('offline after commit'))
  await h.command('打开活动',3)
  await act(async()=>h.closed('window_closed',second[field]))
  expect(readPendingControlledEnds(studyWorkspace.session.session_id,studyWorkspace.session.child_id,kind)).toHaveLength(2)
  h.view.unmount()
  await act(async()=>{for(let i=0;i<8;i++)await Promise.resolve()})
  const attempts=h.end.mock.calls.length
  h.mount()
  await waitFor(()=>expect(h.end).toHaveBeenCalledTimes(attempts+2))
  // A new active lookup is not an authorization to substitute a different native resource.
  const third={...h.original,[field]:'new-active-'+kind}
  if(kind==='video')vi.mocked(localApi.activeControlledContent).mockResolvedValue({playback:third})
  else vi.mocked(localApi.activeControlledGame).mockResolvedValue({game:third})
  const closes=h.close.mock.calls.length, firstId=h.original[field]
  const originals=readPendingControlledEnds(studyWorkspace.session.session_id,studyWorkspace.session.child_id,kind)
  h.end.mockImplementation(async(_sid,_reason,_seconds,id)=>{
    if(partial && id===firstId)throw new Error('original receipt still unavailable')
    expect([firstId,second[field]]).toContain(id)
    return {...(id===firstId?h.original:second),status:'stopped',replayed:true,task_resumed:false}
  })
  const before=h.end.mock.calls.length
  await h.command('停止活动',4)
  expect(h.end.mock.calls.slice(before)).toEqual(originals.map(p=>[p.sessionId,p.reason,p.actualSeconds,p.activityId]))
  expect(h.close).toHaveBeenCalledTimes(closes)
  if(partial){
    expect(h.complete.mock.calls[3]?.[2]).toBe('failed')
    expect(h.complete.mock.calls[3]?.[1]).toContain('还没有全部确认')
    expect(h.complete.mock.calls[3]?.[1]).not.toContain('重新进入')
    expect(readPendingControlledEnds(studyWorkspace.session.session_id,studyWorkspace.session.child_id,kind).map(p=>p.activityId)).toEqual([firstId])
    h.end.mockResolvedValue({...h.original,status:'stopped',replayed:true,task_resumed:false})
    await h.command('停止活动',5)
    expect(h.complete.mock.calls[4]?.[2]).toBe('completed')
    expect(h.close).toHaveBeenCalledTimes(closes)
    expect(h.end.mock.calls.at(-1)?.[3]).toBe(firstId)
  }else{
    expect(h.complete.mock.calls[3]?.[2]).toBe('completed')
    const data=h.complete.mock.calls[3]?.[4] as {execution_receipt:{output:{completed_ends:Record<string,unknown>[]}}}
    expect(data.execution_receipt.output.completed_ends.map(p=>p[field])).toEqual([firstId,second[field]])
    expect(JSON.stringify(data)).not.toMatch(/local_path|executable_path|os_process_id/)
  }
  expect(readPendingControlledEnds(studyWorkspace.session.session_id,studyWorkspace.session.child_id,kind)).toEqual([])
})

it.each(['video','game'] as const)('%s failed final journal removal replays the backend receipt without another physical close',async kind=>{
  const h=await host(kind,false,false)
  const removeItem=Storage.prototype.removeItem
  const remove=vi.spyOn(Storage.prototype,'removeItem').mockImplementation(function(this:Storage,key){
    if(key.startsWith('hsc:controlled-end:'))throw new Error('remove unavailable')
    removeItem.call(this,key)
  })
  await h.command('停止活动',2)
  expect(h.complete.mock.calls[1]?.[2]).toBe('failed')
  expect(h.end).toHaveBeenCalledTimes(1)
  expect(h.close).toHaveBeenCalledTimes(1)
  remove.mockRestore()
  await h.command('停止活动',3)
  expect(h.complete.mock.calls[2]?.[2]).toBe('completed')
  expect(h.end).toHaveBeenCalledTimes(2)
  expect(h.close).toHaveBeenCalledTimes(1)
})

it.each(['video','game'] as const)('%s cold unknown close remains pending with no POST even when active GET is null',async kind=>{
  const h=await host(kind,false,false)
  h.close.mockResolvedValue({closed:false,sessionId:kind==='video'?'original-video':'original-game'})
  await h.command('停止活动',2)
  expect(h.end).not.toHaveBeenCalled()
  h.view.unmount()
  await act(async()=>{for(let i=0;i<8;i++)await Promise.resolve()})
  vi.mocked(localApi.activeControlledContent).mockResolvedValue({playback:null})
  vi.mocked(localApi.activeControlledGame).mockResolvedValue({game:null})
  const closes=h.close.mock.calls.length
  h.mount()
  await waitFor(()=>expect(h.close).toHaveBeenCalledTimes(closes+1))
  expect(h.end).not.toHaveBeenCalled()
  expect(readPendingControlledEnds(studyWorkspace.session.session_id,studyWorkspace.session.child_id,kind)[0].phase).toBe('closure_required')
})

it.each(['video','game'] as const)('%s remount offline retains a visible original end and a later voice stop retries only that request',async kind=>{
  const h=await host(kind,true)
  h.view.unmount()
  await act(async()=>{for(let i=0;i<8;i++)await Promise.resolve()})
  const closes=h.close.mock.calls.length
  h.mount()
  await waitFor(()=>expect(screen.getByText(/上次活动的结束记录尚未核对/)).toBeTruthy())
  h.reconnect()
  await h.command('停止活动',3)
  expect(h.complete.mock.calls[2]?.[2]).toBe('completed')
  expect(h.close).toHaveBeenCalledTimes(closes)
  expect(readPendingControlledEnds(studyWorkspace.session.session_id,studyWorkspace.session.child_id,kind)).toEqual([])
})

it.each(['video','game'] as const)('%s deleted backend scope cannot create a replacement and keeps the failed original request visible',async kind=>{
  const h=await host(kind,true)
  h.view.unmount()
  await act(async()=>{for(let i=0;i<8;i++)await Promise.resolve()})
  h.end.mockRejectedValue(new Error('404 ORIGINAL_SCOPE_DELETED'))
  const starts=h.start.mock.calls.length, closes=h.close.mock.calls.length, ends=h.end.mock.calls.length
  h.mount()
  await waitFor(()=>expect(h.end).toHaveBeenCalledTimes(ends+1))
  await waitFor(()=>expect(screen.getByText(/上次活动的结束记录尚未核对/)).toBeTruthy())
  expect(h.close).toHaveBeenCalledTimes(closes)
  expect(h.start).toHaveBeenCalledTimes(starts)
  expect(readPendingControlledEnds(studyWorkspace.session.session_id,studyWorkspace.session.child_id,kind)[0].phase).toBe('closed')
})
