import {act,cleanup,fireEvent,render,screen,waitFor} from '@testing-library/react'
import {afterEach,beforeEach,expect,it,vi} from 'vitest'
import {localApi} from './api'
import {SessionWorkspace} from './SessionWorkspace'
import * as LocalInputs from './LocalInputs'
import type {AgentTurn,SessionWorkspaceState} from './types'

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

const workspace={session:{session_id:'session-a',child_id:'child-junior-3',state:'study',status:'active',started_at:'2026-09-10T10:00:00Z',last_checkpoint_at:'2026-09-10T10:00:00Z',observation_revision: 0, observation_generation: 'a'.repeat(64), observation_status:'paused',snooze_until:null,recall_declared_empty:0,homework_declared_empty:0},
  family_rule:{child_id:'child-junior-3',latest_end_time:'21:30',max_learning_minutes:120,task_segment_minutes:25,break_minutes:5,pet_min_minutes:0,competition_minutes:0,observation_mode:'shadow',reminder_budget_per_10m:1,misclassification_shadow_threshold:3,version:1,updated_at:'2026-09-10T10:00:00Z'},
  planning_context:{energy_level:'medium',fixed_commitment_minutes:0,fixed_commitment_note:null,tomorrow_event_subject:null,tomorrow_event_title:null,tomorrow_event_minutes:0,updated_at:'2026-09-10T10:00:00Z'},recalls:[],homework:[],carryover_candidates:[],carryover_decisions:[],plan:null,plan_items:[],daily_report_ready:false,latest_tutor_turn:null,drafts:{},sensor_release:{opened_leases:0,active_leases:0,released_leases:0,application_release_confirmed:false,evidence_level:'not_opened',os_release_verified:false,release_verification:null,by_device:{}}} as SessionWorkspaceState
const intent={intentId:'11111111-1111-4111-8111-111111111111',childId:workspace.session.child_id,sessionId:'session-a',requestId:'original-turn',kind:'video' as const,state:'prepared' as const,activityId:null}
afterEach(async()=>{await act(async()=>{cleanup();await Promise.resolve()});vi.restoreAllMocks();vi.unstubAllGlobals();localStorage.clear()})
function host(){
  vi.spyOn(LocalInputs,'speakLocally').mockImplementation(()=>undefined)
  vi.stubGlobal('fetch',vi.fn().mockRejectedValue(Error('NO_NETWORK')))
  vi.spyOn(localApi,'sessionWorkspace').mockResolvedValue(workspace)
  vi.spyOn(localApi,'activeControlledContent').mockResolvedValue({playback:null})
  vi.spyOn(localApi,'activeControlledGame').mockResolvedValue({game:null})
  const start=vi.spyOn(localApi,'startControlledContent').mockRejectedValue(Error('RESPONSE_LOST'))
  const end=vi.spyOn(localApi,'endControlledContent').mockResolvedValue({child_id:intent.childId,session_id:intent.sessionId,content_session_id:'original-activity',status:'stopped',task_resumed:false} as never)
  const prepare=vi.fn().mockResolvedValue(intent),list=vi.fn().mockResolvedValue([]),cancel=vi.fn(),finish=vi.fn().mockResolvedValue(undefined),open=vi.fn()
  vi.stubGlobal('desktop',{prepareControlledLaunch:prepare,listControlledLaunches:list,cancelControlledLaunch:cancel,finishControlledLaunch:finish,openControlledMedia:open})
  vi.spyOn(localApi,'agentTurn').mockResolvedValue({agent_turn_id:intent.requestId,session_id:intent.sessionId,child_id:intent.childId,transcript:'播放视频',action:{type:'start_controlled_content',query:'合成视频'},response_text:'核对后播放。',tool_status:'pending'} as AgentTurn)
  const complete=vi.spyOn(localApi,'completeAgentTurn').mockResolvedValue({} as AgentTurn)
  const view=()=>render(<SessionWorkspace initial={workspace} childName="合成孩子" childGrade="初三" onBack={vi.fn()}/>)
  return {start,end,prepare,list,cancel,finish,open,complete,view}
}
it('persists native intent before HTTP, loses response and remount cancels exact original without another start',async()=>{
  const h=host();let nativeSaved=false
  h.prepare.mockImplementation(async()=>{nativeSaved=true;return intent})
  h.start.mockImplementation(async(...args)=>{expect(nativeSaved).toBe(true);expect(args[4]).toBe(intent.intentId);throw Error('RESPONSE_LOST')})
  const mounted=h.view()
  fireEvent.change(screen.getByLabelText('对学习伙伴说'),{target:{value:'播放视频'}})
  fireEvent.keyDown(screen.getByLabelText('对学习伙伴说'),{key:'Enter'})
  await waitFor(()=>expect(h.complete).toHaveBeenCalled())
  expect(h.prepare).toHaveBeenCalledWith({childId:intent.childId,sessionId:intent.sessionId,kind:'video',requestId:intent.requestId})
  expect(h.start).toHaveBeenCalledTimes(1);expect(h.open).not.toHaveBeenCalled()
  mounted.unmount()
  h.list.mockImplementation(async scope=>scope.kind==='video'?[intent]:[])
  h.cancel.mockResolvedValue({closed:true,sessionId:'original-activity',proof:'cancelled_before_dispatch',binding:{...intent,activityId:'original-activity',status:'active'}})
  h.view()
  await waitFor(()=>expect(h.end).toHaveBeenCalledWith(intent.sessionId,'launch_cancelled',0,'original-activity'))
  await waitFor(()=>expect(h.finish).toHaveBeenCalledWith('video','original-activity'))
  expect(h.start).toHaveBeenCalledTimes(1);expect(h.open).not.toHaveBeenCalled()
})
it.each(['foreign','unknown'] as const)('%s recovered intent never ends or opens an activity',async mode=>{
  const h=host()
  h.list.mockImplementation(async scope=>scope.kind==='video'?[{...intent,...(mode==='foreign'?{childId:'other-child'}:{})}]:[])
  h.cancel.mockResolvedValue({closed:false,proof:'unknown'})
  h.view();await waitFor(()=>expect(h.list).toHaveBeenCalled())
  await act(async()=>{await new Promise(resolve=>setTimeout(resolve,30))})
  expect(h.end).not.toHaveBeenCalled();expect(h.start).not.toHaveBeenCalled();expect(h.open).not.toHaveBeenCalled()
  if(mode==='foreign')expect(h.cancel).not.toHaveBeenCalled()
})
it('an observed native close is an ended activity, not a never-started cancellation',async()=>{
  const h=host()
  h.list.mockImplementation(async scope=>scope.kind==='video'?[{...intent,state:'closed',activityId:'original-activity'}]:[])
  h.cancel.mockResolvedValue({closed:true,sessionId:'original-activity',proof:'observed_closed',
    binding:{...intent,activityId:'original-activity',status:'stopped'}})
  h.view()
  await waitFor(()=>expect(h.finish).toHaveBeenCalledWith('video','original-activity'))
  expect(screen.queryByText('原启动已安全撤销，没有重新打开活动或自动恢复学习。')).not.toBeInTheDocument()
  expect(screen.getByText('原活动结束记录已经核对，没有重新打开活动或自动恢复学习。')).toBeInTheDocument()
})
