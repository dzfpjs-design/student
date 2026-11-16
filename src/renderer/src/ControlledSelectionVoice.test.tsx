import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, afterEach, expect, it, vi } from 'vitest'
import { ApiError, localApi } from './api'
import { SessionWorkspace } from './SessionWorkspace'
import * as LocalInputs from './LocalInputs'
import type { AgentTurn, SessionWorkspaceState, ControlledContentPlayback, ControlledGameSession } from './types'
import contentPacket from './controlledSelection.content.fixture.json'
import gamePacket from './controlledSelection.games.fixture.json'
import { captureControlledSelection, removeControlledSelection, selectionOffer } from './controlledSelection'

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
  session: { session_id:'selection-session', child_id:'child-junior-3', status:'active', state:'study',
    started_at:'2026-09-10T10:00:00Z', last_checkpoint_at:'2026-09-10T10:00:00Z', observation_revision: 0, observation_generation: 'a'.repeat(64), observation_status:'paused',
    snooze_until:null, recall_declared_empty:0, homework_declared_empty:0 },
  family_rule: {child_id:'child-junior-3', latest_end_time:'21:30', max_learning_minutes:120,
    task_segment_minutes:25, break_minutes:5, pet_min_minutes:0, competition_minutes:0,
    observation_mode:'shadow', reminder_budget_per_10m:1, misclassification_shadow_threshold:3,
    version:1,updated_at:'2026-09-10T10:00:00Z'},
  planning_context:{energy_level:'medium',fixed_commitment_minutes:0,fixed_commitment_note:null,
    tomorrow_event_subject:null,tomorrow_event_title:null,tomorrow_event_minutes:0,updated_at:'2026-09-10T10:00:00Z'},
  recalls:[],homework:[],carryover_candidates:[],carryover_decisions:[],
  plan:{plan_id:'selection-plan',plan_date:'2026-09-10',status:'active',total_minutes:25,generator:'local_rule_engine',version:1},
  plan_items:[{plan_item_id:'original-learning-task',homework_id:null,subject:'数学',title:'合成作业',
    expected_minutes:25,actual_minutes:null,priority:1,status:'active',position:1,started_at:'2026-09-10T10:00:00Z',
    completed_at:null,active_seconds:0,active_segment_started_at:'2026-09-10T10:00:00Z',source_type:'school_homework',
    source_ref:null,completion_standard:'完成并检查',cognitive_load:'medium',task_type:'written',
    completion_evidence_type:null,completion_note:null,completion_checklist:[],completion_artifact_id:null}],
  daily_report_ready:false,latest_tutor_turn:null,drafts:{},
  sensor_release:{opened_leases:0,active_leases:0,released_leases:0,application_release_confirmed:false,
    evidence_level:'not_opened',os_release_verified:false,release_verification:null,by_device:{}}
}

afterEach(async () => {
  // Let unmount's local lifecycle reads settle before restoring API spies.
  await act(async()=>{ cleanup(); await new Promise(resolve=>setTimeout(resolve,0)) })
  localStorage.clear(); vi.restoreAllMocks(); vi.unstubAllGlobals()
})

function expectNoCloudFetch():void {
  // Mount/unmount can legitimately fetch local status. No semantic request or
  // external destination is allowed in these private selection turns.
  for(const [input] of vi.mocked(fetch).mock.calls){
    const url=new URL(String(input))
    expect(['127.0.0.1','localhost']).toContain(url.hostname)
    expect(url.pathname).not.toMatch(/agent-turns|cloud|transcrib|tutor\/turn/u)
  }
}

