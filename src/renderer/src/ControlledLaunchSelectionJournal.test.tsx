import {act,cleanup,fireEvent,render,screen,waitFor} from '@testing-library/react'
import {beforeEach,afterEach,expect,it,vi} from 'vitest'
import {mkdtemp,rm} from 'node:fs/promises'
import {tmpdir} from 'node:os'
import {join} from 'node:path'
import {ControlledLaunchIntents} from '../../main/controlled-launch-intent'
import type {ControlledLaunchReceipt,ControlledLaunchScope} from '../../shared/controlledLaunch'
import {ApiError,localApi} from './api'
import {SessionWorkspace} from './SessionWorkspace'
import * as LocalInputs from './LocalInputs'
import {readControlledSelection} from './controlledSelection'
import type {AgentTurn,SessionWorkspaceState} from './types'

beforeEach(() => {
  for (const method of ['readVoiceInteractionHandoff', 'invalidateVoiceInteractionHandoff'] as const) {
    vi.spyOn(localApi, method).mockImplementation(async (session_id, child_id) => ({ session_id, child_id, status: 'none',
      handoff_id: null, agent_action_id: null, deferred_progress: null, request: null, receipt: null, reason: null }))
  }
})

const workspace={session:{session_id:'journal-session',child_id:'child-junior-3',state:'study',status:'active',
  started_at:'2026-09-10T10:00:00Z',last_checkpoint_at:'2026-09-10T10:00:00Z',observation_revision: 0, observation_generation: 'a'.repeat(64), observation_status:'paused',snooze_until:null,recall_declared_empty:0,homework_declared_empty:0},
  family_rule:{child_id:'child-junior-3',latest_end_time:'21:30',max_learning_minutes:120,task_segment_minutes:25,break_minutes:5,pet_min_minutes:0,competition_minutes:0,observation_mode:'shadow',reminder_budget_per_10m:1,misclassification_shadow_threshold:3,version:1,updated_at:'2026-09-10T10:00:00Z'},
  planning_context:{energy_level:'medium',fixed_commitment_minutes:0,fixed_commitment_note:null,tomorrow_event_subject:null,tomorrow_event_title:null,tomorrow_event_minutes:0,updated_at:'2026-09-10T10:00:00Z'},
  recalls:[],homework:[],carryover_candidates:[],carryover_decisions:[],plan:null,plan_items:[],daily_report_ready:false,latest_tutor_turn:null,drafts:{},
  sensor_release:{opened_leases:0,active_leases:0,released_leases:0,application_release_confirmed:false,evidence_level:'not_opened',os_release_verified:false,release_verification:null,by_device:{}}} as SessionWorkspaceState
