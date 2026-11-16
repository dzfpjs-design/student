import {cleanup,fireEvent,render,screen,waitFor,within} from '@testing-library/react'
import {afterEach,expect,it,vi} from 'vitest'
import {ApiError,localApi} from './api'
import * as api from './api'
import {ParentReportDeliveryPanel} from './ParentReportDeliveryPanel'
import type {ParentReportSnapshot} from './types'
const snapshot: ParentReportSnapshot = {
  snapshot_id: 'snapshot-1', source_kind: 'daily_report', source_id: 'report-1', report_id: 'report-1', child_id: 'child-1', report_type: 'daily',
  source_version: 'cloud_report_v1', template_version: 'parent_report_snapshot_v1', evidence_version: 'sha256:evidence', status: 'draft', created_at: '2026-08-27T10:00:00Z', approved_at: null,
  facts: { facts: ['计划20分钟，已记录完成18分钟'] }, inferences: { interpretation: '只根据确认记录。', limitations: ['不评价能力。'] },
  content: { headline: '完成 1 项计划', report_date: '2026-08-27', summary: { headline: '完成 1 项计划', facts: ['计划20分钟，已记录完成18分钟'], interpretation: '只根据确认记录。', evidence_count: 2 }, parent_coaching: { focus: 'correction', evidence_basis: ['订正记录'], scenario: '只处理一处。', why_this_fits: '只用确认记录。', script: ['先说出已经确定的一步。'], action: { what: '订正第一步', duration_minutes: 8, steps: ['圈出不确定处'], stop_condition: '到时停止。' }, avoid: [{ do_not_say: '怎么又错了？', say_instead: '只看第一处。' }] }, evidence_detail: { recalls: [], tasks: [], knowledge_evidence: [{ subject: '数学', knowledge_title: '判别式', evidence_type: 'diagnostic_attempt', correctness: 0.5, hint_level: 0, independent: true, confidence: 1, mastery_probability: 0.42, mastery_evidence_count: 1, status_confidence: 0.8 }], mistakes: [{ subject: '数学', title: '判别式练习', status: 'remediating', confidence: 0.9, updated_at: '2026-08-27T10:00:00Z' }], reflection: null, declarations: { no_recall: false, no_homework: false } } },
  confidence: { overall: 'limited', uncertainty: '未记录的信息不作推断。', evidence_count: 2 }, parent_edit: { delivery_intro: '', parent_note: '' }, feedback: [], corrections: []
}