const defaultWorkspace=workspace
async function host(kind:'video'|'game', secondTitle='星空故事', packet?:typeof contentPacket|typeof gamePacket) {
  const workspace=packet ? {...defaultWorkspace,session:{...defaultWorkspace.session,
    child_id:packet.offer.details.selection.child_id,session_id:packet.offer.details.selection.session_id}} : defaultWorkspace
  const say=vi.spyOn(LocalInputs,'speakLocally').mockImplementation(() => undefined)
  vi.stubGlobal('fetch',vi.fn().mockRejectedValue(new Error('NETWORK_FORBIDDEN')))
  vi.spyOn(localApi,'sessionWorkspace').mockResolvedValue(workspace)
  vi.spyOn(localApi,'activeControlledContent').mockResolvedValue({playback:null})
  vi.spyOn(localApi,'activeControlledGame').mockResolvedValue({game:null})
  const choices=packet?.offer.details.selection.choices ?? [{resource_id:'approved-one',title:'分数练习',source_version:'a'.repeat(64)},
    {resource_id:'approved-two',title:secondTitle,source_version:'b'.repeat(64)},
    ...['第三项合成内容','第四项合成内容','第五项合成内容'].map((title,i)=>({resource_id:`extra-${i}`,title,source_version:String(i+1).repeat(64)}))]
  const selection=packet?.offer.details.selection ?? {version:1,kind,child_id:workspace.session.child_id,session_id:workspace.session.session_id,
    status:'ambiguous',choices,local_only:true,cloud_transmission_allowed:false}
  const offered=new ApiError('找到多个符合的内容，请选择完整名称。',409,
    kind==='video'?'CONTROLLED_CONTENT_SELECTION_REQUIRED':'CONTROLLED_GAME_SELECTION_REQUIRED',false,undefined,{selection})
  const playback=(packet?.selected ?? {child_id:workspace.session.child_id,session_id:workspace.session.session_id,
    title:secondTitle,content_session_id:'selected-video',game_session_id:'selected-game',
    curriculum_row_id:'approved-two',approval_id:'approved-two',local_path:'/private/synthetic-story.mp4',
    executable_path:'C:\\synthetic-story.exe',allowed_minutes:1,remaining_seconds:60,
    status:'active',launch_allowed:true,started_at:new Date().toISOString(),
    selection_binding:{resource_id:'approved-two',source_version:'b'.repeat(64)},
    local_only:true,cloud_transmission_allowed:false}) as unknown as ControlledContentPlayback & ControlledGameSession
  const start=kind==='video'?vi.spyOn(localApi,'startControlledContent'):vi.spyOn(localApi,'startControlledGame')
  start.mockRejectedValueOnce(offered).mockResolvedValue(playback)
  const end=kind==='video'?vi.spyOn(localApi,'endControlledContent'):vi.spyOn(localApi,'endControlledGame')
  end.mockResolvedValue({...playback,status:'stopped'})
  vi.spyOn(localApi,'bindControlledGameProcess').mockResolvedValue(playback)
  const open=vi.fn().mockResolvedValue({opened:true,processId:123})
  const prepare = nativeLaunchFixture()
  const cancel = vi.fn(async (intentId:string) => {
    const index = start.mock.calls.findIndex(call=>call[4]===intentId)
    expect(index).toBeGreaterThanOrEqual(0)
    await expect(start.mock.results[index].value).rejects.toBeInstanceOf(ApiError)
    return {closed:true,noActivity:true,proof:'cancelled_before_start'}
  })
  vi.stubGlobal('desktop',{prepareControlledLaunch:prepare,listControlledLaunches:vi.fn().mockResolvedValue([]),
    cancelControlledLaunch:cancel,
    [kind==='video'?'openControlledMedia':'openControlledGame']:open,
    [kind==='video'?'closeControlledMedia':'closeControlledGame']:vi.fn(async (_reason:string,id:string)=>({closed:true,sessionId:id}))})
  const agent=vi.spyOn(localApi,'agentTurn').mockImplementation(async (_sid,transcript)=>({
    agent_turn_id:'root-choice-turn',child_id:workspace.session.child_id,session_id:workspace.session.session_id,
    transcript,intent:'controlled_start',action:{type:kind==='video'?'start_controlled_content':'start_controlled_game',query:'故事'},
    response_text:'核对家长许可后再打开。',orchestrator_version:'hybrid_companion_v1',tool_status:'pending',
    tool_error_code:null,completed_at:null,created_at:new Date().toISOString()} as AgentTurn))
  const complete=vi.spyOn(localApi,'completeAgentTurn').mockResolvedValue({} as AgentTurn)
  const view=render(<SessionWorkspace initial={workspace} childName="合成孩子" childGrade="初三" onBack={vi.fn()}/> )
  async function command(text:string){
    fireEvent.change(screen.getByLabelText('对学习伙伴说'),{target:{value:text}})
    fireEvent.keyDown(screen.getByLabelText('对学习伙伴说'),{key:'Enter'})
  }
  await command(kind==='video'?'播放故事视频':'打开故事游戏')
  await waitFor(()=>expect(complete).toHaveBeenCalledTimes(1))
  return {kind,say,agent,start,complete,open,view,command,choices,playback,offered,workspace,prepare,cancel}
}

it.each(['video','game'] as const)('%s choice names are spoken locally, not stored as ordinary cloud history',async kind=>{
  const h=await host(kind)
  await waitFor(()=>expect(h.say.mock.calls.some(c=>c[0].includes('星空故事'))).toBe(true))
  const spoken=h.say.mock.calls.find(c=>c[0].includes('星空故事'))!
  expect(spoken[4]).toMatchObject({localOnly:true})
  expect(JSON.stringify(h.complete.mock.calls)).not.toMatch(/星空故事|分数练习|approved-two/)
  expect(h.open).not.toHaveBeenCalled()
  expect(h.cancel).toHaveBeenCalledWith(h.start.mock.calls[0][4])
  expectNoCloudFetch()
})

