import { cleanup, fireEvent, render, screen, waitFor, within, act } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { localApi } from './api'
import { HistoricalParentActionFeedback } from './ParentActionFeedbackPanel'
import { ParentReportDeliveryPanel } from './ParentReportDeliveryPanel'
import type { ParentActionSnapshot, ParentReportSnapshot, ParentReportFeedback } from './types'

const action: ParentActionSnapshot = { binding_status:'bound', snapshot_id:'public-action', report_id:null,
  period_report_id:'public-week', source_kind:'period_report', source_id:'public-week', child_id:'public-child',
  version:1, fingerprint:'public', feedback:null, execution_allowed:true,
  review_binding:{snapshot_id:'public-review',source_version:'public-v1',approval_event_id:'public-approval',decision:'custom',edit_fingerprint:'public-edit',base_action_fingerprint:'public-base'},
  feedback_eligibility:{record_allowed:true,approval_witness_valid:true,current_execution_granted:false,reason:'system_superseded_approval'},
  timing:{report_date:'2026-09-07',when:null,duration_minutes:2,planned_at:null},
  coaching:{focus:'correction',scenario:'公开合成场景',why_this_fits:'公开合成',script:['公开合成话术'],avoid:[],action:{what:'公开合成原批准行动',duration_minutes:2,steps:['公开合成一步'],stop_condition:'停止即结束'}} }
const review: ParentReportSnapshot = { snapshot_id:'public-review',source_kind:'period_report',source_id:'public-week@old',report_id:null,
  child_id:'public-child',report_type:'weekly',source_version:'public-v1',template_version:'public',evidence_version:'sha256:public',
  facts:{},inferences:{limitations:[]},confidence:{overall:'limited',uncertainty:'仅合成测试'},
  content:{headline:'公开合成周报告',report_date:'2026-09-07',summary:{},parent_coaching:action.coaching,evidence_detail:{}},
  status:'revoked',revoke_reason:'后续证据已更新；请审核新版草稿',parent_edit:{delivery_intro:'',parent_note:''},feedback:[],corrections:[],
  created_at:'2026-09-07T00:00:00Z',approved_at:'2026-09-07T00:01:00Z',
  parent_action:{binding_status:'unknown',reason:'review_source_not_current'},historical_parent_actions:[action] }

afterEach(()=>{cleanup();localStorage.clear();vi.restoreAllMocks()})

it('the original report screen offers historical feedback after report experience, without approve/send/execute authority',async()=>{
  vi.spyOn(localApi,'reportSnapshots').mockResolvedValue([review])
  vi.spyOn(localApi,'reportRecipients').mockResolvedValue({items:[],preferences:{quiet_start:'21:00',quiet_end:'07:00',retention_days:90,redaction_level:'standard'}})
  vi.spyOn(localApi,'reportDeliveryChannels').mockResolvedValue([])
  vi.spyOn(localApi,'reportDeliveries').mockResolvedValue([])
  vi.spyOn(localApi,'reportDeliveryStatus').mockResolvedValue({scheduled:0,processing:0,failed:0,unknown:0,recovered:0})
  vi.spyOn(localApi,'reportFeedbackHistory').mockResolvedValue([])
  const save=vi.spyOn(localApi,'saveParentActionFeedback').mockResolvedValue({revision:1,operation:'record',executed:false} as ParentReportFeedback)
  const approve=vi.spyOn(localApi,'approveReportSnapshot'), send=vi.spyOn(localApi,'scheduleReportDelivery')
  render(<ParentReportDeliveryPanel childId="public-child" />)
  const history=await screen.findByRole('region',{name:'历史已批准行动反馈'})
  fireEvent.click(within(history).getByText(/第1版 · 公开合成原批准行动/))
  expect(within(history).getByRole('note')).toHaveTextContent('不会重新批准执行')
  fireEvent.change(within(history).getByLabelText('执行情况'),{target:{value:'no'}})
  fireEvent.click(within(history).getByRole('button',{name:'保存行动反馈'}))
  await waitFor(()=>expect(save).toHaveBeenCalledWith('public-child','public-action',expect.objectContaining({executed:false,expected_revision:0})))
  expect(approve).not.toHaveBeenCalled();expect(send).not.toHaveBeenCalled()
})

