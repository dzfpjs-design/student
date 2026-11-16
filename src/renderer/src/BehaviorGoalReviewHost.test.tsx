import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { webcrypto } from 'node:crypto'
import * as api from './api'
import { operationIdentity, readPrivacyIntent } from './privacyOperationRecovery'
import { BehaviorEvidencePanel, PrivacyOperations } from './ParentCenter'
import { behaviorGoalLocalKey } from './behaviorGoalApi'
import { goalView } from './BehaviorGoalReviewPanel.fixtures'
import type { PrivacyPreferences, SystemHealth } from './types'

afterEach(()=>{cleanup();localStorage.clear();window.desktop=undefined;vi.restoreAllMocks();vi.unstubAllGlobals()})
it('shows actual replay alternatives without crashing the parent experiment entry',async()=>{
  vi.spyOn(api.localApi,'behaviorReplay').mockResolvedValue({events:[{event_id:'observed',event_type:'observation.sample',confidence:1,occurred_at:'2026-08-28T10:00:00Z',alternatives:['观察只覆盖片段，不能代表完整行为。']}],states:[],interventions:[]} as never)
  vi.spyOn(api.localApi,'behaviorBaseline').mockResolvedValue({status:'insufficient_history',days_observed:0})
  vi.spyOn(api.localApi,'behaviorPersonalization').mockResolvedValue({boundary:'本机证据',items:[]} as never)
  vi.spyOn(api.localApi,'behaviorHabitExperiments').mockResolvedValue({boundary:'只补充具体机会',items:[{experiment_id:'goal',title:'具体目标',child_action:'先圈步骤',success_signal:'具体说明',status:'completed',outcome:null}]} as never)
  render(<BehaviorEvidencePanel childId="a"/>);fireEvent.click(screen.getByRole('button',{name:'查看证据'}))
  expect(await screen.findByText('观察只覆盖片段，不能代表完整行为。')).toBeInTheDocument()
  expect(screen.getByRole('button',{name:'核对具体机会'})).toBeInTheDocument()
})
it('mounts original habit opportunities, including a fourth historic experiment; durable feedback refreshes habit and ChildModel reads',async()=>{
  vi.spyOn(api.localApi,'behaviorReplay').mockResolvedValue({events:[],states:[],interventions:[]} as never)
  vi.spyOn(api.localApi,'behaviorBaseline').mockResolvedValue({status:'insufficient_history',days_observed:0})
  vi.spyOn(api.localApi,'behaviorPersonalization').mockResolvedValue({boundary:'本机证据',items:[]} as never)
  const habits=vi.spyOn(api.localApi,'behaviorHabitExperiments').mockResolvedValue({boundary:'只补充具体机会',items:[0,1,2,3].map(i=>({experiment_id:i===3?'goal':`goal-${i}`,title:`实验${i+1}`,child_action:'先圈步骤',success_signal:'具体说明',status:'completed',outcome:null}))} as never)
  const request=vi.spyOn(api,'requestParentLocal').mockImplementation(async(path,init)=>{
    if(path.endsWith('longitudinal-model')) return {child_id:'a',is_current:true,evidence_fingerprint:'model',payload:{behavior:{}}} as never
    if(init?.method==='POST') return {status:'recorded',event_id:'event',replayed:false,historical_receipt:false,local_only:true,cloud_transmission_allowed:false} as never
    return goalView() as never
  })
  render(<BehaviorEvidencePanel childId="a"/>);fireEvent.click(screen.getByRole('button',{name:'查看证据'}))
  const title=await screen.findByText('实验4');const row=within(title.closest('article')!)
  fireEvent.click(row.getByRole('button',{name:'核对具体机会'}))
  const select=await row.findByLabelText('选择需要补充的机会')
  fireEvent.change(select,{target:{value:'opportunity'}})
  fireEvent.click(row.getByRole('button',{name:'保存这次补充'}))
  await row.findByText(/成长模型已按当前证据重读/)
  expect(habits).toHaveBeenCalledTimes(2)
  expect(request.mock.calls.some(([p])=>p.endsWith('/longitudinal-model'))).toBe(true)
})

