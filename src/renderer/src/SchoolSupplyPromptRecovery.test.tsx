import { act, cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { localApi } from './api'
import * as inputs from './LocalInputs'
import * as voice from './companionVoice'
import { SessionWorkspace } from './SessionWorkspace'
import type { DiagnosticPromptResult, SessionWorkspaceState } from './types'

vi.mock('./LocalInputs', async original => ({ ...await original<typeof import('./LocalInputs')>(),
  HandsFreeVoiceInput: () => null, VoiceInputButton: () => null }))
vi.mock('./VoiceCalibrationPanel', () => ({ VoiceCalibrationPanel: () => null }))
vi.mock('./ContinuousDocumentMonitor', () => ({ ContinuousDocumentMonitor: () => null }))
vi.mock('./DocumentCameraCapture', () => ({ DocumentCameraCapture: () => null }))
const now='2026-09-10T08:00:00Z', question='方程 x²+x+1=0 的判别式是多少？只写出判别式数值。'
function workspace(child='child-a',sid='session-a'):SessionWorkspaceState {
  return {
    session:{session_id:sid,child_id:child,status:'active',state:'study',started_at:now,last_checkpoint_at:now,
      observation_revision: 0, observation_generation: 'a'.repeat(64), observation_status:'paused',snooze_until:null,recall_declared_empty:0,homework_declared_empty:0},
    family_rule:{child_id:child,latest_end_time:'21:00',max_learning_minutes:120,task_segment_minutes:35,break_minutes:5,
      pet_min_minutes:0,competition_minutes:0,observation_mode:'shadow',reminder_budget_per_10m:0,
      misclassification_shadow_threshold:3,version:1,updated_at:now},
    planning_context:{energy_level:'medium',fixed_commitment_minutes:0,fixed_commitment_note:null,
      tomorrow_event_subject:null,tomorrow_event_title:null,tomorrow_event_minutes:0,updated_at:now},
    recalls:[],homework:[],carryover_candidates:[],carryover_decisions:[],drafts:{},latest_tutor_turn:null,daily_report_ready:false,
    plan:{plan_id:'plan-a',plan_date:'2026-09-10',status:'active',total_minutes:15,generator:'local_rule_engine',version:1},
    plan_items:[{plan_item_id:'original-task',homework_id:null,subject:'数学',title:'根的判别式',expected_minutes:15,
      actual_minutes:null,priority:1,status:'paused',position:1,started_at:now,completed_at:null,active_seconds:3,
      active_segment_started_at:null,source_type:'junior_exam_goal',source_ref:'original-cycle',completion_standard:'原练习',
      cognitive_load:'medium',task_type:'test_prep',completion_evidence_type:null,completion_note:null,
      completion_checklist:[],completion_artifact_id:null}],
    constraint_decisions:[{decision_id:'original-receipt',obligation_type:'junior_exam_goal',source_ref:'original-cycle',
      decision:'scheduled',reason:'supply_resume',requested_minutes:15,allocated_minutes:15,created_at:now,
      metadata:{supply_resume:{version:'supply_gap_resume_v1',operation:'supply',child_id:child,session_id:sid,
        plan_id:'plan-a',plan_item_id:'original-task',source_type:'junior_exam_goal',source_ref:'original-cycle',
        diagnostic_item_id:'original-item',item_version:'original-version'}}}],
    sensor_release:{opened_leases:0,active_leases:0,released_leases:0,application_release_confirmed:false,
      evidence_level:'not_opened',os_release_verified:false,release_verification:null,by_device:{}},
  }
}
function prompt():DiagnosticPromptResult{return {status:'waiting_answer',reason:null,attempt:null,prompt:{
  prompt_id:'original-prompt',diagnostic_item_id:'original-item',item_version:'original-version',review_task_id:null,
  child_id:'child-a',origin_session_id:'session-a',created_at:now,item:{diagnostic_item_id:'original-item',
    track:'school',knowledge_node_id:'original-node',title:'根的判别式',subject:'数学',difficulty:1,assessment_mode:'objective',
    prompt:question,answer_type:'text',stimulus_text:null,stimulus_asset:null}}}}
beforeEach(()=>{
  vi.stubGlobal('fetch',vi.fn().mockRejectedValue(Error('UNEXPECTED_NETWORK')))
  vi.stubGlobal('desktop',{getWakeWordSettings:vi.fn(async()=>({childId:'child-a',wakeWord:'小知',enabled:true,inactivitySeconds:60,updatedAt:now})),
    getVoiceOnboarding:vi.fn(async()=>({childId:'child-a',version:1,step:'completed',updatedAt:now}))})
  vi.spyOn(voice,'getCompanionVoiceState').mockReturnValue('idle')
  vi.spyOn(inputs,'speakLocally').mockImplementation((_text,_sid,done)=>done?.('text_only'))
  vi.spyOn(inputs,'cancelLocalSpeech').mockImplementation(()=>undefined)
  vi.spyOn(localApi,'sessionWorkspace').mockResolvedValue(workspace())
  vi.spyOn(localApi,'nextDiagnostic').mockResolvedValue({item:null,pet_baseline:null})
  vi.spyOn(localApi,'prepareDiagnosticPrompt').mockResolvedValue(prompt())
  vi.spyOn(localApi,'diagnosticPrompt').mockResolvedValue(prompt())
  vi.spyOn(localApi,'executeAgentTurn').mockRejectedValue(Error('NO_EXECUTION_EXPECTED'))
})
afterEach(()=>{cleanup();localStorage.clear();vi.restoreAllMocks();vi.unstubAllGlobals()})
const mount=(value=workspace())=>render(<SessionWorkspace key={value.session.child_id+value.session.session_id} initial={value} childName="同学" childGrade="初三" onBack={vi.fn()}/> )
it('restores the exact paused school supply prompt on mount without prepare/play/start',async()=>{
  mount();await screen.findByText(question)
  expect(localApi.diagnosticPrompt).toHaveBeenCalledWith('session-a')
  expect(localApi.nextDiagnostic).not.toHaveBeenCalled();expect(localApi.prepareDiagnosticPrompt).not.toHaveBeenCalled()
  expect(localApi.executeAgentTurn).not.toHaveBeenCalled()
  expect(vi.mocked(inputs.speakLocally).mock.calls.map(c=>c[0]).join(' ')).not.toContain(question)
})
it.each(['child','item','version','withdrawn'] as const)('does not show a %s mismatched current prompt',async fault=>{
  const result=prompt();if(fault==='child')result.prompt!.child_id='another-child'
  if(fault==='item')result.prompt!.diagnostic_item_id='another-item'
  if(fault==='version')result.prompt!.item_version='another-version'
  if(fault==='withdrawn')result.status='invalidated'
  vi.mocked(localApi.diagnosticPrompt).mockResolvedValue(result)
  mount();await waitFor(()=>expect(localApi.diagnosticPrompt).toHaveBeenCalled())
  expect(screen.queryByText(question)).toBeNull();expect(localApi.prepareDiagnosticPrompt).not.toHaveBeenCalled()
})
it('does not apply a late original read to a different child/session',async()=>{
  let resolve!:(v:DiagnosticPromptResult)=>void
  vi.mocked(localApi.diagnosticPrompt).mockReturnValueOnce(new Promise<DiagnosticPromptResult>(r=>{resolve=r})).mockResolvedValue({status:'no_item',prompt:null,attempt:null,reason:null})
  const view=mount();await waitFor(()=>expect(localApi.diagnosticPrompt).toHaveBeenCalledWith('session-a'))
  const other=workspace('child-b','session-b')
  await act(async()=>view.rerender(<SessionWorkspace key="child-bsession-b" initial={other} childName="另一位同学" childGrade="初三" onBack={vi.fn()}/>))
  await act(async()=>resolve(prompt()));expect(screen.queryByText(question)).toBeNull()
})