afterEach(()=>{cleanup();vi.restoreAllMocks();localStorage.clear()})
const version=(s:string)=>s.repeat(64)
function state(id:string,current:boolean,replacement:string|null=null){return {snapshot_id:id,child_id:'child-1',status:current?'draft' as const:'revoked' as const,source_current:current,replacements:replacement?[{snapshot_id:id,replacement_snapshot_id:replacement,event_id:'public-event',occurred_at:'2026-10-24T10:00:00Z'}]:[],prior_submissions:[],replacement_submissions:[],recipient_ids_requiring_correction:[],correction_notification_required:false,latest_replacement_id:replacement,latest_replacement_availability:replacement?'available' as const:'none' as const,latest_replacement_status:replacement?'draft' as const:null,next_action:'recheck_source' as const,automatic_delivery:false as const,human_delivery_verified:false as const,local_only:true as const,cloud_transmission_allowed:false as const}}
function arrange(){
 const old:ParentReportSnapshot={...structuredClone(snapshot),snapshot_id:'old-monthly',source_kind:'period_report',source_id:'period-october@old',report_type:'monthly',parent_edit_version:version('a'),parent_edit:{delivery_intro:'',parent_note:'原稿已存备注',coaching_decision:'custom',coaching_script:'原稿已存话术',coaching_action:'原稿已存行动',coaching_duration_minutes:2,coaching_observation:'原稿已存观察'},content:{...snapshot.content,headline:'旧版月报',report_date:'2026-10-31'},correction_status:state('old-monthly',false,'new-monthly')}
 const target:ParentReportSnapshot={...structuredClone(old),snapshot_id:'new-monthly',source_id:'period-october@new',parent_edit_version:version('b'),parent_edit:{delivery_intro:'',parent_note:''},content:{...old.content,headline:'更新依据后的月报'},correction_status:state('new-monthly',true)}
 const rows=vi.spyOn(localApi,'reportSnapshots').mockResolvedValueOnce([old]).mockResolvedValue([{...old,status:'revoked'},target])
 vi.spyOn(localApi,'reportRecipients').mockResolvedValue({items:[],preferences:{quiet_start:'21:00',quiet_end:'07:00',retention_days:90,redaction_level:'standard'}})
 vi.spyOn(localApi,'reportDeliveryChannels').mockResolvedValue([]);vi.spyOn(localApi,'reportDeliveries').mockResolvedValue([])
 vi.spyOn(localApi,'reportDeliveryStatus').mockResolvedValue({scheduled:0,processing:0,failed:0,unknown:0,recovered:0});vi.spyOn(localApi,'retractedPeriodEvidence').mockResolvedValue([])
 vi.spyOn(localApi,'parentActions').mockResolvedValue([])
 const detail=vi.spyOn(api,'requestParentLocal').mockImplementation(async(path)=>structuredClone(path.endsWith('/old-monthly')?{...old,status:'revoked'}:target) as never)
 const approve=vi.spyOn(localApi,'approveReportSnapshot').mockImplementation(async()=>({...target,status:'approved',parent_edit:old.parent_edit}))
 return{old,target,rows,detail,approve}
}
async function open(a:ReturnType<typeof arrange>){render(<ParentReportDeliveryPanel childId="child-1"/>);expect(await screen.findByRole('button',{name:'确认并批准'})).toBeDisabled();fireEvent.click(screen.getByRole('button',{name:'查看新版并沿用修改'}));await screen.findByRole('heading',{name:'更新依据后的月报',level:3});await screen.findByRole('region',{name:'核对旧稿已保存修改'});expect(a.approve).not.toHaveBeenCalled()}
function receipt(a:ReturnType<typeof arrange>,payload:Parameters<typeof localApi.carryReportSavedEdits>[1]){return{...a.target,parent_edit:a.old.parent_edit,parent_edit_version:version('a'),edit_transfer:{...payload,target_snapshot_id:'new-monthly',saved_edit_version:version('a'),saved_at:'2026-10-24T10:00:01Z',replayed:false}}}

it('shows exact current evidence and old saved edits, explicitly carries then separately approves',async()=>{
 const a=arrange();await open(a)
 const carry=vi.spyOn(localApi,'carryReportSavedEdits').mockImplementation(async(_id,p)=>receipt(a,p))
 expect(screen.getByText('原稿已存行动')).toBeInTheDocument();expect(carry).not.toHaveBeenCalled()
 fireEvent.click(screen.getByRole('button',{name:'沿用已保存修改到这份新版'}))
 await screen.findByText('修改已保存到新版。请核对新版证据和修改内容，再单独确认并批准。')
 expect(carry).toHaveBeenCalledTimes(1);expect(a.approve).not.toHaveBeenCalled()
 expect(screen.getByRole('textbox',{name:'修改今天行动'})).toHaveValue('原稿已存行动')
 fireEvent.click(screen.getByRole('button',{name:'确认并批准'}));await waitFor(()=>expect(a.approve).toHaveBeenCalledTimes(1))
 expect(a.approve.mock.calls[0][0]).toBe('new-monthly')
})

it('keeps an unknown transfer command through remount and reuses it once without approving',async()=>{
 const a=arrange();await open(a)
 const carry=vi.spyOn(localApi,'carryReportSavedEdits').mockRejectedValueOnce(new Error('响应未知')).mockImplementation(async(_id,p)=>receipt(a,p))
 fireEvent.click(screen.getByRole('button',{name:'沿用已保存修改到这份新版'}));await screen.findByText('响应未知')
 const first=structuredClone(carry.mock.calls[0]);expect(localStorage.getItem('hsc:report-saved-edit-carry:child-1')).not.toBeNull()
 cleanup();render(<ParentReportDeliveryPanel childId="child-1"/>);fireEvent.click(await screen.findByRole('button',{name:'核对上次沿用结果'}))
 await screen.findByText('修改已保存到新版。请核对新版证据和修改内容，再单独确认并批准。')
 expect(carry.mock.calls[1]).toEqual(first);expect(a.approve).not.toHaveBeenCalled();expect(localStorage.getItem('hsc:report-saved-edit-carry:child-1')).toBeNull()
})