const directories:string[]=[]
afterEach(async()=>{
  await act(async()=>{cleanup();await new Promise(resolve=>setTimeout(resolve,0))})
  vi.restoreAllMocks();vi.unstubAllGlobals();localStorage.clear()
  for(const root of directories.splice(0))await rm(root,{recursive:true,force:true})
})
type Failure='selection'|'permission'|'resource'|'network'|'server'|'foreign'
async function host(kind:'video'|'game',failure:Failure='selection',cancelMode:'confirmed'|'unknown'|'failed'='confirmed'){
  const root=await mkdtemp(join(tmpdir(),'hsc-mounted-native-intent-'));directories.push(root)
  const scope={childId:workspace.session.child_id,sessionId:workspace.session.session_id,kind}
  const records=new Map<string,ControlledLaunchReceipt>(),cancelled=new Set<string>()
  const cancelMissing=vi.fn(async(intent:ControlledLaunchScope)=>{
    if(cancelMode==='failed')throw Error('CANCELLATION_RESPONSE_LOST')
    if(cancelMode==='unknown'||records.has(intent.requestId))return false
    cancelled.add(intent.requestId);return true
  })
  const journal=new ControlledLaunchIntents(root,{
    epoch:async()=> 'synthetic-native-epoch',read:async intent=>records.get(intent.requestId)??null,
    cancelMissing,close:async()=>({closed:false,proof:'unknown'}),hasResource:async()=>false,
    own:async()=>({pid:process.pid,birth:'synthetic-test-owner',
      executable:{pathHash:'f'.repeat(64),fileId:'synthetic-native-owner'}})})
  const prepare=vi.fn(async(s:ControlledLaunchScope)=>journal.prepareForBackend(s))
  const cancel=vi.fn(async(id:string)=>journal.cancel(id))
  const nativeOpen=vi.fn(async()=>({opened:true,processId:123}))
  const open=vi.fn(async(value:{nativeIntentId:string;contentSessionId?:string;gameSessionId?:string})=>
    journal.dispatch(value.nativeIntentId,(value.contentSessionId??value.gameSessionId)!,nativeOpen))
  vi.stubGlobal('desktop',{prepareControlledLaunch:prepare,cancelControlledLaunch:cancel,
    listControlledLaunches:vi.fn(async s=>journal.list(s)),
    [kind==='video'?'openControlledMedia':'openControlledGame']:open})
  const say=vi.spyOn(LocalInputs,'speakLocally').mockImplementation(()=>undefined)
  vi.stubGlobal('fetch',vi.fn().mockRejectedValue(Error('NO_NETWORK')))
  vi.spyOn(localApi,'sessionWorkspace').mockResolvedValue(workspace)
  vi.spyOn(localApi,'activeControlledContent').mockResolvedValue({playback:null})
  vi.spyOn(localApi,'activeControlledGame').mockResolvedValue({game:null})
  const selection={version:1,kind,child_id:scope.childId,session_id:scope.sessionId,status:'ambiguous',
    choices:[{resource_id:'original-a',title:'合成甲',source_version:'a'.repeat(64)},
      {resource_id:'original-b',title:'合成乙',source_version:'b'.repeat(64)}],local_only:true,cloud_transmission_allowed:false}
  const prefix=kind==='video'?'CONTROLLED_CONTENT':'CONTROLLED_GAME'
  let first=true
  const playback={child_id:scope.childId,session_id:scope.sessionId,content_session_id:'chosen-video',game_session_id:'chosen-game',
    curriculum_row_id:'original-b',approval_id:'original-b',title:'合成乙',local_path:'/synthetic/inert.mp4',executable_path:'/synthetic/inert.exe',
    status:'active',launch_allowed:true,remaining_seconds:60,allowed_minutes:1,
    selection_binding:{resource_id:'original-b',source_version:'b'.repeat(64)}}
  const start=vi.spyOn(localApi,kind==='video'?'startControlledContent':'startControlledGame').mockImplementation(async(sid,_query,key,_binding,intentId,epoch)=>{
    expect(sid).toBe(scope.sessionId);expect(cancelled.has(key)).toBe(false)
    const intent=journal.list(scope).find(r=>r.intentId===intentId)
    expect(intent).toMatchObject({...scope,requestId:key,state:'prepared',nativeEpoch:epoch})
    if(first){first=false
      if(failure==='selection'||failure==='foreign')throw new ApiError('需要明确选择',409,prefix+'_SELECTION_REQUIRED',false,undefined,
        {selection:{...selection,...(failure==='foreign'?{child_id:'other-child'}:{})}})
      if(failure==='permission'||failure==='resource')throw new ApiError(failure==='permission'?'家长尚未启用':'许可目录里没有资源',409,prefix+'_UNAVAILABLE')
      if(failure==='server')throw new ApiError('Server error',500,prefix+'_UNAVAILABLE')
      // A lost response may follow an actual backend commit; never cancel it by inference.
      records.set(key,{...intent!,activityId:kind==='video'?playback.content_session_id:playback.game_session_id,status:'active'})
      throw Error('START_RESPONSE_LOST')
    }
    records.set(key,{...intent!,activityId:kind==='video'?playback.content_session_id:playback.game_session_id,status:'active'})
    return playback as never
  })
  vi.spyOn(localApi,'bindControlledGameProcess').mockResolvedValue(playback as never)
  let n=0
  const agent=vi.spyOn(localApi,'agentTurn').mockImplementation(async(_sid,transcript)=>({
    agent_turn_id:`original-request-${++n}`,child_id:scope.childId,session_id:scope.sessionId,transcript,
    action:{type:kind==='video'?'start_controlled_content':'start_controlled_game',query:'合成'},
    response_text:'核对后启动',tool_status:'pending'} as AgentTurn))
  const complete=vi.spyOn(localApi,'completeAgentTurn').mockResolvedValue({} as AgentTurn)
  render(<SessionWorkspace initial={workspace} childName="合成孩子" childGrade="初三" onBack={vi.fn()}/>)
  await act(async()=>{await Promise.resolve()})
  async function command(text:string){
    fireEvent.change(screen.getByLabelText('对学习伙伴说'),{target:{value:text}})
    fireEvent.keyDown(screen.getByLabelText('对学习伙伴说'),{key:'Enter'})
  }
  await command(kind==='video'?'播放合成视频':'打开合成游戏')
  await waitFor(()=>expect(complete).toHaveBeenCalledTimes(1))
  return {root,journal,scope,prepare,cancel,cancelMissing,nativeOpen,start,command,complete,agent,say,selection,cancelled,records}
}
it.each(['video','game'] as const)('%s real journal releases exact no-start selection before a new named choice',async kind=>{
  const h=await host(kind)
  expect(readControlledSelection(h.scope.sessionId,h.scope.childId)?.offer).toEqual(h.selection)
  await h.command('那就选合成乙吧')
  await waitFor(()=>expect(h.prepare).toHaveBeenCalledTimes(2))
  await expect(h.prepare.mock.results[1].value).resolves.toMatchObject({...h.scope,state:'prepared'})
  await waitFor(()=>expect(h.nativeOpen).toHaveBeenCalledTimes(1))
  expect(h.start).toHaveBeenCalledTimes(2)
  expect(h.start.mock.calls[1][3]).toEqual({resource_id:'original-b',source_version:'b'.repeat(64)})
  expect(h.start.mock.calls[1][2]).not.toBe(h.start.mock.calls[0][2])
  expect(h.start.mock.calls[1][4]).not.toBe(h.start.mock.calls[0][4])
  expect(h.cancel).toHaveBeenCalledWith(h.start.mock.calls[0][4])
  expect(h.cancelled.has('original-request-1')).toBe(true)
  expect(h.journal.list(h.scope)).toHaveLength(1)
  const old=await h.journal.prepareForBackend({...h.scope,requestId:'original-request-1'})
  expect(old.state).toBe('settled')
  expect(h.agent).toHaveBeenCalledTimes(1)
})
it.each(['video','game'].flatMap(kind=>['permission','resource'].map(failure=>({kind:kind as 'video'|'game',failure:failure as Failure}))))(
  '$kind explicit $failure denial does not poison subsequent authorized request',async({kind,failure})=>{
  const h=await host(kind,failure)
  await h.command(kind==='video'?'现在播放合成视频':'现在打开合成游戏')
  await waitFor(()=>expect(h.complete).toHaveBeenCalledTimes(2))
  await expect(h.prepare.mock.results[1].value).resolves.toMatchObject({...h.scope,state:'prepared'})
  expect(h.nativeOpen).toHaveBeenCalledTimes(1)
  expect(h.start).toHaveBeenCalledTimes(2)
  expect(h.cancelled.has('original-request-1')).toBe(true)
})