it('explicit withdrawal blocks a new record but still lets the parent retract the original version',async()=>{
  const withdrawn={...action,feedback:{revision:1,operation:'record',executed:false} as ParentReportFeedback,
    feedback_eligibility:{...action.feedback_eligibility!,record_allowed:false,reason:'explicit_parent_withdrawal' as const}}
  const save=vi.spyOn(localApi,'saveParentActionFeedback').mockResolvedValue({revision:2,operation:'retract'} as ParentReportFeedback)
  render(<HistoricalParentActionFeedback childId="public-child" actions={[withdrawn]} />)
  fireEvent.click(screen.getByText(/第1版 · 公开合成原批准行动/))
  expect(screen.getByRole('button',{name:'保存行动反馈'})).toBeDisabled()
  fireEvent.change(screen.getByLabelText('撤回原因'),{target:{value:'公开合成撤回'}})
  fireEvent.click(screen.getByRole('button',{name:'撤回这次反馈'}))
  await waitFor(()=>expect(save).toHaveBeenCalledWith('public-child','public-action',expect.objectContaining({operation:'retract',expected_revision:1})))
})

it('distinct original versions remain independently editable and forged/other-child entries are never offered',async()=>{
  const save=vi.spyOn(localApi,'saveParentActionFeedback').mockResolvedValue({revision:1,operation:'record',executed:false} as ParentReportFeedback)
  render(<HistoricalParentActionFeedback childId="public-child" actions={[action,{...action,snapshot_id:'second',version:2},
    {...action,snapshot_id:'forged',feedback_eligibility:{...action.feedback_eligibility!,approval_witness_valid:false}},
    {...action,snapshot_id:'foreign',child_id:'other-child'}]} />)
  for(const s of screen.getAllByText(/第[12]版 · 公开合成原批准行动/))fireEvent.click(s)
  const panels=screen.getAllByRole('region',{name:'行动版本反馈',hidden:true})
  expect(panels).toHaveLength(2)
  for(const p of panels){fireEvent.change(within(p).getByLabelText('执行情况'),{target:{value:'no'}});fireEvent.click(within(p).getByRole('button',{name:'保存行动反馈',hidden:true}))}
  await waitFor(()=>expect(save).toHaveBeenCalledTimes(2))
  expect(save.mock.calls.map(x=>x[1])).toEqual(['public-action','second'])
})

it('daily history late reads from a previous child cannot cross into the next child view',async()=>{
  let resolve!:(value:ParentActionSnapshot[])=>void
  vi.spyOn(localApi,'parentActions').mockReturnValueOnce(new Promise(r=>{resolve=r})).mockResolvedValueOnce([])
  const view=render(<HistoricalParentActionFeedback childId="public-child" reportId="public-daily" />)
  view.rerender(<HistoricalParentActionFeedback childId="other-child" reportId="other-daily" />)
  await act(async()=>{resolve([{...action,report_id:'public-daily'}])})
  expect(screen.queryByText(/第1版 · 公开合成原批准行动/)).not.toBeInTheDocument()
  expect(screen.queryByRole('region',{name:'行动版本反馈'})).not.toBeInTheDocument()
  expect(screen.getByText('当前没有可补记的历史行动。')).toBeInTheDocument()
})

it('daily history offers read-only retry after an initial failure, without resubmitting feedback',async()=>{
  const read=vi.spyOn(localApi,'parentActions').mockRejectedValueOnce(new Error('temporary read failure')).mockResolvedValueOnce([{...action,report_id:'public-daily'}])
  const save=vi.spyOn(localApi,'saveParentReportFeedback'),approve=vi.spyOn(localApi,'approveReportSnapshot')
  render(<HistoricalParentActionFeedback childId="public-child" reportId="public-daily" />)
  expect(await screen.findByText(/历史行动反馈暂不可读取/)).toBeInTheDocument()
  expect(read).toHaveBeenCalledTimes(1)
  fireEvent.click(screen.getByRole('button',{name:'重新读取历史行动'}))
  expect(await screen.findByText(/第1版 · 公开合成原批准行动/)).toBeInTheDocument()
  expect(read).toHaveBeenCalledTimes(2);expect(save).not.toHaveBeenCalled();expect(approve).not.toHaveBeenCalled()
})

it('the same report reloads its history when the active binding becomes unknown',async()=>{
  const read=vi.spyOn(localApi,'parentActions').mockResolvedValue([{...action,report_id:'public-daily'}])
  const view=render(<HistoricalParentActionFeedback childId="public-child" reportId="public-daily" excludeSnapshotId="public-action" />)
  await screen.findByText('当前没有可补记的历史行动。')
  expect(read).toHaveBeenCalledTimes(1)
  view.rerender(<HistoricalParentActionFeedback childId="public-child" reportId="public-daily" excludeSnapshotId={undefined} />)
  expect(await screen.findByText(/第1版 · 公开合成原批准行动/)).toBeInTheDocument()
  expect(read).toHaveBeenCalledTimes(2)
})