it.each(['child','root','date','type'] as const)('rejects a different %s replacement without carrying or approving',async wrong=>{
 const a=arrange();const bad={...a.target,...(wrong==='child'?{child_id:'other'}:wrong==='root'?{source_id:'other-period@new'}:wrong==='type'?{report_type:'weekly' as const}:{content:{...a.target.content,report_date:'2026-09-30'}})}
 a.detail.mockImplementation(async(path)=>structuredClone(path.endsWith('/old-monthly')?{...a.old,status:'revoked'}:bad) as never)
 const carry=vi.spyOn(localApi,'carryReportSavedEdits');render(<ParentReportDeliveryPanel childId="child-1"/>);fireEvent.click(await screen.findByRole('button',{name:'查看新版并沿用修改'}))
 await screen.findByText('新版归属、周期或当前证据未能核对；没有沿用或覆盖任何修改。');expect(carry).not.toHaveBeenCalled();expect(a.approve).not.toHaveBeenCalled()
})

it('returns to the intact old draft after a definite CAS conflict without overwriting the new draft',async()=>{
 const a=arrange();await open(a);const carry=vi.spyOn(localApi,'carryReportSavedEdits').mockRejectedValue(new ApiError('两稿均保留',409,'REPORT_EDIT_CARRY_NOT_APPLIED'))
 fireEvent.click(screen.getByRole('button',{name:'沿用已保存修改到这份新版'}));await screen.findByText('两稿均保留')
 expect(carry).toHaveBeenCalledTimes(1);expect(a.approve).not.toHaveBeenCalled();expect(localStorage.getItem('hsc:report-saved-edit-carry:child-1')).toBeNull()
 expect(screen.getByRole('heading',{name:'旧版月报',level:3})).toBeInTheDocument()
})


it('keeps unsaved old-draft input instead of refreshing and switching versions',async()=>{
 const a=arrange();render(<ParentReportDeliveryPanel childId="child-1"/>);await screen.findByRole('heading',{name:'旧版月报',level:3})
 const input=screen.getByRole('textbox',{name:'补充备注'});fireEvent.change(input,{target:{value:'刚输入尚未保存的旧稿备注'}})
 const count=a.rows.mock.calls.length
 fireEvent.click(screen.getByRole('button',{name:'查看新版并沿用修改'}))
 expect(a.rows.mock.calls.length).toBe(count);expect(input).toHaveValue('刚输入尚未保存的旧稿备注')
 expect(screen.getByRole('button',{name:'查看新版并沿用修改'})).toBeDisabled()
 expect(screen.getByText('当前有未保存的修改。请先保存采用方式或恢复已保存内容，再查看新版或沿用旧稿。')).toBeInTheDocument()
})

it.each(['补充备注','修改今天行动'])('keeps unsaved current %s and sends no carry request',async label=>{
 const a=arrange();await open(a);const carry=vi.spyOn(localApi,'carryReportSavedEdits').mockImplementation(async(_id,p)=>receipt(a,p))
 if(label==='修改今天行动')fireEvent.click(screen.getByRole('button',{name:'修改后采用'}))
 const input=screen.getByRole('textbox',{name:label});fireEvent.change(input,{target:{value:'新版刚输入尚未保存'}})
 fireEvent.click(screen.getByRole('button',{name:'沿用已保存修改到这份新版'}))
 expect(carry).not.toHaveBeenCalled();expect(input).toHaveValue('新版刚输入尚未保存')
 expect(screen.getByRole('button',{name:'沿用已保存修改到这份新版'})).toBeDisabled()
 fireEvent.click(screen.getByRole('button',{name:'恢复已保存内容'}))
 await waitFor(()=>expect(screen.getByRole('button',{name:'沿用已保存修改到这份新版'})).toBeEnabled())
 expect(carry).not.toHaveBeenCalled();expect(a.approve).not.toHaveBeenCalled()
})