it.each(['video','game'].flatMap(kind=>['unknown','failed'].map(mode=>({kind:kind as 'video'|'game',mode:mode as 'unknown'|'failed'}))))(
  '$kind cancellation $mode keeps original journal blocked and never advertises a selectable offer',async({kind,mode})=>{
  const h=await host(kind,'selection',mode)
  expect(h.cancel).toHaveBeenCalledWith(h.start.mock.calls[0][4])
  expect(h.cancelMissing).toHaveBeenCalledTimes(1)
  expect(h.journal.list(h.scope)).toMatchObject([{requestId:'original-request-1',state:'closed',activityId:null}])
  expect(readControlledSelection(h.scope.sessionId,h.scope.childId)).toBeNull()
  expect(h.complete.mock.calls[0][2]).toBe('failed')
  expect(h.complete.mock.calls[0][1]).toContain('原启动是否执行仍待核对')
  expect(h.say.mock.calls.some(([text])=>text.includes('想选哪一个'))).toBe(false)
  await expect(h.journal.prepareForBackend({...h.scope,requestId:'new-unrelated-request'})).rejects.toThrow('LAUNCH_INTENT_UNRESOLVED')
  expect(h.start).toHaveBeenCalledTimes(1)
  expect(h.nativeOpen).not.toHaveBeenCalled()
})

it.each(['video','game'].flatMap(kind=>['network','server','foreign'].map(failure=>({kind:kind as 'video'|'game',failure:failure as Failure}))))(
  '$kind $failure does not infer missing activity or clear the native barrier',async({kind,failure})=>{
  const h=await host(kind,failure)
  expect(h.cancel).not.toHaveBeenCalled()
  expect(h.cancelMissing).not.toHaveBeenCalled()
  expect(h.journal.list(h.scope)).toMatchObject([{requestId:'original-request-1',state:'prepared'}])
  expect(readControlledSelection(h.scope.sessionId,h.scope.childId)).toBeNull()
  await expect(h.journal.prepareForBackend({...h.scope,requestId:'later-request'})).rejects.toThrow('LAUNCH_INTENT_UNRESOLVED')
  expect(h.start).toHaveBeenCalledTimes(1)
  expect(h.nativeOpen).not.toHaveBeenCalled()
  expect(h.records.size).toBe(failure==='network'?1:0)
})