it.each(['video','game'] as const)('%s natural explicit choice is a new exact local request, not another cloud turn',async kind=>{
  const h=await host(kind)
  await h.command('那就选星空故事吧')
  await waitFor(()=>expect(h.start).toHaveBeenCalledTimes(2))
  expect(h.agent).toHaveBeenCalledTimes(1)
  const selected=h.start.mock.calls[1]
  expect(selected[0]).toBe(workspace.session.session_id)
  expect(selected[1]).toBe('星空故事')
  expect(selected[2]).not.toBe('root-choice-turn')
  expect(selected[3]).toMatchObject({resource_id:'approved-two',source_version:'b'.repeat(64)})
  await waitFor(()=>expect(h.open).toHaveBeenCalledTimes(1))
  expect(JSON.stringify(h.agent.mock.calls)).not.toContain('星空故事')
  expectNoCloudFetch()
})

it.each(['video','game'] as const)('%s punctuation in the offered title stays local and selects its exact version',async kind=>{
  const h=await host(kind,'星空，故事。')
  await h.command('那就选星空，故事。吧')
  await waitFor(()=>expect(h.open).toHaveBeenCalledTimes(1))
  expect(h.agent).toHaveBeenCalledTimes(1)
  expect(h.start.mock.calls[1].slice(1)).toEqual(['星空，故事。',expect.any(String),
    {resource_id:'approved-two',source_version:'b'.repeat(64)},(await h.prepare.mock.results[1].value).intentId,undefined])
  expectNoCloudFetch()
})

it.each(['第二个','就第二个吧','第六个','第二个还是第三个'])('ordinal %s never borrows the current offer to execute',async utterance=>{
  const h=await host('video')
  const before=h.say.mock.calls.length
  await h.command(utterance)
  await waitFor(()=>expect(h.say.mock.calls.length).toBeGreaterThan(before))
  expect(h.agent).toHaveBeenCalledTimes(1)
  expect(h.start).toHaveBeenCalledTimes(1)
  expect(h.open).not.toHaveBeenCalled()
  expect(h.say.mock.calls.at(-1)?.[4]).toMatchObject({localOnly:true})
  expectNoCloudFetch()
})

it('a delayed ordinal after offer replacement only clarifies; a new explicit title is required',async()=>{
  const h=await host('video')
  const storageKey='hsc:controlled-selection:v1:'+JSON.stringify([workspace.session.child_id,workspace.session.session_id])
  const pending=JSON.parse(localStorage.getItem(storageKey)!)
  pending.offer.choices=[pending.offer.choices[1],pending.offer.choices[0]]
  pending.offer.choices[1].title='新顺序的名称'
  pending.offer.choices[1].source_version='c'.repeat(64)
  localStorage.setItem(storageKey,JSON.stringify(pending))
  const before=h.say.mock.calls.length
  await h.command('就第二个吧')
  await waitFor(()=>expect(h.say.mock.calls.length).toBeGreaterThan(before))
  expect(h.start).toHaveBeenCalledTimes(1)
  expect(h.open).not.toHaveBeenCalled()
  expect(h.agent).toHaveBeenCalledTimes(1)
  expect(h.say.mock.calls.at(-1)?.[0]).toContain('完整名称')
})

it.each(['video','game'] as const)('%s actual HTTP offer/result packets drive the mounted named selection receiver',async kind=>{
  // These packets are unmodified exports from the real isolated HTTP/restart
  // test. This is mounted packet replay, not live HTTP or physical playback.
  const packet=kind==='video'?contentPacket:gamePacket
  const h=await host(kind,undefined,packet)
  await h.command('就选Synthetic story吧')
  await waitFor(()=>expect(h.open).toHaveBeenCalledTimes(1))
  expect(h.start.mock.calls[1][3]).toEqual(packet.selected.selection_binding)
  expect(h.start.mock.calls[1][1]).toBe(packet.selected.title)
  expect(h.agent).toHaveBeenCalledTimes(1)
  expectNoCloudFetch()
})