it('freezes every human field and manual save while an unknown carry is pending',async()=>{
 const a=arrange();await open(a);const carry=vi.spyOn(localApi,'carryReportSavedEdits').mockRejectedValueOnce(new Error('原结果未知')).mockImplementation(async(_id,p)=>receipt(a,p))
 fireEvent.click(screen.getByRole('button',{name:'沿用已保存修改到这份新版'}));await screen.findByText('原结果未知')
 for(const name of ['采用建议','修改后采用','本次跳过','保存采用方式'])expect(screen.getByRole('button',{name})).toBeDisabled()
 for(const name of ['补充备注','开场白','观察与明天调整'])expect(screen.getByRole('textbox',{name})).toBeDisabled()
 fireEvent.click(screen.getByRole('button',{name:'修改后采用'}));expect(screen.queryByRole('textbox',{name:'修改今天行动'})).not.toBeInTheDocument()
 const first=structuredClone(carry.mock.calls[0]);fireEvent.click(screen.getByRole('button',{name:'核对上次沿用结果'}));await screen.findByText('修改已保存到新版。请核对新版证据和修改内容，再单独确认并批准。')
 expect(carry.mock.calls[1]).toEqual(first);expect(screen.getByRole('textbox',{name:'修改今天行动'})).toHaveValue('原稿已存行动');expect(a.approve).not.toHaveBeenCalled()
})


it('R78 stale-self normal refresh keeps saved work during delayed list then explicitly carries and approves',async()=>{
 const a=arrange()
 const stale:ParentReportSnapshot={...a.old,status:'draft',correction_status:{...state(a.old.snapshot_id,false),status:'draft',latest_replacement_id:null,latest_replacement_availability:'none',latest_replacement_status:null}}
 const oldAfter={...a.old,status:'revoked' as const,correction_status:state(a.old.snapshot_id,false,a.target.snapshot_id)}
 let resolveList!:(v:ParentReportSnapshot[])=>void
 const pending=new Promise<ParentReportSnapshot[]>(resolve=>{resolveList=resolve})
 a.rows.mockReset();a.rows.mockResolvedValueOnce([stale]).mockReturnValueOnce(pending).mockResolvedValue([oldAfter,a.target])
 a.detail.mockImplementation(async(path)=>structuredClone(path.endsWith('/old-monthly')?oldAfter:a.target)as never)
 const carry=vi.spyOn(localApi,'carryReportSavedEdits').mockImplementation(async(_id,p)=>receipt(a,p))
 render(<ParentReportDeliveryPanel childId="child-1"/>)
 await screen.findByRole('heading',{name:'旧版月报',level:3})
 expect(screen.getByText('新版尚未就绪；旧报告不会自动重新发送。')).toBeInTheDocument()
 expect(screen.getByRole('button',{name:'确认并批准'})).toBeDisabled()
 fireEvent.click(screen.getByRole('button',{name:'查看新版并沿用修改'}))
 await waitFor(()=>expect(a.rows).toHaveBeenCalledTimes(2))
 expect(screen.getByRole('heading',{name:'旧版月报',level:3})).toBeInTheDocument()
 expect(screen.getByRole('textbox',{name:'补充备注'})).toHaveValue(a.old.parent_edit.parent_note)
 expect(carry).not.toHaveBeenCalled();expect(a.approve).not.toHaveBeenCalled()
 resolveList([oldAfter,a.target])
 await screen.findByRole('heading',{name:'更新依据后的月报',level:3})
 await screen.findByRole('region',{name:'核对旧稿已保存修改'})
 expect(screen.getByText(a.old.parent_edit.coaching_action!)).toBeInTheDocument()
 expect(carry).not.toHaveBeenCalled();expect(a.approve).not.toHaveBeenCalled()
 fireEvent.click(screen.getByRole('button',{name:'沿用已保存修改到这份新版'}))
 await screen.findByText('修改已保存到新版。请核对新版证据和修改内容，再单独确认并批准。')
 expect(carry).toHaveBeenCalledTimes(1);expect(carry.mock.calls[0][0]).toBe(a.target.snapshot_id)
 expect(a.approve).not.toHaveBeenCalled();expect(screen.getByRole('textbox',{name:'修改今天行动'})).toHaveValue(a.old.parent_edit.coaching_action)
 fireEvent.click(screen.getByRole('button',{name:'确认并批准'}));await waitFor(()=>expect(a.approve).toHaveBeenCalledTimes(1))
 expect(a.approve.mock.calls[0][0]).toBe(a.target.snapshot_id)
})