const health={database_integrity:'ok',devices:[],device_incidents:[],recent_deletion_failures:[],recent_agent_failures:[],recent_report_failures:[],recent_model_failures:[],maintenance:{},recent_maintenance_failures:[]} as unknown as SystemHealth
const preferences={cloud_model_text_allowed:false,cloud_audio_allowed:false,raw_media_storage_allowed:false,model_improvement_allowed:false,model_call_retention_days:90,tutor_turn_retention_days:180,report_retention_days:730,updated_at:'2026-09-08T00:00:00Z'} as PrivacyPreferences
function privacy() {
  vi.stubGlobal('crypto', webcrypto)
  const generation = '50617198-ed7b-4865-a98f-5b7c1b44532e'
  vi.spyOn(api.localApi, 'privacyOperationGeneration').mockResolvedValue({ generation })
  vi.spyOn(api.localApi, 'privacyOperationReceipt').mockImplementation(async requestId => {
    const intent = readPrivacyIntent()!
    expect(intent.requestId).toBe(requestId)
    const method = intent.kind === 'delete' ? 'deleteChildData' : intent.kind === 'restore' ? 'restoreBackup' : 'restorePortableBackup'
    const original = vi.mocked(api.localApi[method])
    const args = original.mock.calls.at(-1)!
    expect(args.at(-1)).toEqual(operationIdentity(intent))
    expect(intent.generation).toBe(generation)
    const result = original.mock.results.at(-1)!
    expect(result.type).toBe('return')
    let committed = true
    try { await result.value } catch { committed = false }
    const current = committed && intent.kind !== 'delete' ? '6052e5c0-a6b1-4c15-ab74-1d72530ec58e' : generation
    return { request_id: requestId, kind: intent.kind, child_id: intent.childId, scope: intent.scope,
      origin_generation: generation, result_generation: committed ? current : null, current_generation: current,
      cleanup_manifest_sha256: intent.manifestSha256, status: committed ? 'committed' : 'failed', cleanup_allowed: committed,
      reference: committed ? { type: intent.kind === 'delete' ? 'deletion_job' : 'audit_event', id: 'original-operation-receipt' } : null,
      local_only: true, cloud_transmission_allowed: false }
  })
  vi.spyOn(api.localApi,'listBackups').mockResolvedValue([{backup_id:'backup',created_at:'2026-09-08T00:00:00Z',reason:'manual',size_bytes:1024,integrity_status:'ok'}] as never)
  vi.spyOn(api.localApi,'captureArtifacts').mockResolvedValue([])
  vi.spyOn(api.localApi,'systemHealth').mockResolvedValue(health)
  vi.spyOn(api.localApi,'deletionPreview').mockResolvedValue({habit_experiment_event:1})
  window.desktop={choosePortableBackup:vi.fn().mockResolvedValue({path:'/isolated/backup.hscbackup'})} as never
  localStorage.setItem(behaviorGoalLocalKey('a','goal'),'pending-a');localStorage.setItem(behaviorGoalLocalKey('b','goal'),'pending-b')
  localStorage.setItem('hsc:unrelated','keep')
  render(<PrivacyOperations childId="a" initial={preferences} health={health} onHealthChange={vi.fn()}/>)
}
async function perform(scope:string) {
  await screen.findByRole('option',{name:/家长手动/})
  if(scope==='local') {
    fireEvent.change(screen.getByLabelText('恢复确认'),{target:{value:'确认恢复'}});fireEvent.click(screen.getByRole('button',{name:'恢复'}))
  } else if(scope==='portable') {
    fireEvent.click(screen.getByRole('button',{name:'选择迁移包'}));await screen.findByText('已选择迁移包')
    fireEvent.change(screen.getByLabelText('迁移备份口令'),{target:{value:'Synthetic isolated secret'}})
    fireEvent.change(screen.getByLabelText('迁移恢复确认'),{target:{value:'确认迁移恢复'}});fireEvent.click(screen.getByRole('button',{name:'迁移恢复'}))
  } else {
    fireEvent.change(screen.getByLabelText('删除范围'),{target:{value:scope}});fireEvent.change(screen.getByLabelText('删除确认'),{target:{value:'确认删除'}})
    fireEvent.click(screen.getByRole('button',{name:'删除'}))
  }
}
it.each(['local','portable','learning','media','knowledge','all_history'])('clears only matching behavior recovery after successful %s',async scope=>{
  vi.spyOn(api.localApi,scope==='local'?'restoreBackup':scope==='portable'?'restorePortableBackup':'deleteChildData').mockResolvedValue({deleted_counts:{},recovery_rotation:{invalidated_backup_count:0}} as never)
  privacy();await perform(scope)
  await waitFor(()=>expect(localStorage.getItem(behaviorGoalLocalKey('a','goal'))).toBeNull())
  expect(localStorage.getItem(behaviorGoalLocalKey('b','goal'))).toBe(['local','portable'].includes(scope)?null:'pending-b')
  expect(localStorage.getItem('hsc:unrelated')).toBe('keep')
})
it('preserves recovery after deletion transaction fails',async()=>{
  vi.spyOn(api.localApi,'deleteChildData').mockRejectedValue(new api.ApiError('删除未完成',409))
  privacy();await perform('media');await screen.findByText('删除未完成')
  expect(localStorage.getItem(behaviorGoalLocalKey('a','goal'))).toBe('pending-a')
})