it.each(['video','game'] as const)('%s deletion during local start prevents late launch or offer recreation',async kind=>{
  const h=await host(kind)
  let resolve!:(value:typeof h.playback)=>void
  h.start.mockImplementationOnce(()=>new Promise<typeof h.playback>(r=>{resolve=r}))
  await h.command('星空故事')
  await waitFor(()=>expect(h.start).toHaveBeenCalledTimes(2))
  const snapshot=captureControlledSelection(h.workspace.session.session_id,h.workspace.session.child_id)
  expect(removeControlledSelection(snapshot)).toBe(true)
  await act(async()=>resolve(h.playback))
  expect(h.open).not.toHaveBeenCalled()
  expect(localStorage.getItem(snapshot.key)).toBeNull()
  expect(h.agent).toHaveBeenCalledTimes(1)
})

it.each(['video','game'] as const)('%s failed response survives remount and retries only the same exact receipt',async kind=>{
  const h=await host(kind)
  h.start.mockRejectedValueOnce(new Error('response lost'))
  await h.command('星空故事')
  await waitFor(()=>expect(h.say.mock.calls.some(c=>c[0].includes('原请求已在本机保留'))).toBe(true))
  const request=h.start.mock.calls[1]
  h.view.unmount()
  render(<SessionWorkspace initial={h.workspace} childName="合成孩子" childGrade="初三" onBack={vi.fn()}/> )
  expect(h.start).toHaveBeenCalledTimes(2)
  h.start.mockResolvedValueOnce({...h.playback,replayed:true,launch_allowed:false})
  await h.command('重试打开')
  await waitFor(()=>expect(h.start).toHaveBeenCalledTimes(3))
  expect(h.start.mock.calls[2]).toEqual(request)
  expect(h.open).not.toHaveBeenCalled()
  expect(h.agent).toHaveBeenCalledTimes(1)
})

it('exact cleanup cannot remove a replacement offer',async()=>{
  const h=await host('video')
  const old=captureControlledSelection(h.workspace.session.session_id,h.workspace.session.child_id)
  const newer=JSON.parse(old.raw!)
  newer.rootTurnId='new-offer-turn'
  localStorage.setItem(old.key,JSON.stringify(newer))
  expect(removeControlledSelection(old)).toBe(false)
  expect(JSON.parse(localStorage.getItem(old.key)!).rootTurnId).toBe('new-offer-turn')
})

it.each(['星空，故事。不要打开','他说星空，故事。','星空，故事。和分数练习'])('ambiguous or negative private title stays local: %s',async utterance=>{
  const h=await host('video','星空，故事。')
  const before=h.say.mock.calls.length
  await h.command(utterance)
  await waitFor(()=>expect(h.say.mock.calls.length).toBeGreaterThan(before))
  expect(h.agent).toHaveBeenCalledTimes(1)
  expect(h.start).toHaveBeenCalledTimes(1)
  expect(h.open).not.toHaveBeenCalled()
  expectNoCloudFetch()
})

it.each(['video','game'] as const)('%s a generic follow-up cannot turn an outstanding selection into an empty first-choice start',async kind=>{
  const h=await host(kind)
  h.agent.mockResolvedValueOnce({agent_turn_id:'generic-retry-turn',child_id:h.workspace.session.child_id,
    session_id:h.workspace.session.session_id,transcript:'想换一种放松方式',intent:'controlled_start',
    action:{type:kind==='video'?'start_controlled_content':'start_controlled_game',query:''},
    response_text:'核对许可。',tool_status:'pending'} as AgentTurn)
  await h.command('想换一种放松方式')
  await waitFor(()=>expect(h.complete).toHaveBeenCalledTimes(2))
  expect(h.start).toHaveBeenCalledTimes(1)
  expect(h.open).not.toHaveBeenCalled()
  expect(JSON.stringify(h.agent.mock.calls)).not.toMatch(/星空故事|分数练习|approved-two/)
})

it('a current offer explicitly explains excluded long titles without truncating them into selectable names',async()=>{
  const h=await host('video')
  const snapshot=captureControlledSelection(h.workspace.session.session_id,h.workspace.session.child_id)
  const value=JSON.parse(snapshot.raw!)
  value.offer.unselectable_title_count=1
  localStorage.setItem(snapshot.key,JSON.stringify(value))
  const before=h.say.mock.calls.length
  await h.command('第二个')
  await waitFor(()=>expect(h.say.mock.calls.length).toBeGreaterThan(before))
  expect(h.say.mock.calls.at(-1)?.[0]).toContain('名称过长')
  expect(h.start).toHaveBeenCalledTimes(1)
  expect(h.agent).toHaveBeenCalledTimes(1)
  value.offer.choices[0].title='甲'.repeat(201)
  expect(selectionOffer(value.offer,h.workspace.session.session_id,h.workspace.session.child_id)).toBe(false)
})
