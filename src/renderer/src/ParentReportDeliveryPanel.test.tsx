import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError, localApi } from './api'
import * as apiModule from './api'
import { ParentReportDeliveryPanel, clearReportPeriodEvidenceLocalState } from './ParentReportDeliveryPanel'
import type { ParentReportSnapshot, ParentActionSnapshot, ParentReportFeedback, ReportDelivery } from './types'

const snapshot: ParentReportSnapshot = {
  snapshot_id: 'snapshot-1', source_kind: 'daily_report', source_id: 'report-1', report_id: 'report-1', child_id: 'child-1', report_type: 'daily',
  source_version: 'cloud_report_v1', template_version: 'parent_report_snapshot_v1', evidence_version: 'sha256:evidence', status: 'draft', created_at: '2026-08-27T10:00:00Z', approved_at: null,
  facts: { facts: ['计划20分钟，已记录完成18分钟'] }, inferences: { interpretation: '只根据确认记录。', limitations: ['不评价能力。'] },
  content: { headline: '完成 1 项计划', report_date: '2026-08-27', summary: { headline: '完成 1 项计划', facts: ['计划20分钟，已记录完成18分钟'], interpretation: '只根据确认记录。', evidence_count: 2 }, parent_coaching: { focus: 'correction', evidence_basis: ['订正记录'], scenario: '只处理一处。', why_this_fits: '只用确认记录。', script: ['先说出已经确定的一步。'], action: { what: '订正第一步', duration_minutes: 8, steps: ['圈出不确定处'], stop_condition: '到时停止。' }, avoid: [{ do_not_say: '怎么又错了？', say_instead: '只看第一处。' }] }, evidence_detail: { recalls: [], tasks: [], knowledge_evidence: [{ subject: '数学', knowledge_title: '判别式', evidence_type: 'diagnostic_attempt', correctness: 0.5, hint_level: 0, independent: true, confidence: 1, mastery_probability: 0.42, mastery_evidence_count: 1, status_confidence: 0.8 }], mistakes: [{ subject: '数学', title: '判别式练习', status: 'remediating', confidence: 0.9, updated_at: '2026-08-27T10:00:00Z' }], reflection: null, declarations: { no_recall: false, no_homework: false } } },
  confidence: { overall: 'limited', uncertainty: '未记录的信息不作推断。', evidence_count: 2 }, parent_edit: { delivery_intro: '', parent_note: '' }, feedback: [], corrections: []
}

describe('period evidence pending actions', () => {
  afterEach(() => { cleanup(); window.localStorage.clear(); vi.restoreAllMocks() })
  function arrange() {
    const weekly: ParentReportSnapshot = { ...structuredClone(snapshot), report_type: 'weekly',
      facts: { learning_summary: { evidence: [{ evidence_ref: 'record-cycle', subject: '数学', knowledge_title: '角度', summary: '已记录作答', date: '2026-09-07' }] } } }
    const list = vi.spyOn(localApi, 'reportSnapshots').mockResolvedValue([weekly])
    vi.spyOn(localApi, 'reportRecipients').mockResolvedValue({ items: [], preferences: { quiet_start: '21:00', quiet_end: '07:00', retention_days: 90, redaction_level: 'standard' } })
    vi.spyOn(localApi, 'reportDeliveryChannels').mockResolvedValue([])
    vi.spyOn(localApi, 'reportDeliveries').mockResolvedValue([])
    vi.spyOn(localApi, 'reportDeliveryStatus').mockResolvedValue({ scheduled: 0, processing: 0, failed: 0, unknown: 0, recovered: 0 })
    vi.spyOn(localApi, 'retractedPeriodEvidence').mockResolvedValue([{ evidence_ref: 'record-cycle', subject: '数学', knowledge_title: '角度', summary: '待核对作答', date: '2026-09-07', note: '需要核对' }])
    const retract = vi.spyOn(localApi, 'retractPeriodEvidence').mockResolvedValue({ updated_reports: 2, failures: [] })
    const restore = vi.spyOn(localApi, 'restorePeriodEvidence').mockResolvedValue({ updated_reports: 2, failures: [] })
    return { list, retract, restore }
  }

  it('shows reliable original observations separately and withdraws the exact record after lost response', async () => {
    const { list, retract } = arrange()
    const weekly: ParentReportSnapshot = { ...structuredClone(snapshot), report_type: 'weekly',
      facts: { learning_summary: { evidence: [], qualified_observations: [{ evidence_ref: 'record-original',
        subject: '数学', knowledge_title: '原稿推理', date: '2026-09-10',
        summary: '已校准自动评分：本次尚未达到量表要求。不等于稳定掌握。',
        passed: false, mastery_claim_allowed: false }],
        coverage: { eligible_evidence_count: 0, knowledge_nodes_covered: 0, qualified_observation_count: 1 } } } }
    list.mockResolvedValue([weekly])
    retract.mockRejectedValueOnce(new Error('响应丢失'))
    const first = render(<ParentReportDeliveryPanel childId="child-1" />)
    fireEvent.click(await screen.findByText('查看学习证据明细'))
    expect(screen.getByText('可靠的原作答核对（不等同掌握）')).toBeInTheDocument()
    expect(screen.getByText(/原稿推理：已校准自动评分：本次尚未达到量表要求/)).toBeInTheDocument()
    expect(screen.queryByText('record-original')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '这条记录需撤回核对' }))
    await screen.findByText('响应丢失')
    const original = retract.mock.calls[0]
    expect(original[1]).toBe('record-original')
    first.unmount()
    render(<ParentReportDeliveryPanel childId="child-1" />)
    await screen.findByRole('region', { name: '待确认的证据操作' })
    await waitFor(() => expect(screen.getByRole('button', { name: '继续核对上次操作' })).toBeEnabled())
    fireEvent.click(screen.getByRole('button', { name: '继续核对上次操作' }))
    await waitFor(() => expect(retract).toHaveBeenCalledTimes(2))
    expect(retract.mock.calls[1]).toEqual(original)
  })

  it('preserves the exact uncertain restore across remount, isolates children, and does not auto execute', async () => {
    const { restore } = arrange()
    restore.mockRejectedValueOnce(new Error('响应中断'))
    const first = render(<ParentReportDeliveryPanel childId="child-1" />)
    fireEvent.click(await screen.findByText('查看学习证据明细'))
    fireEvent.change(screen.getByLabelText('恢复说明'), { target: { value: '已核实原始答卷，恢复这条记录。' } })
    fireEvent.click(screen.getByRole('button', { name: '恢复这条记录' }))
    await screen.findByText('响应中断')
    const original = restore.mock.calls[0]
    fireEvent.change(screen.getByLabelText('恢复说明'), { target: { value: '随后编辑的内容不能偷换原重试。' } })
    first.unmount()
    const other = render(<ParentReportDeliveryPanel childId="child-2" />)
    await screen.findByRole('button', { name: '刷新报告状态' })
    expect(screen.queryByRole('region', { name: '待确认的证据操作' })).not.toBeInTheDocument()
    expect(restore).toHaveBeenCalledTimes(1)
    other.unmount()
    render(<ParentReportDeliveryPanel childId="child-1" />)
    const pending = await screen.findByRole('region', { name: '待确认的证据操作' })
    expect(within(pending).getByText(original[2])).toBeInTheDocument()
    await waitFor(() => expect(screen.getByRole('button', { name: '继续核对上次操作' })).toBeEnabled())
    expect(restore).toHaveBeenCalledTimes(1)
    fireEvent.click(screen.getByRole('button', { name: '继续核对上次操作' }))
    await waitFor(() => expect(restore).toHaveBeenCalledTimes(2))
    expect(restore.mock.calls[1]).toEqual(original)
    await waitFor(() => expect(screen.queryByRole('region', { name: '待确认的证据操作' })).not.toBeInTheDocument())
    expect(window.localStorage.getItem('hsc:period-evidence-pending:child-1')).toBeNull()
  })

  it('finishes the mutation before a failed refresh and distinguishes partial report rebuild', async () => {
    const { list, retract } = arrange()
    retract.mockResolvedValueOnce({ updated_reports: 1, failures: [{ report_type: 'monthly', error_code: 'REBUILD_FAILED' }] })
    render(<ParentReportDeliveryPanel childId="child-1" />)
    fireEvent.click(await screen.findByText('查看学习证据明细'))
    list.mockRejectedValueOnce(new Error('刷新断开'))
    fireEvent.click(screen.getByRole('button', { name: '这条记录需撤回核对' }))
    expect(await screen.findByText(/部分报告尚未完成更正/)).toBeInTheDocument()
    expect(await screen.findByText(/报告状态未能更新/)).toBeInTheDocument()
    expect(screen.queryByRole('region', { name: '待确认的证据操作' })).not.toBeInTheDocument()
    expect(window.localStorage.getItem('hsc:period-evidence-pending:child-1')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: '刷新报告状态' }))
    await waitFor(() => expect(screen.getByRole('button', { name: '这条记录需撤回核对' })).toBeEnabled())
    expect(retract).toHaveBeenCalledTimes(1)
  })

  it('clears only the deleted child journal without writing to the server', async () => {
    const { restore } = arrange()
    const action = { kind: 'restore', evidence_ref: 'record-cycle', note: '原始说明', client_action_id: 'pending-one' }
    for (const child of ['child-1', 'child-2']) window.localStorage.setItem(`hsc:period-evidence-pending:${child}`, JSON.stringify(action))
    clearReportPeriodEvidenceLocalState('child-1')
    render(<ParentReportDeliveryPanel childId="child-1" />)
    await screen.findByText('查看学习证据明细')
    expect(screen.queryByRole('region', { name: '待确认的证据操作' })).not.toBeInTheDocument()
    expect(window.localStorage.getItem('hsc:period-evidence-pending:child-2')).not.toBeNull()
    expect(restore).not.toHaveBeenCalled()
    window.localStorage.setItem('unrelated-setting', 'keep')
    clearReportPeriodEvidenceLocalState()
    expect(window.localStorage.getItem('hsc:period-evidence-pending:child-2')).toBeNull()
    expect(window.localStorage.getItem('unrelated-setting')).toBe('keep')
  })

  it.each(['approve', 'revoke'] as const)('keeps a saved %s distinct from failed reads and retries only queries', async (operation) => {
    const { list } = arrange()
    const initial: ParentReportSnapshot = { ...structuredClone(snapshot), status: operation === 'approve' ? 'draft' : 'approved' }
    const saved: ParentReportSnapshot = { ...initial, status: operation === 'approve' ? 'approved' : 'revoked' }
    list.mockResolvedValue([initial])
    const write = operation === 'approve'
      ? vi.spyOn(localApi, 'approveReportSnapshot').mockResolvedValue(saved)
      : vi.spyOn(localApi, 'revokeReportSnapshot').mockResolvedValue(saved)
    const changed = vi.fn<() => Promise<void>>().mockRejectedValueOnce(new Error('父层读取断开')).mockResolvedValue(undefined)
    const view = render(<ParentReportDeliveryPanel childId="child-1" onReviewChanged={changed} />)
    const button = await screen.findByRole('button', { name: operation === 'approve' ? '确认并批准' : '撤销此报告并停止待发送任务' })
    expect(changed).not.toHaveBeenCalled()
    list.mockRejectedValueOnce(new Error('报告刷新断开'))
    fireEvent.click(button)
    expect(await screen.findByText(/审核已保存，但关联报告未能全部刷新/)).toBeInTheDocument()
    expect(write).toHaveBeenCalledTimes(1)
    expect(changed).toHaveBeenCalledTimes(1)
    expect(screen.queryByRole('button', { name: operation === 'approve' ? '确认并批准' : '撤销此报告并停止待发送任务' })).not.toBeInTheDocument()
    list.mockResolvedValue([saved])
    fireEvent.click(screen.getByRole('button', { name: '重新读取审核结果' }))
    await waitFor(() => expect(screen.queryByText(/审核已保存，但关联报告未能全部刷新/)).not.toBeInTheDocument())
    expect(write).toHaveBeenCalledTimes(1)
    expect(changed).toHaveBeenCalledTimes(2)
    const newCallback = vi.fn<() => Promise<void>>().mockResolvedValue(undefined)
    view.rerender(<ParentReportDeliveryPanel childId="child-1" onReviewChanged={newCallback} />)
    expect(newCallback).not.toHaveBeenCalled()
  })

  it('shows the frozen approved edit when an earlier lost approval won over later unsaved wording', async () => {
    const { list } = arrange()
    list.mockResolvedValue([snapshot])
    const saved: ParentReportSnapshot = { ...structuredClone(snapshot), status: 'approved',
      parent_edit: { ...snapshot.parent_edit, coaching_decision: 'custom', coaching_action: '已冻结的第一次行动', coaching_duration_minutes: 2 } }
    vi.spyOn(localApi, 'approveReportSnapshot').mockResolvedValue(saved)
    render(<ParentReportDeliveryPanel childId="child-1" />)
    fireEvent.click(await screen.findByRole('button', { name: '修改后采用' }))
    fireEvent.change(screen.getByLabelText('修改今天行动'), { target: { value: '响应中断后另改的未保存行动' } })
    list.mockResolvedValue([saved])
    fireEvent.click(screen.getByRole('button', { name: '确认并批准' }))
    await screen.findByText('批准已保存。')
    await waitFor(() => expect(screen.getByLabelText('修改今天行动')).toBeDisabled())
    expect(screen.getByLabelText('修改今天行动')).toHaveValue('已冻结的第一次行动')
    expect(screen.getByText('已冻结的第一次行动（约2分钟）')).toBeInTheDocument()
  })

  it('mounts the real period action feedback against the scoped API and shows persisted feedback after remount', async () => {
    const { list } = arrange()
    const action: ParentActionSnapshot = { binding_status: 'bound', snapshot_id: 'period-action', child_id: 'child-1',
      report_id: null, period_report_id: 'period-1', source_kind: 'period_report', source_id: 'period-1', version: 1, fingerprint: 'frozen',
      coaching: snapshot.content.parent_coaching, timing: { report_date: '2026-09-07', when: null, duration_minutes: 2, planned_at: null },
      feedback: null, execution_allowed: true }
    const weekly: ParentReportSnapshot = { ...structuredClone(snapshot), report_type: 'weekly', status: 'approved', parent_action: action }
    list.mockResolvedValue([weekly])
    const receipt = { revision: 1, operation: 'record', executed: false, action_snapshot_id: action.snapshot_id,
      report_id: null, period_report_id: 'period-1', effectiveness: null, stress_rating: null } as ParentReportFeedback
    const save = vi.spyOn(localApi, 'saveParentActionFeedback').mockResolvedValue(receipt)
    const legacy = vi.spyOn(localApi, 'saveParentReportFeedback')
    const view = render(<ParentReportDeliveryPanel childId="child-1" />)
    const feedback = await screen.findByRole('region', { name: '行动版本反馈' })
    fireEvent.change(within(feedback).getByLabelText('执行情况'), { target: { value: 'no' } })
    fireEvent.click(within(feedback).getByRole('button', { name: '保存行动反馈' }))
    await waitFor(() => expect(save).toHaveBeenCalledWith('child-1', 'period-action', expect.objectContaining({ executed: false, effectiveness: null, stress_rating: null })))
    expect(legacy).not.toHaveBeenCalled()
    view.unmount()
    list.mockResolvedValue([{ ...weekly, parent_action: { ...action, feedback: receipt } }])
    render(<ParentReportDeliveryPanel childId="child-1" />)
    expect(await screen.findByText(/已保存反馈版本：1.*未执行/)).toBeInTheDocument()
  })

  it('delegates period skip to the real non-executable action panel and hides unbound execution controls', async () => {
    const { list } = arrange()
    const action: ParentActionSnapshot = { binding_status: 'bound', snapshot_id: 'period-skipped', child_id: 'child-1',
      report_id: null, period_report_id: 'period-1', version: 1, fingerprint: 'skip', coaching: { ...snapshot.content.parent_coaching, action: null },
      timing: { report_date: '2026-09-07', when: null, duration_minutes: 0, planned_at: null }, feedback: null, execution_allowed: false }
    const weekly: ParentReportSnapshot = { ...structuredClone(snapshot), report_type: 'weekly', status: 'approved', parent_action: action }
    list.mockResolvedValue([weekly])
    render(<ParentReportDeliveryPanel childId="child-1" />)
    expect(await screen.findByRole('heading', { name: '本次已选择跳过' })).toBeInTheDocument()
    expect(screen.queryByLabelText('执行情况')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('实际效果')).not.toBeInTheDocument()
    list.mockResolvedValue([{ ...weekly, status: 'revoked', parent_action: { binding_status: 'unknown', reason: 'review_source_not_current' } }])
    fireEvent.click(screen.getByRole('button', { name: '刷新报告状态' }))
    expect(await screen.findByText(/本期行动的审核或来源已变化，当前不可执行/)).toBeInTheDocument()
    expect(screen.queryByRole('region', { name: '行动版本反馈' })).not.toBeInTheDocument()
  })

  it('shows excluded source gaps next to the actual period evidence counts', async () => {
    const { list } = arrange()
    const weekly = { ...structuredClone(snapshot), report_type: 'weekly' as const,
      facts: { coverage: { recorded_days: 1, recorded_sessions: 1, evidence_count: 3, excluded_daily_reports: 1 } },
      content: { ...snapshot.content, summary: { ...snapshot.content.summary, facts: [] } } }
    list.mockResolvedValue([weekly])
    render(<ParentReportDeliveryPanel childId="child-1" />)
    expect(await screen.findByText('另有1份日报来源暂不能核对，未计入本期汇总；原稿仍保留。')).toBeInTheDocument()
    expect(screen.getByText('本期已记录1天、1次学习会话，共3条证据。')).toBeInTheDocument()
  })

  it('focuses an exact off-page report and its unknown delivery only after rendering real delivery evidence', async () => {
    arrange()
    const historical={...structuredClone(snapshot),snapshot_id:'historical-exact',content:{...snapshot.content,headline:'指定页外报告'}}
    vi.spyOn(apiModule,'requestParentLocal').mockResolvedValue(historical)
    let resolve!: (value:unknown)=>void
    vi.mocked(localApi.reportDeliveries).mockImplementation(async id => id==='historical-exact'
      ? await new Promise<unknown>(r=>{resolve=r}) as never : [])
    const focused=vi.fn()
    render(<ParentReportDeliveryPanel childId="child-1" focusRequest={{objectId:'delivery-exact',relatedId:'historical-exact',nonce:'focus-1'}} onFocusResolved={focused}/>)
    await screen.findByRole('heading',{name:'指定页外报告'})
    expect(focused).not.toHaveBeenCalled()
    await act(async()=>resolve([{delivery_id:'delivery-exact',snapshot_id:'historical-exact',child_id:'child-1',recipient_id:'recipient',channel:'email',status:'unknown',attempt_count:1}]))
    await waitFor(()=>expect(focused).toHaveBeenCalledWith('focus-1'))
    expect(document.activeElement).toHaveAttribute('data-work-object','delivery-exact')
  })

  it('reports deleted deep-link failure without certifying the first report as the target', async () => {
    arrange()
    vi.spyOn(apiModule,'requestParentLocal').mockRejectedValue(new ApiError('missing',404))
    const focused=vi.fn()
    render(<ParentReportDeliveryPanel childId="child-1" focusRequest={{objectId:'deleted',nonce:'focus-404'}} onFocusResolved={focused}/>)
    await waitFor(()=>expect(focused).toHaveBeenCalledWith('focus-404',expect.stringContaining('没有以首条报告替代')))
    expect(focused).toHaveBeenCalledTimes(1)
    expect(document.querySelector('[data-work-object="deleted"]')).toBeNull()
  })
})

describe('report correction journey', () => {
  afterEach(() => { cleanup(); vi.restoreAllMocks() })
  function arrange() {
    const old: ParentReportSnapshot = { ...structuredClone(snapshot), status: 'revoked', revoke_reason:'后续证据已更新；请审核新版草稿',
      correction_status: { snapshot_id:'snapshot-1', child_id:'child-1', status:'revoked', source_current:false,
        replacements:[{snapshot_id:'snapshot-1',replacement_snapshot_id:'new-version',event_id:'private-event',occurred_at:'2026-09-07T10:00:00Z'}],
        prior_submissions:[{delivery_id:'old-mom',recipient_id:'mom',status:'sent'},{delivery_id:'old-dad',recipient_id:'dad',status:'unknown'}],
        replacement_submissions:[],recipient_ids_requiring_correction:['mom','dad'],correction_notification_required:true,
        next_action:'review_replacement_and_choose_notification',automatic_delivery:false,human_delivery_verified:false,local_only:true,cloud_transmission_allowed:false } }
    const next: ParentReportSnapshot = { ...structuredClone(snapshot), snapshot_id:'new-version',
      content:{...snapshot.content,headline:'本期更正报告'} }
    const list = vi.spyOn(localApi,'reportSnapshots').mockResolvedValue([old,next])
    vi.spyOn(localApi,'reportRecipients').mockResolvedValue({items:['mom','dad'].map((id)=>({recipient_id:id,child_id:'child-1',channel:'email' as const,address:`${id}@local.test`,label:id==='mom'?'妈妈':'爸爸',consent_status:'consented' as const,consented_at:null})),preferences:{quiet_start:'21:00',quiet_end:'07:00',retention_days:90,redaction_level:'standard'}})
    vi.spyOn(localApi,'reportDeliveryChannels').mockResolvedValue([])
    const deliveries = vi.spyOn(localApi,'reportDeliveries').mockResolvedValue([])
    vi.spyOn(localApi,'reportDeliveryStatus').mockResolvedValue({scheduled:0,processing:0,failed:0,unknown:1,recovered:0})
    const send = vi.spyOn(localApi,'scheduleReportDelivery')
    const process = vi.spyOn(localApi,'processReportDeliveries')
    const reissue = vi.spyOn(localApi,'reissueUnknownReportDelivery')
    return {old,next,list,deliveries,send,process,reissue}
  }

  it('shows the specific replacement and each original recipient without suggesting another old send', async () => {
    const {list,old,next,send,reissue} = arrange()
    list.mockResolvedValue([old,{...next,snapshot_id:'unrelated',content:{...next.content,headline:'另一份草稿'}},next])
    render(<ParentReportDeliveryPanel childId="child-1" />)
    const correction = await screen.findByRole('region',{name:'报告更正核对'})
    expect(within(correction).getByRole('heading',{name:'旧报告需要更正'})).toBeInTheDocument()
    expect(within(correction).getByText('妈妈：旧报告已交给通道，不等于已读。')).toBeInTheDocument()
    expect(within(correction).getByText('爸爸：旧报告结果未知，可能已发送；不会自动重发。')).toBeInTheDocument()
    expect(within(correction).getByText('新版待审核')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button',{name:'刷新报告状态'}))
    await waitFor(()=>expect(screen.getByRole('button',{name:'刷新报告状态'})).toBeEnabled())
    expect(screen.getByRole('heading',{name:'完成 1 项计划',level:3})).toBeInTheDocument()
    fireEvent.click(within(correction).getByRole('button',{name:'查看更正报告'}))
    expect(await screen.findByRole('heading',{name:'本期更正报告',level:3})).toBeInTheDocument()
    expect(screen.queryByText('private-event')).not.toBeInTheDocument()
    expect(send).not.toHaveBeenCalled()
    expect(reissue).not.toHaveBeenCalled()
  })

  it('keeps same-recipient accepted and unknown corrections distinct across refresh and remount', async () => {
    const {old,next,list,send,process,reissue} = arrange()
    old.correction_status!.replacement_submissions = ['mom','dad'].map((id)=>({delivery_id:`new-${id}`,snapshot_id:'new-version',recipient_id:id,
      status:id==='mom'?'sent':'unknown',sent_at:null,provider_message_id:null,evidence_version:'private-hash',source_version:'private-source',source_current:true,
      channel_acceptance:id==='mom'?'accepted':'unknown',human_delivery_verified:false,human_read_verified:false}))
    old.correction_status!.recipient_ids_requiring_correction=['dad']
    list.mockResolvedValue([old,{...next,status:'approved'}])
    const view = render(<ParentReportDeliveryPanel childId="child-1" />)
    expect(await screen.findByText('妈妈：更正报告已交给通道，不等于已读。')).toBeInTheDocument()
    expect(screen.getByText('爸爸：更正报告结果未知，可能已发送；先核对收件情况。')).toBeInTheDocument()
    old.correction_status!.replacement_submissions[1].status='sent'
    old.correction_status!.replacement_submissions[1].channel_acceptance='accepted'
    old.correction_status!.correction_notification_required=false
    old.correction_status!.recipient_ids_requiring_correction=[]
    list.mockResolvedValue([structuredClone(old),next])
    fireEvent.click(screen.getByRole('button',{name:'刷新报告状态'}))
    expect(await screen.findByText('爸爸：更正报告已交给通道，不等于已读。')).toBeInTheDocument()
    view.unmount()
    render(<ParentReportDeliveryPanel childId="child-1" />)
    expect(await screen.findByRole('heading',{name:'更正报告已交给通道'})).toBeInTheDocument()
    expect(screen.queryByText('private-hash')).not.toBeInTheDocument()
    expect(send).not.toHaveBeenCalled(); expect(process).not.toHaveBeenCalled(); expect(reissue).not.toHaveBeenCalled()
  })

  it('preserves readable history on refresh failure and recovers without dispatching', async () => {
    const {old,list,process,send} = arrange()
    render(<ParentReportDeliveryPanel childId="child-1" />)
    await screen.findByRole('region',{name:'报告更正核对'})
    list.mockRejectedValueOnce(new Error('离线'))
    fireEvent.click(screen.getByRole('button',{name:'刷新报告状态'}))
    expect(await screen.findByText('报告状态未能更新；当前显示上次读取的记录，恢复连接后请重新核对。')).toBeInTheDocument()
    expect(screen.getByText('妈妈：旧报告已交给通道，不等于已读。')).toBeInTheDocument()
    old.correction_status!.replacements=[]
    list.mockResolvedValue([structuredClone(old)])
    fireEvent.click(screen.getByRole('button',{name:'刷新报告状态'}))
    expect(await screen.findByText('新版尚未就绪；旧报告不会自动重新发送。')).toBeInTheDocument()
    expect(screen.queryByText('报告状态未能更新；当前显示上次读取的记录，恢复连接后请重新核对。')).not.toBeInTheDocument()
    expect(process).not.toHaveBeenCalled(); expect(send).not.toHaveBeenCalled()
  })

  it('discards an old child response after switching children', async () => {
    const {old,list} = arrange()
    let finish!: (value:ParentReportSnapshot[])=>void
    list.mockImplementationOnce(()=>new Promise((resolve)=>{finish=resolve})).mockResolvedValue([])
    const view = render(<ParentReportDeliveryPanel childId="child-1" />)
    view.rerender(<ParentReportDeliveryPanel childId="child-2" />)
    await waitFor(()=>expect(list).toHaveBeenCalledWith('child-2'))
    await act(async()=>finish([old]))
    expect(screen.queryByRole('region',{name:'报告更正核对'})).not.toBeInTheDocument()
    expect(screen.queryByRole('heading',{name:'完成 1 项计划',level:3})).not.toBeInTheDocument()
  })

  it('loads the linked replacement outside the list with the existing trusted request and recovers read failure', async () => {
    const {old,next,list,send,process} = arrange()
    list.mockResolvedValue([old])
    let attempted = false
    const detail = vi.spyOn(apiModule,'requestParentLocal').mockImplementation(async path => {
      if (path.endsWith('/report-source-issues')) return { child_id: 'child-1', items: [] } as never
      if (!attempted) { attempted = true; throw new Error('新版读取暂不可用') }
      return next as never
    })
    render(<ParentReportDeliveryPanel childId="child-1" />)
    fireEvent.click(await screen.findByRole('button',{name:'查看更正报告'}))
    expect(await screen.findByText('新版读取暂不可用')).toBeInTheDocument()
    await waitFor(()=>expect(screen.getByRole('button',{name:'查看更正报告'})).toBeEnabled())
    fireEvent.click(screen.getByRole('button',{name:'查看更正报告'}))
    expect(await screen.findByRole('heading',{name:'本期更正报告',level:3})).toBeInTheDocument()
    expect(detail).toHaveBeenCalledWith('/v1/parent/report-snapshots/new-version')
    expect(send).not.toHaveBeenCalled(); expect(process).not.toHaveBeenCalled()
  })

  it('rejects a linked detail belonging to a different child', async () => {
    const {old,next,list} = arrange()
    list.mockResolvedValue([old])
    vi.spyOn(apiModule,'requestParentLocal').mockResolvedValue({...next,child_id:'child-2',content:{...next.content,headline:'另一孩子的报告'}})
    render(<ParentReportDeliveryPanel childId="child-1" />)
    fireEvent.click(await screen.findByRole('button',{name:'查看更正报告'}))
    expect(await screen.findByText('更正报告不属于当前孩子，未打开。')).toBeInTheDocument()
    expect(screen.queryByRole('heading',{name:'另一孩子的报告'})).not.toBeInTheDocument()
  })

  it('does not display a late old-report receipt as the selected replacement receipt', async () => {
    const {old,next,list,deliveries} = arrange()
    list.mockResolvedValue([old,{...next,status:'approved'}])
    let finish!: (value:Awaited<ReturnType<typeof localApi.reportDeliveries>>)=>void
    deliveries.mockImplementation((id)=>id==='snapshot-1' ? new Promise((resolve)=>{finish=resolve}) : Promise.resolve([]))
    render(<ParentReportDeliveryPanel childId="child-1" />)
    fireEvent.click(await screen.findByRole('button',{name:/2026-08-27 · 本期更正报告/}))
    await waitFor(()=>expect(screen.getAllByRole('button',{name:'立即发送/排队'}).every((button)=>!button.hasAttribute('disabled'))).toBe(true))
    await act(async()=>finish([{delivery_id:'late-old-receipt',snapshot_id:'snapshot-1',recipient_id:'dad',channel:'email',status:'unknown',scheduled_at:'2026-09-07T10:00:00Z',attempt_count:1,last_error_code:null,last_error_message:null,sent_at:null,cancel_reason:null,attempts:[]}]))
    expect(screen.queryByRole('button',{name:'明确创建新的重发'})).not.toBeInTheDocument()
    expect(screen.getAllByRole('button',{name:'立即发送/排队'}).every((button)=>!button.hasAttribute('disabled'))).toBe(true)
  })

  it('blocks generic sending for accepted or unknown recipients and disables old reissue after a 409 refresh', async () => {
    const {old,list,deliveries,send,reissue} = arrange()
    const approved = {...old,status:'approved' as const,correction_status:{...old.correction_status!,source_current:true,status:'approved' as const,replacements:[],prior_submissions:[],correction_notification_required:false}}
    list.mockResolvedValue([approved])
    deliveries.mockResolvedValue(['mom','dad'].map((id)=>({delivery_id:`del-${id}`,snapshot_id:'snapshot-1',recipient_id:id,channel:'email',status:id==='mom'?'sent':'unknown',
      scheduled_at:'2026-09-07T10:00:00Z',attempt_count:1,last_error_code:null,last_error_message:null,sent_at:null,cancel_reason:null,attempts:[]})))
    reissue.mockRejectedValue(new ApiError('报告来源已变化，请先核对更正版本。',409))
    render(<ParentReportDeliveryPanel childId="child-1" />)
    const explicit = await screen.findByRole('button',{name:'明确创建新的重发'})
    await waitFor(()=>expect(explicit).toBeEnabled())
    expect(screen.getAllByRole('button',{name:'立即发送/排队'}).every((button)=>button.hasAttribute('disabled'))).toBe(true)
    fireEvent.click(explicit)
    fireEvent.click(screen.getByRole('button',{name:'确认再发一封'}))
    expect(await screen.findByText('报告来源已变化，请先核对更正版本。')).toBeInTheDocument()
    expect(screen.getByRole('button',{name:'明确创建新的重发'})).toBeDisabled()
    list.mockResolvedValue([old])
    fireEvent.click(screen.getByRole('button',{name:'刷新报告状态'}))
    expect(await screen.findByText('旧版来源已变化，请先审核更正报告，不能重发旧版。')).toBeInTheDocument()
    expect(await screen.findByRole('button',{name:'明确创建新的重发'})).toBeDisabled()
    expect(reissue).toHaveBeenCalledTimes(1)
    expect(send).not.toHaveBeenCalled()
  })

  it('uses the explicit latest state rather than falling back after deletion and shows blocked consent', async () => {
    const {old,list} = arrange()
    Object.assign(old.correction_status!,{latest_replacement_id:null,latest_replacement_availability:'deleted',latest_replacement_status:null,recipient_ids_blocked_by_consent:['mom']})
    list.mockResolvedValue([old])
    render(<ParentReportDeliveryPanel childId="child-1" />)
    expect(await screen.findByText('最新更正报告已删除，旧版本不能代替它。')).toBeInTheDocument()
    expect(screen.queryByRole('button',{name:'查看更正报告'})).not.toBeInTheDocument()
    expect(screen.getByText('妈妈：接收授权已撤销或不可用，不能发送更正报告。')).toBeInTheDocument()
  })

  it('keeps an out-of-page historical selection fresh and explicitly handles its deletion', async () => {
    const {old,next,list,send} = arrange()
    list.mockResolvedValue([old])
    const detail = vi.spyOn(apiModule,'requestParentLocal').mockResolvedValue(next)
    render(<ParentReportDeliveryPanel childId="child-1" />)
    fireEvent.click(await screen.findByRole('button',{name:'查看更正报告'}))
    await screen.findByRole('heading',{name:'本期更正报告',level:3})
    await waitFor(()=>expect(screen.getByRole('button',{name:'刷新报告状态'})).toBeEnabled())
    detail.mockResolvedValue({...next,content:{...next.content,headline:'历史更正报告已重新核对'}})
    fireEvent.click(screen.getByRole('button',{name:'刷新报告状态'}))
    expect(await screen.findByRole('heading',{name:'历史更正报告已重新核对',level:3})).toBeInTheDocument()
    expect(detail.mock.calls.filter(([path]) => path.startsWith('/v1/parent/report-snapshots/'))).toHaveLength(2)
    detail.mockRejectedValue(new ApiError('找不到报告快照。',404))
    fireEvent.click(screen.getByRole('button',{name:'刷新报告状态'}))
    expect(await screen.findByText('已选历史报告已删除或不可访问，已返回当前报告列表。')).toBeInTheDocument()
    expect(screen.queryByRole('heading',{name:'历史更正报告已重新核对',level:3})).not.toBeInTheDocument()
    expect(send).not.toHaveBeenCalled()
  })

  it('isolates an unreadable selected snapshot while showing its persisted issue and valid reports', async () => {
    const { old, next, list, send } = arrange()
    list.mockResolvedValue([old])
    let corrupted = false
    vi.spyOn(apiModule, 'requestParentLocal').mockImplementation(async path => {
      if (path.endsWith('/report-source-issues')) return { child_id: 'child-1', items: corrupted ? [{
        issue_id: 'corrupt-copy', child_id: 'child-1', source_id: next.snapshot_id, status: 'open',
        report_type: 'daily', report_date: '2026-09-08', snapshot_id: null,
        message: '历史快照损坏，已停止批准和发送。', recovery_hint: '核对完整备份，不补造原始记录。',
      }] : [] } as never
      if (corrupted) throw new ApiError('快照无法读取。', 409)
      return next as never
    })
    render(<ParentReportDeliveryPanel childId="child-1" />)
    fireEvent.click(await screen.findByRole('button', { name: '查看更正报告' }))
    await screen.findByRole('heading', { name: '本期更正报告', level: 3 })
    corrupted = true
    fireEvent.click(screen.getByRole('button', { name: '刷新报告状态' }))
    expect(await screen.findByText('历史快照损坏，已停止批准和发送。')).toBeInTheDocument()
    expect(screen.getByText('已选历史快照暂不能读取，旧稿仍保留；请核对下方来源问题。')).toBeInTheDocument()
    expect(screen.queryByText(/报告状态未能更新/)).not.toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: '本期更正报告', level: 3 })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '刷新报告状态' })).toBeEnabled()
    expect(send).not.toHaveBeenCalled()
  })

  it('shows old recipient obligations when only the latest report fits the page', async () => {
    const {old,next,list} = arrange()
    next.correction_status={...old.correction_status!,snapshot_id:'new-version',status:'draft',source_current:true,
      latest_replacement_id:'new-version',latest_replacement_availability:'available',latest_replacement_status:'draft'}
    list.mockResolvedValue([next])
    render(<ParentReportDeliveryPanel childId="child-1" />)
    expect(await screen.findByText('妈妈：旧报告已交给通道，不等于已读。')).toBeInTheDocument()
    expect(screen.getAllByRole('region',{name:'报告更正核对'})).toHaveLength(1)
    expect(screen.getByText('当前查看的是更正版本。')).toBeInTheDocument()
    expect(screen.queryByRole('button',{name:'查看更正报告'})).not.toBeInTheDocument()
  })
})

describe('report consent checkbox labels', () => {
  afterEach(() => { cleanup(); vi.restoreAllMocks() })

  it.each([
    ['确认收件人授权', '我确认该成年人/群内所有成员已同意接收此孩子的报告。', false],
  ] as const)('keeps %s native and toggles through its adjacent label text', async (name, text, checked) => {
    vi.spyOn(localApi, 'reportSnapshots').mockResolvedValue([snapshot])
    vi.spyOn(localApi, 'reportRecipients').mockResolvedValue({ items: [], preferences: { quiet_start: '21:00', quiet_end: '07:00', retention_days: 90, redaction_level: 'standard' } })
    vi.spyOn(localApi, 'reportDeliveryChannels').mockResolvedValue([])
    vi.spyOn(localApi, 'reportDeliveries').mockResolvedValue([])
    vi.spyOn(localApi, 'reportDeliveryStatus').mockResolvedValue({ scheduled: 0, processing: 0, failed: 0, unknown: 0, recovered: 0 })
    vi.spyOn(localApi, 'reportDeliveryChannelTests').mockResolvedValue([])
    const send = vi.spyOn(localApi, 'testReportDeliveryChannel')
    const save = vi.spyOn(localApi, 'saveReportDeliveryChannel')
    render(<ParentReportDeliveryPanel childId="child-1" />)
    await screen.findByRole('checkbox', { name: '确认收件人授权' })
    fireEvent.click(screen.getByText('送达设置（本机受保护配置）'))
    const input = screen.getByRole('checkbox', { name }) as HTMLInputElement
    const caption = screen.getByText(text)
    expect(caption.tagName).toBe('SPAN')
    expect(caption.parentElement).toBe(input.closest('label.recipient-consent'))
    expect(input.type).toBe('checkbox')
    expect(input.tabIndex).toBe(0)
    expect(input.checked).toBe(checked)
    fireEvent.click(caption)
    expect(input.checked).toBe(!checked)
    input.focus()
    expect(input).toHaveFocus()
    fireEvent.click(caption)
    expect(input.checked).toBe(checked)
    expect(send).not.toHaveBeenCalled()
    expect(save).not.toHaveBeenCalled()
  })
})

describe('ParentReportDeliveryPanel', () => {
  afterEach(() => { cleanup(); window.localStorage.clear(); window.desktop = undefined; vi.useRealTimers(); vi.restoreAllMocks() })

  const status = () => vi.spyOn(localApi, 'reportDeliveryStatus').mockResolvedValue({ scheduled: 7, processing: 2, failed: 3, unknown: 0, recovered: 1 })

  it.each(['legacy', 'qualified'] as const)('puts %s evidence and uncertainty before parent approval, without exposing internal IDs', async (kind) => {
    const current = structuredClone(snapshot)
    if (kind === 'qualified') {
      current.content.evidence_detail!.knowledge_evidence![0] = {
        ...current.content.evidence_detail!.knowledge_evidence![0],
        score_status: 'verified_local', correctness: 1, mastery_probability: null,
      }
    }
    vi.spyOn(localApi, 'reportSnapshots').mockResolvedValue([current])
    vi.spyOn(localApi, 'reportRecipients').mockResolvedValue({ items: [], preferences: { quiet_start: '21:00', quiet_end: '07:00', retention_days: 90, redaction_level: 'standard' } })
    vi.spyOn(localApi, 'reportDeliveryChannels').mockResolvedValue([{ channel: 'email', configured: false, credential_updated_at: null, requires_encryption_key: false }, { channel: 'wechat_work_bot', configured: false, credential_updated_at: null, requires_encryption_key: false }])
    vi.spyOn(localApi, 'reportDeliveries').mockResolvedValue([])
    vi.spyOn(localApi, 'approveReportSnapshot').mockResolvedValue({ ...snapshot, status: 'approved', approved_at: '2026-08-27T10:01:00Z' })
    vi.spyOn(localApi, 'requestReportSnapshotCorrection').mockResolvedValue({ ...snapshot, status: 'revoked', corrections: [{ note: '任务完成记录需要核对。', created_at: '2026-08-27T10:01:00Z' }] })

    render(<ParentReportDeliveryPanel childId="child-1" />)

    expect(await screen.findByText('依据的事实')).toBeInTheDocument()
    expect(screen.getByText('计划20分钟，已记录完成18分钟')).toBeInTheDocument()
    expect(screen.getByText('未记录的信息不作推断。')).toBeInTheDocument()
    expect(screen.getByText((_, node) => node?.tagName === 'P' && node.textContent?.includes('证据不足：这份报告只反映已确认的少量记录') === true)).toBeInTheDocument()
    expect(screen.getByText('基于事实的解读')).toBeInTheDocument()
    expect(screen.getByText('依据的事实').compareDocumentPosition(screen.getByText('只根据确认记录。')) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(screen.getByText('今晚做什么')).toBeInTheDocument()
    expect(screen.getByText('订正第一步（约8分钟）')).toBeInTheDocument()
    expect(screen.getByText('先说出已经确定的一步。')).toBeInTheDocument()
    expect(screen.getByText('怎么又错了？')).toBeInTheDocument()
    expect(screen.getByText((_, node) => node?.tagName === 'LI' && node.textContent === '怎么又错了？可以改说：只看第一处。')).toBeInTheDocument()
    fireEvent.click(screen.getByText('查看学习证据明细'))
    expect(screen.getByText(kind === 'qualified'
      ? '数学 · 判别式：已核验独立作答 · 正确率100%'
      : '数学 · 判别式：历史评分，未重新核验 · 正确率50% · 历史掌握估计42%')).toBeInTheDocument()
    expect(screen.queryByText(/当前掌握估计/)).not.toBeInTheDocument()
    expect(screen.getByText('数学 · 判别式练习：待继续处理；证据置信度90%')).toBeInTheDocument()
    expect(screen.queryByText('snapshot-1')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '确认授权并添加' })).toBeDisabled()
    fireEvent.change(screen.getByLabelText('修正说明'), { target: { value: '任务完成记录需要核对。' } })
    fireEvent.click(screen.getByRole('button', { name: '标记待修正并停止外发' }))
    await waitFor(() => expect(localApi.requestReportSnapshotCorrection).toHaveBeenCalledWith('snapshot-1', '任务完成记录需要核对。'))
    const approve = screen.getByRole('button', { name: '确认并批准' })
    await waitFor(() => expect(approve).toBeEnabled())
    fireEvent.click(approve)
    await waitFor(() => expect(localApi.approveReportSnapshot).toHaveBeenCalledWith('snapshot-1', expect.not.objectContaining({ coaching_duration_minutes: expect.anything() })))
  })

  it('renders weekly coverage and daily signals as evidence before delivery approval', async () => {
    const weekly: ParentReportSnapshot = {
      ...snapshot, snapshot_id: 'snapshot-week-1', source_kind: 'period_report', source_id: 'period-week-1', report_id: null, report_type: 'weekly',
      facts: { coverage: { recorded_days: 3, recorded_sessions: 4, evidence_count: 9 }, daily_signals: [{ date: '2026-08-28', headline: '完成订正' }] },
      content: { ...snapshot.content, headline: '3天有学习记录', report_date: '2026-08-30', summary: { headline: '3天有学习记录' } },
    }
    vi.spyOn(localApi, 'reportSnapshots').mockResolvedValue([weekly])
    vi.spyOn(localApi, 'reportRecipients').mockResolvedValue({ items: [], preferences: { quiet_start: '21:00', quiet_end: '07:00', retention_days: 90, redaction_level: 'standard' } })
    vi.spyOn(localApi, 'reportDeliveryChannels').mockResolvedValue([])
    vi.spyOn(localApi, 'reportDeliveries').mockResolvedValue([])

    render(<ParentReportDeliveryPanel childId="child-1" />)

    expect(await screen.findByText('本期已记录3天、4次学习会话，共9条证据。')).toBeInTheDocument()
    expect(screen.getByText('2026-08-28：完成订正')).toBeInTheDocument()
  })

  it('drills into period mastery evidence without internal IDs and refreshes after a parent withdrawal', async () => {
    const weekly: ParentReportSnapshot = {
      ...snapshot, snapshot_id: 'snapshot-week-mastery', source_kind: 'period_report', source_id: 'period-week@safe-version', report_id: null, report_type: 'weekly',
      facts: { coverage: { recorded_days: 3, recorded_sessions: 4, evidence_count: 3 }, learning_summary: {
        coverage: { eligible_evidence_count: 2, knowledge_nodes_covered: 1 }, uncertainty: '单次正确、单次错题或一次独立变式通过，都不足以单独宣称已经掌握；未作答不作负面推断。',
        knowledge_support: [{ subject: '数学', knowledge_title: '根的判别式', evidence_count: 2, support: '本期有多条可追溯记录支持，仍需后续变式复核。' }],
        new_weaknesses: [], pending_confirmation: [],
        independent_variant_retests: [{ subject: '数学', title: '判别式变式', date: '2026-08-28', summary: '独立变式通过', evidence_ref: 'record-safe-ref' }],
        evidence: [{ subject: '数学', knowledge_title: '根的判别式', date: '2026-08-28', summary: '孩子完成独立作答', independent: true, correctness: 1, evidence_ref: 'record-safe-ref' }],
      } },
      content: { ...snapshot.content, headline: '本期知识证据汇总', report_date: '2026-08-30', summary: { headline: '本期知识证据汇总' }, parent_coaching: { ...snapshot.content.parent_coaching, action: null, script: ['这份周报不安排今晚额外任务。'] } },
    }
    vi.spyOn(localApi, 'reportSnapshots').mockResolvedValue([weekly])
    vi.spyOn(localApi, 'reportRecipients').mockResolvedValue({ items: [], preferences: { quiet_start: '21:00', quiet_end: '07:00', retention_days: 90, redaction_level: 'standard' } })
    vi.spyOn(localApi, 'reportDeliveryChannels').mockResolvedValue([])
    vi.spyOn(localApi, 'reportDeliveries').mockResolvedValue([])
    vi.spyOn(localApi, 'reportDeliveryStatus').mockResolvedValue({ scheduled: 0, processing: 0, failed: 0, unknown: 0, recovered: 0 })
    vi.spyOn(localApi, 'retractedPeriodEvidence').mockResolvedValue([{ evidence_ref: 'record-retracted', subject: '数学', knowledge_title: '根的判别式', summary: '需要再次核对的作答', date: '2026-08-27', note: '误记，待核对' }])
    vi.spyOn(localApi, 'retractPeriodEvidence').mockResolvedValue({ updated_reports: 2, failures: [] })
    vi.spyOn(localApi, 'restorePeriodEvidence').mockResolvedValue({ updated_reports: 2, failures: [] })
    vi.spyOn(crypto, 'randomUUID')
      .mockReturnValueOnce('00000000-0000-4000-8000-000000000010')
      .mockReturnValueOnce('00000000-0000-4000-8000-000000000011')
      .mockReturnValueOnce('00000000-0000-4000-8000-000000000012')
    render(<ParentReportDeliveryPanel childId="child-1" />)
    expect(await screen.findByText('其中2条为可追溯、未撤回的知识证据，覆盖1个知识点。')).toBeInTheDocument()
    fireEvent.click(screen.getByText('查看学习证据明细'))
    expect(screen.getByText('本期知识证据支持程度')).toBeInTheDocument()
    expect(screen.getByText(/本次独立变式通过（不等同稳定掌握）/)).toBeInTheDocument()
    expect(screen.queryByText(/未记录正误，不作负面推断/)).not.toBeInTheDocument()
    expect(screen.queryByText('record-safe-ref')).not.toBeInTheDocument()
    fireEvent.click(screen.getAllByRole('button', { name: '这条记录需撤回核对' })[0])
    await waitFor(() => expect(localApi.retractPeriodEvidence).toHaveBeenCalledWith('child-1', 'record-safe-ref', '家长请求撤回本期汇总中的这条记录，待核对。', '00000000-0000-4000-8000-000000000010'))
    expect(screen.getByText(/证据操作已记录；请以刷新后的当前记录为准/)).toBeInTheDocument()
    expect(screen.getByText('本期已撤回记录')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '恢复这条记录' }))
    await waitFor(() => expect(localApi.restorePeriodEvidence).toHaveBeenCalledWith('child-1', 'record-retracted', '家长核对后恢复这条学习记录。', '00000000-0000-4000-8000-000000000011'))
    await waitFor(() => expect(screen.getAllByRole('button', { name: '这条记录需撤回核对' })[0]).toBeEnabled())
    fireEvent.click(screen.getAllByRole('button', { name: '这条记录需撤回核对' })[0])
    await waitFor(() => expect(localApi.retractPeriodEvidence).toHaveBeenCalledTimes(2))
    expect(vi.mocked(localApi.retractPeriodEvidence).mock.calls[1][3]).toBe('00000000-0000-4000-8000-000000000012')
  })

  it('explains restart recovery and SMTP credential failure without exposing error codes', async () => {
    vi.spyOn(localApi, 'reportSnapshots').mockResolvedValue([{ ...snapshot, status: 'approved' }])
    vi.spyOn(localApi, 'reportRecipients').mockResolvedValue({ items: [], preferences: { quiet_start: '21:00', quiet_end: '07:00', retention_days: 90, redaction_level: 'standard' } })
    vi.spyOn(localApi, 'reportDeliveryChannels').mockResolvedValue([{ channel: 'email', configured: false, credential_updated_at: null, requires_encryption_key: false }])
    vi.spyOn(localApi, 'reportDeliveries').mockResolvedValue([
      { delivery_id: 'delivery-recovered', snapshot_id: 'snapshot-1', recipient_id: 'recipient-1', channel: 'email', status: 'unknown', scheduled_at: '2026-08-28T10:00:00Z', attempt_count: 1, last_error_code: 'UNKNOWN_AFTER_RESTART', last_error_message: null, sent_at: null, cancel_reason: null, attempts: [] },
      { delivery_id: 'delivery-failed', snapshot_id: 'snapshot-1', recipient_id: 'recipient-1', channel: 'email', status: 'failed', scheduled_at: '2026-08-28T10:00:00Z', attempt_count: 1, last_error_code: 'SMTP_CREDENTIALS_INVALID', last_error_message: null, sent_at: null, cancel_reason: null, attempts: [] },
    ])
    render(<ParentReportDeliveryPanel childId="child-1" />)
    expect((await screen.findAllByText((_, node) => node?.textContent?.includes('应用或本地服务重启时无法确认通道是否已接受') === true)).length).toBeGreaterThan(0)
    expect(screen.getAllByText((_, node) => node?.textContent?.includes('邮箱授权信息无效；更新应用专用密码后可重试。') === true).length).toBeGreaterThan(0)
    expect(screen.queryByText('SMTP_CREDENTIALS_INVALID')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '修复后重试' })).toBeInTheDocument()
  })

  it.each([
    ['recipient_snapshot_scope_mismatch', '收件人与报告的孩子或发送渠道不一致；请核对接收人设置后重新安排发送。'],
    ['delivery_envelope_changed', '收件信息在发送前发生变化；请核对当前接收人后重新安排发送。'],
    ['channel_configuration_changed', '发送设置在提交前发生变化；请核对通道设置后重新安排发送。'],
    ['report_disclosure_changed', '报告分享范围已改变；请按当前分享范围重新安排发送。'],
    ['snapshot_source_changed', '学习记录已变化，旧报告未发送；请重新生成并审核报告。'],
  ])('explains cancellation %s without offering blind retry', async (reason, explanation) => {
    vi.spyOn(localApi, 'reportSnapshots').mockResolvedValue([{ ...snapshot, status: 'approved' }])
    vi.spyOn(localApi, 'reportRecipients').mockResolvedValue({ items: [], preferences: { quiet_start: '21:00', quiet_end: '07:00', retention_days: 90, redaction_level: 'standard' } })
    vi.spyOn(localApi, 'reportDeliveryChannels').mockResolvedValue([])
    vi.spyOn(localApi, 'reportDeliveries').mockResolvedValue([
      { delivery_id: 'cancelled-before-submit', snapshot_id: 'snapshot-1', recipient_id: 'recipient-1',
        channel: 'email', status: 'cancelled', scheduled_at: '2026-08-28T10:00:00Z', attempt_count: 1,
        last_error_code: null, last_error_message: null, sent_at: null, cancel_reason: reason, attempts: [] },
    ])
    render(<ParentReportDeliveryPanel childId="child-1" />)
    expect(await screen.findByText(explanation, { exact: false })).toBeInTheDocument()
    expect(screen.queryByText(reason)).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '修复后重试' })).not.toBeInTheDocument()
  })

  it('keeps the report visible when aggregate status fails, then restores authoritative counts on retry', async () => {
    vi.spyOn(localApi, 'reportSnapshots').mockResolvedValue([snapshot])
    vi.spyOn(localApi, 'reportRecipients').mockResolvedValue({ items: [], preferences: { quiet_start: '21:00', quiet_end: '07:00', retention_days: 90, redaction_level: 'standard' } })
    vi.spyOn(localApi, 'reportDeliveryChannels').mockResolvedValue([])
    vi.spyOn(localApi, 'reportDeliveries').mockResolvedValue([])
    const aggregate = vi.spyOn(localApi, 'reportDeliveryStatus').mockRejectedValueOnce(new Error('temporary'))
    render(<ParentReportDeliveryPanel childId="child-1" />)
    expect(await screen.findByText('完成 1 项计划')).toBeInTheDocument()
    expect(screen.getByText(/送达汇总状态暂不可用/)).toBeInTheDocument()
    expect(screen.queryByText(/待发送：9份/)).not.toBeInTheDocument()
    aggregate.mockResolvedValue({ scheduled: 7, processing: 2, failed: 3, unknown: 0, recovered: 1 })
    fireEvent.click(screen.getByRole('button', { name: '重试状态查询' }))
    await waitFor(() => expect(screen.queryByText(/送达汇总状态暂不可用/)).not.toBeInTheDocument())
    expect(screen.getByText(/待发送：9份/)).toBeInTheDocument()
    expect(screen.getByText(/发送需处理：3份/)).toBeInTheDocument()
  })

  it('uses authoritative aggregate counts and hides numbers when aggregate recovery fails', async () => {
    vi.spyOn(localApi, 'reportSnapshots').mockResolvedValue([snapshot]); vi.spyOn(localApi, 'reportRecipients').mockResolvedValue({ items: [], preferences: { quiet_start: '21:00', quiet_end: '07:00', retention_days: 90, redaction_level: 'standard' } }); vi.spyOn(localApi, 'reportDeliveryChannels').mockResolvedValue([]); vi.spyOn(localApi, 'reportDeliveries').mockResolvedValue([]); status()
    render(<ParentReportDeliveryPanel childId="child-1" />)
    expect(await screen.findByText(/待发送：9份/)).toBeInTheDocument(); expect(screen.getByText(/发送需处理：3份/)).toBeInTheDocument()
  })

  it('shows persisted SMTP test failure and repair without credentials, while WeCom remains truthfully blocked', async () => {
    vi.spyOn(localApi, 'reportSnapshots').mockResolvedValue([snapshot])
    vi.spyOn(localApi, 'reportRecipients').mockResolvedValue({ items: [{ recipient_id: 'mom', child_id: 'child-1', channel: 'email', address: 'mom@example.test', label: '妈妈', consent_status: 'consented', consented_at: '2026-01-01' }], preferences: { quiet_start: '21:00', quiet_end: '07:00', retention_days: 90, redaction_level: 'standard' } })
    vi.spyOn(localApi, 'reportDeliveryChannels').mockResolvedValue([{ channel: 'email', configured: true, credential_updated_at: '2026-08-28T10:00:00Z', requires_encryption_key: false }, { channel: 'wechat_work_bot', configured: false, credential_updated_at: null, requires_encryption_key: false }])
    vi.spyOn(localApi, 'reportDeliveries').mockResolvedValue([])
    vi.spyOn(localApi, 'reportDeliveryChannelTests').mockResolvedValue([
      { channel: 'email', status: 'failed', message: '邮箱授权信息无效；更新应用专用密码后可再次测试。', tested_at: '2026-08-28T10:01:00Z' },
      { channel: 'wechat_work_bot', status: 'blocked', message: '个人微信没有稳定公开 API，系统不会伪造或自动化发送。', tested_at: '2026-08-28T10:01:00Z' },
    ])
    const saveChannel = vi.spyOn(localApi, 'saveReportDeliveryChannel').mockResolvedValue()
    const repair = vi.spyOn(localApi, 'testReportDeliveryChannel').mockRejectedValueOnce(new Error('timeout')).mockResolvedValue({ channel: 'email', status: 'passed', recipient_id: 'mom', message: '本机 SMTP sink已接受发给已授权“妈妈”的不含孩子数据的测试邮件；这不代表外部邮箱已阅读或最终送达。', tested_at: '2026-08-28T10:02:00Z' })
    status()
    render(<ParentReportDeliveryPanel childId="child-1" />)
    await screen.findByText('完成 1 项计划')
    fireEvent.click(screen.getByText('送达设置（本机受保护配置）'))
    expect(await screen.findByText(/邮件发送测试：未通过。邮箱授权信息无效/)).toBeInTheDocument()
    expect(screen.getByText(/仅支持企业微信群机器人，不是个人微信/)).toBeInTheDocument()
    expect(screen.getByText(/个人微信没有稳定公开 API/)).toBeInTheDocument()
    expect(screen.queryByText('SMTP_CREDENTIALS_INVALID')).not.toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('SMTP主机'), { target: { value: '127.0.0.1' } })
    fireEvent.change(screen.getByLabelText('SMTP发件地址'), { target: { value: 'report@local.test' } })
    fireEvent.change(screen.getByLabelText('SMTP加密方式'), { target: { value: 'none' } })
    fireEvent.click(screen.getByRole('button', { name: '保存配置并允许邮件发送' }))
    await waitFor(() => expect(saveChannel).toHaveBeenCalledWith('email', expect.objectContaining({ host: '127.0.0.1', from_address: 'report@local.test', starttls: false })))
    await waitFor(() => expect(screen.getByRole('button', { name: '发送不含孩子数据的测试邮件' })).toBeEnabled())
    fireEvent.click(screen.getByRole('button', { name: '发送不含孩子数据的测试邮件' }))
    await waitFor(() => expect(repair).toHaveBeenCalledTimes(1))
    const firstActionId = repair.mock.calls[0][2]
    cleanup()
    render(<ParentReportDeliveryPanel childId="child-1" />)
    await screen.findByText('完成 1 项计划')
    fireEvent.click(screen.getByText('送达设置（本机受保护配置）'))
    await waitFor(() => expect(screen.getByRole('button', { name: '发送不含孩子数据的测试邮件' })).toBeEnabled())
    fireEvent.click(screen.getByRole('button', { name: '发送不含孩子数据的测试邮件' }))
    await waitFor(() => expect(repair).toHaveBeenCalledTimes(2))
    expect(repair.mock.calls[1][2]).toBe(firstActionId)
    expect(screen.getByText(/邮件发送测试：已通过。本机 SMTP sink已接受/)).toBeInTheDocument()
  })

  it('does not allow plaintext SMTP outside a local sink', async () => {
    vi.spyOn(localApi, 'reportSnapshots').mockResolvedValue([snapshot])
    vi.spyOn(localApi, 'reportRecipients').mockResolvedValue({ items: [], preferences: { quiet_start: '21:00', quiet_end: '07:00', retention_days: 90, redaction_level: 'standard' } })
    vi.spyOn(localApi, 'reportDeliveryChannels').mockResolvedValue([])
    vi.spyOn(localApi, 'reportDeliveries').mockResolvedValue([])
    status()
    render(<ParentReportDeliveryPanel childId="child-1" />)
    await screen.findByText('完成 1 项计划')
    fireEvent.click(screen.getByText('送达设置（本机受保护配置）'))
    fireEvent.change(screen.getByLabelText('SMTP主机'), { target: { value: 'smtp.example.test' } })
    fireEvent.change(screen.getByLabelText('SMTP发件地址'), { target: { value: 'report@example.test' } })
    fireEvent.change(screen.getByLabelText('SMTP加密方式'), { target: { value: 'none' } })
    expect(screen.getByText(/外部 SMTP 必须使用 SSL\/TLS 或 STARTTLS/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '保存配置并允许邮件发送' })).toBeDisabled()
  })

  it('hands an external SMTP password to the desktop secure-store bridge without reading it back', async () => {
    const saveSecrets = vi.fn().mockResolvedValue({ smtpPasswordConfigured: true, wecomWebhookConfigured: false })
    const clearSecrets = vi.fn().mockResolvedValue({ smtpPasswordConfigured: false, wecomWebhookConfigured: false })
    window.desktop = { saveReportDeliverySecrets: saveSecrets, clearReportDeliverySecrets: clearSecrets } as unknown as NonNullable<typeof window.desktop>
    vi.spyOn(localApi, 'reportSnapshots').mockResolvedValue([snapshot])
    vi.spyOn(localApi, 'reportRecipients').mockResolvedValue({ items: [], preferences: { quiet_start: '21:00', quiet_end: '07:00', retention_days: 90, redaction_level: 'standard' } })
    vi.spyOn(localApi, 'reportDeliveryChannels').mockResolvedValueOnce([]).mockResolvedValue([
      { channel: 'email', configured: true, credential_updated_at: '2026-08-28T10:00:00Z', requires_encryption_key: false },
      { channel: 'wechat_work_bot', configured: true, credential_updated_at: '2026-08-28T10:00:00Z', requires_encryption_key: false },
    ])
    vi.spyOn(localApi, 'reportDeliveries').mockResolvedValue([])
    vi.spyOn(localApi, 'reportDeliveryChannelTests').mockResolvedValue([])
    const saveConfig = vi.spyOn(localApi, 'saveReportDeliveryChannel').mockResolvedValue()
    const clearConfig = vi.spyOn(localApi, 'clearReportDeliveryChannel').mockResolvedValue({ runtimeRestarted: false })
    status()
    render(<ParentReportDeliveryPanel childId="child-1" />)
    await screen.findByText('完成 1 项计划')
    fireEvent.click(screen.getByText('送达设置（本机受保护配置）'))
    fireEvent.change(screen.getByLabelText('SMTP主机'), { target: { value: 'smtp.example.test' } })
    fireEvent.change(screen.getByLabelText('SMTP发件地址'), { target: { value: 'parent@example.test' } })
    fireEvent.change(screen.getByLabelText('SMTP密码'), { target: { value: 'synthetic-password' } })
    fireEvent.change(screen.getByLabelText('SMTP加密方式'), { target: { value: 'ssl' } })
    fireEvent.click(screen.getByRole('button', { name: '保存配置并允许邮件发送' }))
    await waitFor(() => expect(saveSecrets).toHaveBeenCalledWith({ smtpPassword: 'synthetic-password' }))
    expect(saveConfig).toHaveBeenCalledWith('email', expect.objectContaining({ host: 'smtp.example.test', port: 465, password: 'synthetic-password', use_ssl: true, starttls: false }))
    expect(screen.getByLabelText('SMTP密码')).toHaveValue('')
    await waitFor(() => expect(screen.getByRole('button', { name: '删除邮件配置与密码' })).toBeEnabled())
    fireEvent.click(screen.getByRole('button', { name: '删除邮件配置与密码' }))
    await waitFor(() => expect(clearConfig).toHaveBeenCalledWith('email'))
    expect(clearSecrets).not.toHaveBeenCalled()
    const clearWecom = screen.getByRole('button', { name: '删除企业微信配置与 Webhook' })
    await waitFor(() => expect(clearWecom).toBeEnabled())
    fireEvent.click(clearWecom)
    await waitFor(() => expect(clearConfig).toHaveBeenCalledWith('wechat_work_bot'))
    expect(clearSecrets).not.toHaveBeenCalled()
    await waitFor(() => expect(clearWecom).toBeEnabled())
  })

  it('warns that configured channels cannot send when desktop secure storage is unavailable', async () => {
    window.desktop = { getReportDeliverySecretStatus: vi.fn().mockResolvedValue({ state: 'unavailable', smtpPasswordConfigured: false, wecomWebhookConfigured: false }) } as unknown as NonNullable<typeof window.desktop>
    vi.spyOn(localApi, 'reportSnapshots').mockResolvedValue([snapshot])
    vi.spyOn(localApi, 'reportRecipients').mockResolvedValue({ items: [], preferences: { quiet_start: '21:00', quiet_end: '07:00', retention_days: 90, redaction_level: 'standard' } })
    vi.spyOn(localApi, 'reportDeliveryChannels').mockResolvedValue([{ channel: 'email', configured: true, credential_updated_at: null, requires_encryption_key: false }])
    vi.spyOn(localApi, 'reportDeliveries').mockResolvedValue([])
    status()
    render(<ParentReportDeliveryPanel childId="child-1" />)
    expect(await screen.findByText(/系统安全存储不可用；不会尝试外发/)).toBeInTheDocument()
  })

  it('recovers an unknown SMTP test without automatic resend and makes a new test explicit', async () => {
    vi.spyOn(localApi, 'reportSnapshots').mockResolvedValue([snapshot])
    vi.spyOn(localApi, 'reportRecipients').mockResolvedValue({ items: [{ recipient_id: 'mom', child_id: 'child-1', channel: 'email', address: 'mom@example.test', label: '妈妈', consent_status: 'consented', consented_at: '2026-01-01' }], preferences: { quiet_start: '21:00', quiet_end: '07:00', retention_days: 90, redaction_level: 'standard' } })
    vi.spyOn(localApi, 'reportDeliveryChannels').mockResolvedValue([{ channel: 'email', configured: true, credential_updated_at: null, requires_encryption_key: false }])
    vi.spyOn(localApi, 'reportDeliveries').mockResolvedValue([])
    vi.spyOn(localApi, 'reportDeliveryChannelTests').mockResolvedValue([{ channel: 'email', status: 'unknown', message: '上次测试结果未知，可能已发出。', tested_at: '2026-08-28T10:00:00Z', client_action_id: 'unknown-action-001', recipient_id: 'mom' }])
    const ordinary = vi.spyOn(localApi, 'testReportDeliveryChannel').mockResolvedValue({ channel: 'email', status: 'unknown', message: '上次测试结果未知，可能已发出。', tested_at: '2026-08-28T10:00:00Z', client_action_id: 'unknown-action-001', recipient_id: 'mom' })
    const keep = vi.spyOn(localApi, 'keepUnknownReportDeliveryChannelTest').mockResolvedValue({ channel: 'email', status: 'unknown', message: '家长选择保持不重发。', tested_at: '2026-08-28T10:01:00Z', client_action_id: 'unknown-action-001', recipient_id: 'mom', kept_without_resend: true })
    const reissue = vi.spyOn(localApi, 'reissueReportDeliveryChannelTest').mockResolvedValue({ channel: 'email', status: 'passed', message: '本机 SMTP sink已接受固定测试邮件。', tested_at: '2026-08-28T10:02:00Z' })
    status()
    render(<ParentReportDeliveryPanel childId="child-1" />)
    await screen.findByText('完成 1 项计划')
    fireEvent.click(screen.getByText('送达设置（本机受保护配置）'))
    expect((await screen.findAllByText(/上次测试结果未知，可能已发出/)).length).toBeGreaterThan(0)
    fireEvent.click(screen.getByRole('button', { name: '普通重试（不会再次发送）' }))
    await waitFor(() => expect(ordinary).toHaveBeenCalledWith('child-1', 'email', 'unknown-action-001', 'mom'))
    const keepWithoutResend = screen.getByRole('button', { name: '保持不重发' })
    await waitFor(() => expect(keepWithoutResend).toBeEnabled())
    fireEvent.click(keepWithoutResend)
    await waitFor(() => expect(keep).toHaveBeenCalledWith('child-1', 'email', 'unknown-action-001'))
    await waitFor(() => expect(screen.getByRole('button', { name: '明确发起新的测试' })).toBeEnabled())
    fireEvent.click(screen.getByRole('button', { name: '明确发起新的测试' }))
    await waitFor(() => expect(reissue).toHaveBeenCalledTimes(1))
    expect(reissue.mock.calls[0][0]).toBe('child-1')
    expect(reissue.mock.calls[0][1]).toBe('email')
    expect(reissue.mock.calls[0][2]).toBe('unknown-action-001')
    expect(reissue.mock.calls[0][3]).not.toBe('unknown-action-001')
    expect(reissue.mock.calls[0][4]).toBe('mom')
    expect(screen.getByText(/可能会收到第二封固定的非隐私测试邮件/)).toBeInTheDocument()
  })

  it('shows the current reauthorized address and does not send until the parent confirms', async () => {
    vi.spyOn(localApi, 'reportSnapshots').mockResolvedValue([snapshot])
    vi.spyOn(localApi, 'reportRecipients').mockResolvedValue({items:[{recipient_id:'mom',child_id:'child-1',channel:'email',address:'mom@example.test',label:'妈妈',consent_status:'consented',consented_at:'2026-01-01'}],preferences:{quiet_start:'21:00',quiet_end:'07:00',retention_days:90,redaction_level:'standard'}})
    vi.spyOn(localApi,'reportDeliveryChannels').mockResolvedValue([{channel:'email',configured:true,credential_updated_at:null,requires_encryption_key:false}])
    vi.spyOn(localApi,'reportDeliveries').mockResolvedValue([])
    vi.spyOn(localApi,'reportDeliveryChannelTests').mockResolvedValue([{channel:'email',status:'unknown',message:'结果未知',tested_at:null,client_action_id:'unknown-action-001',recipient_id:'mom'}])
    const send=vi.spyOn(localApi,'reissueReauthorizedEmailTest').mockResolvedValue({channel:'email',status:'passed',message:'已接受',tested_at:null,recipient_id:'mom'})
    status(); render(<ParentReportDeliveryPanel childId="child-1" />)
    fireEvent.click(await screen.findByText('送达设置（本机受保护配置）'))
    fireEvent.click(await screen.findByRole('button',{name:'核对重新授权的收件邮箱'}))
    expect(screen.getByText(/当前重新授权的 mom@example.test/)).toHaveTextContent('邮件可能已送达')
    expect(send).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button',{name:'取消当前邮箱重测'})); expect(send).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button',{name:'核对重新授权的收件邮箱'}))
    fireEvent.click(screen.getByRole('button',{name:'确认当前邮箱并发送新测试'}))
    await waitFor(()=>expect(send).toHaveBeenCalledOnce())
    expect(send).toHaveBeenCalledWith('child-1','unknown-action-001',expect.any(String),'mom','mom@example.test')
  })

  it('makes a successful SMTP retest explicit and assigns a fresh action id', async () => {
    vi.spyOn(localApi, 'reportSnapshots').mockResolvedValue([snapshot])
    vi.spyOn(localApi, 'reportRecipients').mockResolvedValue({ items: [{ recipient_id: 'mom', child_id: 'child-1', channel: 'email', address: 'mom@example.test', label: '妈妈', consent_status: 'consented', consented_at: '2026-01-01' }], preferences: { quiet_start: '21:00', quiet_end: '07:00', retention_days: 90, redaction_level: 'standard' } })
    vi.spyOn(localApi, 'reportDeliveryChannels').mockResolvedValue([{ channel: 'email', configured: true, credential_updated_at: '2026-08-28T10:00:00Z', requires_encryption_key: false }])
    vi.spyOn(localApi, 'reportDeliveries').mockResolvedValue([])
    vi.spyOn(localApi, 'reportDeliveryChannelTests').mockResolvedValue([{ channel: 'email', status: 'passed', verified_recipient_ids: ['mom'], message: '本机 SMTP sink 已接受固定测试邮件。', tested_at: '2026-08-28T10:02:00Z' }])
    const retest = vi.spyOn(localApi, 'testReportDeliveryChannel').mockResolvedValue({ channel: 'email', status: 'passed', recipient_id: 'mom', message: '本机 SMTP sink 已接受新的固定测试邮件。', tested_at: '2026-08-28T10:03:00Z' })
    vi.spyOn(crypto, 'randomUUID').mockReturnValue('00000000-0000-4000-8000-000000000777')
    window.localStorage.setItem('hsc:report-delivery-test:child-1:email', 'completed-action-001')
    status()

    render(<ParentReportDeliveryPanel childId="child-1" />)
    await screen.findByText('完成 1 项计划')
    fireEvent.click(screen.getByText('送达设置（本机受保护配置）'))
    const button = await screen.findByRole('button', { name: '重新发送一封测试邮件' })
    fireEvent.click(button)

    await waitFor(() => expect(retest).toHaveBeenCalledWith('child-1', 'email', '00000000-0000-4000-8000-000000000777', 'mom'))
    expect(retest.mock.calls[0][2]).not.toBe('completed-action-001')
    expect(window.localStorage.getItem('hsc:report-delivery-test:child-1:email:mom')).toBe('00000000-0000-4000-8000-000000000777')
  })

  it('keeps a recovered processing test visible and disables a duplicate send', async () => {
    vi.spyOn(localApi, 'reportSnapshots').mockResolvedValue([snapshot])
    vi.spyOn(localApi, 'reportRecipients').mockResolvedValue({ items: [], preferences: { quiet_start: '21:00', quiet_end: '07:00', retention_days: 90, redaction_level: 'standard' } })
    vi.spyOn(localApi, 'reportDeliveryChannels').mockResolvedValue([{ channel: 'email', configured: true, credential_updated_at: null, requires_encryption_key: false }])
    vi.spyOn(localApi, 'reportDeliveries').mockResolvedValue([])
    const channelTests = vi.spyOn(localApi, 'reportDeliveryChannelTests').mockResolvedValue([{ channel: 'email', status: 'processing', message: '测试正在处理中；为避免重复投递，暂时不能再次发送。', tested_at: null, client_action_id: 'processing-action-001' }])
    status()
    render(<ParentReportDeliveryPanel childId="child-1" />)
    await screen.findByText('完成 1 项计划')
    fireEvent.click(screen.getByText('送达设置（本机受保护配置）'))
    expect(await screen.findByText(/邮件发送测试：处理中/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '测试处理中（请稍候）' })).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: '刷新测试状态' }))
    await waitFor(() => expect(channelTests).toHaveBeenCalledTimes(2))
  })

  it('keeps an unknown report delivery visible after refresh and requires an explicit new action to resend', async () => {
    vi.spyOn(localApi, 'reportSnapshots').mockResolvedValue([{ ...snapshot, status: 'approved' }])
    vi.spyOn(localApi, 'reportRecipients').mockResolvedValue({ items: [], preferences: { quiet_start: '21:00', quiet_end: '07:00', retention_days: 90, redaction_level: 'standard' } })
    vi.spyOn(localApi, 'reportDeliveryChannels').mockResolvedValue([])
    const deliveries = vi.spyOn(localApi, 'reportDeliveries').mockResolvedValue([{ delivery_id: 'unknown-delivery-1', snapshot_id: 'snapshot-1', recipient_id: 'recipient-1', channel: 'email', status: 'unknown', scheduled_at: '2026-08-28T10:00:00Z', attempt_count: 1, last_error_code: 'UNKNOWN_AFTER_RESTART', last_error_message: '结果未知', sent_at: null, cancel_reason: null, attempts: [] }])
    vi.spyOn(localApi, 'reportDeliveryStatus').mockResolvedValue({ scheduled: 0, processing: 0, failed: 0, unknown: 1, recovered: 0 })
    const acknowledged: ReportDelivery = { delivery_id: 'unknown-delivery-1', snapshot_id: 'snapshot-1', recipient_id: 'recipient-1', channel: 'email', status: 'unknown_acknowledged', scheduled_at: '2026-08-28T10:00:00Z', attempt_count: 1, last_error_code: 'UNKNOWN_AFTER_RESTART', last_error_message: null, sent_at: null, cancel_reason: 'parent_kept_unknown_without_resend', attempts: [] }
    const keep = vi.spyOn(localApi, 'keepUnknownReportDelivery').mockImplementation(async () => {
      deliveries.mockResolvedValue([acknowledged])
      return acknowledged
    })
    const reissue = vi.spyOn(localApi, 'reissueUnknownReportDelivery').mockResolvedValue({ delivery_id: 'replacement-1', snapshot_id: 'snapshot-1', recipient_id: 'recipient-1', channel: 'email', status: 'scheduled', scheduled_at: '2026-08-28T10:01:00Z', attempt_count: 0, last_error_code: null, last_error_message: null, sent_at: null, cancel_reason: null, attempts: [] })
    vi.spyOn(crypto, 'randomUUID').mockReturnValue('00000000-0000-4000-8000-000000000099')
    render(<ParentReportDeliveryPanel childId="child-1" />)
    expect(await screen.findByText(/结果未知，可能已发送/)).toBeInTheDocument()
    expect(screen.getByText(/结果未知：1份/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '保持不重发' }))
    await waitFor(() => expect(keep).toHaveBeenCalledWith('unknown-delivery-1'))
    // Observe the refreshed record, not a briefly enabled button from the old
    // delivery list before reload schedules its dependent read.
    await screen.findByText(/你之后仍可改变决定并明确创建新的重发/)
    await waitFor(() => expect(screen.getByRole('button', { name: '明确创建新的重发' })).toBeEnabled())
    fireEvent.click(screen.getByRole('button', { name: '明确创建新的重发' }))
    expect(screen.getByText(/原邮件可能已经送达/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '取消，不再发送' }))
    expect(reissue).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: '明确创建新的重发' }))
    fireEvent.click(screen.getByRole('button', { name: '确认再发一封' }))
    await waitFor(() => expect(reissue).toHaveBeenCalledWith('unknown-delivery-1', '00000000-0000-4000-8000-000000000099'))
    deliveries.mockResolvedValue([{ ...acknowledged, status: 'unknown', cancel_reason: null }])
    cleanup()
    render(<ParentReportDeliveryPanel childId="child-1" />)
    await screen.findByText(/结果未知，可能已发送/)
    fireEvent.click(screen.getByRole('button', { name: '明确创建新的重发' }))
    fireEvent.click(screen.getByRole('button', { name: '确认再发一封' }))
    await waitFor(() => expect(reissue).toHaveBeenCalledTimes(2))
    expect(reissue.mock.calls[1][1]).toBe('00000000-0000-4000-8000-000000000099')
  })

  it('polls a processing test to completion, then stops polling without hiding the report', async () => {
    vi.useFakeTimers()
    vi.spyOn(localApi, 'reportSnapshots').mockResolvedValue([snapshot])
    vi.spyOn(localApi, 'reportRecipients').mockResolvedValue({ items: [{ recipient_id: 'mom', child_id: 'child-1', channel: 'email', address: 'mom@example.test', label: '妈妈', consent_status: 'consented', consented_at: '2026-01-01' }], preferences: { quiet_start: '21:00', quiet_end: '07:00', retention_days: 90, redaction_level: 'standard' } })
    vi.spyOn(localApi, 'reportDeliveryChannels').mockResolvedValue([{ channel: 'email', configured: true, credential_updated_at: null, requires_encryption_key: false }])
    vi.spyOn(localApi, 'reportDeliveries').mockResolvedValue([])
    const tests = vi.spyOn(localApi, 'reportDeliveryChannelTests')
      .mockResolvedValueOnce([{ channel: 'email', status: 'processing', message: '测试正在处理中；为避免重复投递，暂时不能再次发送。', tested_at: null, client_action_id: 'processing-action-001' }])
      .mockResolvedValueOnce([{ channel: 'email', status: 'passed', verified_recipient_ids: ['mom'], message: '本机 SMTP sink 已接受固定测试邮件。', tested_at: '2026-08-28T10:02:00Z' }])
    status()
    render(<ParentReportDeliveryPanel childId="child-1" />)
    await act(async () => { await Promise.resolve() })
    expect(screen.getByText('完成 1 项计划')).toBeInTheDocument()
    fireEvent.click(screen.getByText('送达设置（本机受保护配置）'))
    expect(screen.getByText(/邮件发送测试：处理中/)).toBeInTheDocument()
    await act(async () => { await vi.advanceTimersByTimeAsync(3000) })
    expect(screen.getByText(/邮件发送测试：已通过/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '重新发送一封测试邮件' })).toBeEnabled()
    const completedCalls = tests.mock.calls.length
    await act(async () => { await vi.advanceTimersByTimeAsync(15000) })
    expect(tests).toHaveBeenCalledTimes(completedCalls)
    expect(screen.getByText('完成 1 项计划')).toBeInTheDocument()
  })

  it('refreshes new period snapshots without discarding unsaved delivery or SMTP drafts', async () => {
    const weekly: ParentReportSnapshot = {
      ...snapshot, snapshot_id: 'snapshot-week-1', source_kind: 'period_report', source_id: 'period-week-1', report_id: null, report_type: 'weekly',
      content: { ...snapshot.content, headline: '本周学习汇总', report_date: '2026-08-30', summary: { headline: '本周学习汇总' } },
    }
    vi.spyOn(localApi, 'reportSnapshots').mockResolvedValueOnce([snapshot]).mockResolvedValueOnce([weekly, snapshot])
    vi.spyOn(localApi, 'reportRecipients').mockResolvedValue({ items: [], preferences: { quiet_start: '21:00', quiet_end: '07:00', retention_days: 90, redaction_level: 'standard' } })
    vi.spyOn(localApi, 'reportDeliveryChannels').mockResolvedValue([])
    vi.spyOn(localApi, 'reportDeliveries').mockResolvedValue([])
    status()
    const view = render(<ParentReportDeliveryPanel childId="child-1" refreshToken={0} />)
    expect(await screen.findByText('完成 1 项计划')).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('开场白'), { target: { value: '这段话还没有保存。' } })
    fireEvent.click(screen.getByText('送达设置（本机受保护配置）'))
    fireEvent.change(screen.getByLabelText('SMTP主机'), { target: { value: '127.0.0.1' } })
    view.rerender(<ParentReportDeliveryPanel childId="child-1" refreshToken={1} />)
    expect(await screen.findByRole('button', { name: /本周学习汇总/ })).toBeInTheDocument()
    expect(screen.getByLabelText('开场白')).toHaveValue('这段话还没有保存。')
    expect(screen.getByLabelText('SMTP主机')).toHaveValue('127.0.0.1')
  })

  it('shows child-specific reasons and saves feedback for the next suggestion without exposing strategy data', async () => {
    const personalized: ParentReportSnapshot = {
      ...snapshot,
      content: { ...snapshot.content, parent_coaching: {
        ...snapshot.content.parent_coaching,
        scenario: '孩子说 PET 听力细节卡住，今晚只处理一个小卡点。',
        why_this_fits: '先承认孩子指出的困难；过去反馈显示沟通压力偏高，本次缩短时长。',
        evidence_basis: ['孩子自述困难：PET听力细节', '学习阶段与目标：四年级阶段目标：PET周目标50分钟，已记录10分钟'],
        adaptation: { basis: 'parent_feedback', feedback_count: 2, average_effectiveness: 2.5, average_stress: 4.5, note: '过去反馈显示沟通压力偏高，本次缩短时长，并保留孩子提前停止的权利。' },
        personalization: { child_stage: '四年级', goal_context: '四年级阶段目标：PET周目标50分钟，已记录10分钟', recent_confirmed_facts: ['今日完成：PET听力10分钟'], feedback_considered: 2 },
      } },
    }
    vi.spyOn(localApi, 'reportSnapshots').mockResolvedValue([personalized])
    vi.spyOn(localApi, 'reportRecipients').mockResolvedValue({ items: [], preferences: { quiet_start: '21:00', quiet_end: '07:00', retention_days: 90, redaction_level: 'standard' } })
    vi.spyOn(localApi, 'reportDeliveryChannels').mockResolvedValue([])
    vi.spyOn(localApi, 'reportDeliveries').mockResolvedValue([])
    vi.spyOn(localApi, 'saveReportSnapshotFeedback').mockResolvedValue(personalized)
    vi.spyOn(crypto, 'randomUUID').mockReturnValue('00000000-0000-4000-8000-000000000001')
    status()
    render(<ParentReportDeliveryPanel childId="child-1" />)
    expect(await screen.findByText('为什么适合这次沟通')).toBeInTheDocument()
    expect(screen.getByText('孩子说 PET 听力细节卡住，今晚只处理一个小卡点。')).toBeInTheDocument()
    expect(screen.getByText(/本次考虑的年龄\/目标：四年级阶段目标：PET周目标50分钟/)).toBeInTheDocument()
    expect(screen.getByText(/已参考最近2次家长反馈：过去反馈显示沟通压力偏高/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '保存反馈' })).toBeDisabled()
    fireEvent.change(screen.getByLabelText('报告反馈备注'), { target: { value: '只留说明。' } })
    expect(screen.getByRole('button', { name: '保存反馈' })).toBeEnabled()
    fireEvent.change(screen.getByLabelText('建议帮助程度'), { target: { value: '4' } })
    fireEvent.change(screen.getByLabelText('沟通压力'), { target: { value: '2' } })
    fireEvent.change(screen.getByLabelText('报告反馈备注'), { target: { value: '先让孩子选。' } })
    fireEvent.click(screen.getByRole('button', { name: '保存反馈' }))
    await waitFor(() => expect(localApi.saveReportSnapshotFeedback).toHaveBeenCalledWith('snapshot-1', { helpfulness: 4, stress_rating: 2, notes: '先让孩子选。', client_action_id: '00000000-0000-4000-8000-000000000001' }))
    expect(screen.queryByText('feedback_considered')).not.toBeInTheDocument()
  })

  it('lets a parent review, customize, and persist an evidence-bound coaching draft', async () => {
    const phased: ParentReportSnapshot = {
      ...snapshot,
      content: { ...snapshot.content, parent_coaching: {
        ...snapshot.content.parent_coaching,
        evidence_basis: ['孩子对一次温和学习提醒选择“稍后”', '今日完成：PET听力10分钟'],
        uncertainty: '行为记录只说明一次已记录的提醒反馈，不用于判断情绪、态度、人格或因果。',
        script_phases: [
          { phase: '开场', text: '我只看已经确认的这件事。' }, { phase: '倾听', text: '你可以先说一件具体发生的事。' },
          { phase: '共情', text: '现在不想说也可以。' }, { phase: '共同选择', text: '你选下一小步或今天结束。' }, { phase: '收尾', text: '按约定停止。' },
        ],
      } },
    }
    vi.spyOn(localApi, 'reportSnapshots').mockResolvedValue([phased])
    vi.spyOn(localApi, 'reportRecipients').mockResolvedValue({ items: [], preferences: { quiet_start: '21:00', quiet_end: '07:00', retention_days: 90, redaction_level: 'standard' } })
    vi.spyOn(localApi, 'reportDeliveryChannels').mockResolvedValue([])
    vi.spyOn(localApi, 'reportDeliveries').mockResolvedValue([])
    vi.spyOn(localApi, 'editReportSnapshot').mockResolvedValue({ ...phased, parent_edit: { delivery_intro: '', parent_note: '', coaching_decision: 'custom', coaching_script: '我先听你说完。', coaching_action: '只圈出第一处。', coaching_duration_minutes: 8, coaching_observation: '若拒绝，明天不追加。' } })
    status()
    render(<ParentReportDeliveryPanel childId="child-1" />)
    expect(await screen.findByText('分阶段可以这样说')).toBeInTheDocument()
    expect(screen.getByText('孩子对一次温和学习提醒选择“稍后”')).toBeInTheDocument()
    expect(screen.getByText(/行为记录只说明一次已记录的提醒反馈/)).toBeInTheDocument()
    expect(screen.queryByText('snapshot-1')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '修改后采用' }))
    await waitFor(() => expect(screen.getByLabelText('修改行动时长')).toHaveValue(8))
    fireEvent.change(screen.getByLabelText('修改沟通话术'), { target: { value: '我先听你说完。' } })
    fireEvent.change(screen.getByLabelText('修改今天行动'), { target: { value: '只圈出第一处。' } })
    fireEvent.change(screen.getByLabelText('观察与明天调整'), { target: { value: '若拒绝，明天不追加。' } })
    fireEvent.change(screen.getByLabelText('修改行动时长'), { target: { value: '6' } })
    expect(screen.getByText('只圈出第一处。（约6分钟）')).toBeInTheDocument()
    expect(screen.getAllByText('我先听你说完。').length).toBeGreaterThan(1)
    fireEvent.click(screen.getByRole('button', { name: '保存采用方式' }))
    await waitFor(() => expect(localApi.editReportSnapshot).toHaveBeenCalledWith('snapshot-1', {
      delivery_intro: '', parent_note: '', coaching_decision: 'custom', coaching_script: '我先听你说完。', coaching_action: '只圈出第一处。', coaching_duration_minutes: 6, coaching_observation: '若拒绝，明天不追加。',
    }))
  })

  it('renders retention support as a bounded parent action without exposing internal evidence ids', async () => {
    const retained: ParentReportSnapshot = { ...snapshot, content: { ...snapshot.content,
      parent_coaching: { ...snapshot.content.parent_coaching, focus: 'verified_retention_support',
        scenario: '四则运算已经出现隔天保持的证据。', script: ['这次隔天还能独立完成，值得记下来。', '现在不想分享也可以。'],
        action: { what: '一起确认下次需要哪种帮助', duration_minutes: 5,
          steps: ['孩子选择先独立尝试，或需要时求助。'], stop_condition: '五分钟到时停止，不临时加题。',
          learning_target: { evidence_id: 'private-retention-evidence', knowledge_node_id: 'private-node',
            knowledge_title: '四则运算', question_identity: 'private-identity', mastery_claim_allowed: false,
            evidence_kind: 'qualified_mistake_retention', next_use: 'optional_independence_support',
            retention_basis_refs: ['private-baseline'] } },
        avoid: [{ do_not_say: '以后不许再错。', say_instead: '其他题型还可以慢慢验证。' }] } } }
    vi.spyOn(localApi, 'reportSnapshots').mockResolvedValue([retained])
    vi.spyOn(localApi, 'reportRecipients').mockResolvedValue({ items: [], preferences: { quiet_start: '21:00', quiet_end: '07:00', retention_days: 90, redaction_level: 'standard' } })
    vi.spyOn(localApi, 'reportDeliveryChannels').mockResolvedValue([])
    vi.spyOn(localApi, 'reportDeliveries').mockResolvedValue([])
    status()
    render(<ParentReportDeliveryPanel childId="child-1" />)
    expect(await screen.findByText('四则运算已经出现隔天保持的证据。')).toBeInTheDocument()
    expect(screen.getByText(/一起确认下次需要哪种帮助/)).toBeInTheDocument()
    expect(screen.getByText(/五分钟到时停止，不临时加题/)).toBeInTheDocument()
    expect(screen.getByText(/其他题型还可以慢慢验证/)).toBeInTheDocument()
    expect(screen.queryByText(/private-retention-evidence|private-baseline|private-node/)).not.toBeInTheDocument()
  })

  it('saves the currently selected skip before approving even when the save button was not used', async () => {
    vi.spyOn(localApi, 'reportSnapshots').mockResolvedValue([snapshot])
    vi.spyOn(localApi, 'reportRecipients').mockResolvedValue({ items: [], preferences: { quiet_start: '21:00', quiet_end: '07:00', retention_days: 90, redaction_level: 'standard' } })
    vi.spyOn(localApi, 'reportDeliveryChannels').mockResolvedValue([])
    vi.spyOn(localApi, 'reportDeliveries').mockResolvedValue([])
    const save = vi.spyOn(localApi, 'editReportSnapshot')
    const approve = vi.spyOn(localApi, 'approveReportSnapshot').mockImplementation(async (_, patch) => ({ ...snapshot,
      parent_edit: { ...snapshot.parent_edit, ...patch }, status: 'approved', approved_at: '2026-08-28T10:00:00Z' }))
    status()
    render(<ParentReportDeliveryPanel childId="child-1" />)
    await screen.findByText('完成 1 项计划')
    fireEvent.click(screen.getByRole('button', { name: '本次跳过' }))
    expect(await screen.findByText('家长本次选择不执行额外行动（约0分钟）')).toBeInTheDocument()
    expect(screen.getByText('这次先不追加对话或行动；明天根据孩子愿意确认的事实再调整。')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '确认并批准' }))
    await waitFor(() => expect(approve).toHaveBeenCalledWith('snapshot-1', expect.objectContaining({ coaching_decision: 'skip', coaching_duration_minutes: 0 })))
    expect(save).not.toHaveBeenCalled()
  })

  it('reuses the feedback action ID after a timed-out save retry', async () => {
    vi.spyOn(localApi, 'reportSnapshots').mockResolvedValue([snapshot])
    vi.spyOn(localApi, 'reportRecipients').mockResolvedValue({ items: [], preferences: { quiet_start: '21:00', quiet_end: '07:00', retention_days: 90, redaction_level: 'standard' } })
    vi.spyOn(localApi, 'reportDeliveryChannels').mockResolvedValue([])
    vi.spyOn(localApi, 'reportDeliveries').mockResolvedValue([])
    vi.spyOn(localApi, 'saveReportSnapshotFeedback').mockRejectedValueOnce(new Error('timeout')).mockResolvedValue(snapshot)
    vi.spyOn(crypto, 'randomUUID').mockReturnValueOnce('00000000-0000-4000-8000-000000000002').mockReturnValueOnce('00000000-0000-4000-8000-000000000003')
    status()
    render(<ParentReportDeliveryPanel childId="child-1" />)
    await screen.findByText('完成 1 项计划')
    fireEvent.change(screen.getByLabelText('建议帮助程度'), { target: { value: '3' } })
    fireEvent.click(screen.getByRole('button', { name: '保存反馈' }))
    await waitFor(() => expect(localApi.saveReportSnapshotFeedback).toHaveBeenCalledTimes(1))
    fireEvent.click(screen.getByRole('button', { name: '保存反馈' }))
    await waitFor(() => expect(localApi.saveReportSnapshotFeedback).toHaveBeenCalledTimes(2))
    expect(localApi.saveReportSnapshotFeedback).toHaveBeenNthCalledWith(1, 'snapshot-1', { helpfulness: 3, stress_rating: null, notes: null, client_action_id: '00000000-0000-4000-8000-000000000002' })
    expect(localApi.saveReportSnapshotFeedback).toHaveBeenNthCalledWith(2, 'snapshot-1', { helpfulness: 3, stress_rating: null, notes: null, client_action_id: '00000000-0000-4000-8000-000000000002' })
    fireEvent.change(screen.getByLabelText('沟通压力'), { target: { value: '2' } })
    fireEvent.click(screen.getByRole('button', { name: '保存反馈' }))
    await waitFor(() => expect(localApi.saveReportSnapshotFeedback).toHaveBeenCalledTimes(3))
    expect(localApi.saveReportSnapshotFeedback).toHaveBeenNthCalledWith(3, 'snapshot-1', { helpfulness: 3, stress_rating: 2, notes: null, client_action_id: '00000000-0000-4000-8000-000000000003' })
  })
})

describe('recipient-specific fixed email testing', () => {
  afterEach(() => { cleanup(); window.localStorage.clear(); vi.restoreAllMocks() })
  const items = ['mom', 'dad'].map(id => ({ recipient_id: id, child_id: 'child-1', channel: 'email' as const,
    address: `${id}@example.test`, label: id === 'mom' ? '妈妈' : '爸爸', consent_status: 'consented' as const, consented_at: '2026-01-01' }))
  function arrange(test: {channel:'email'; status:'unknown'|'failed'|'passed'; message:string; tested_at:string|null; client_action_id?:string; recipient_id?:string|null; verified_recipient_ids?:string[]}) {
    vi.spyOn(localApi, 'reportSnapshots').mockResolvedValue([snapshot])
    vi.spyOn(localApi, 'reportRecipients').mockResolvedValue({items, preferences:{quiet_start:'21:00',quiet_end:'07:00',retention_days:90,redaction_level:'standard'}})
    vi.spyOn(localApi, 'reportDeliveryChannels').mockResolvedValue([{channel:'email',configured:true,credential_updated_at:null,requires_encryption_key:false}])
    vi.spyOn(localApi, 'reportDeliveries').mockResolvedValue([])
    vi.spyOn(localApi, 'reportDeliveryStatus').mockResolvedValue({scheduled:0,processing:0,failed:0,unknown:0,recovered:0})
    vi.spyOn(localApi, 'reportDeliveryChannelTests').mockResolvedValue([test])
  }
  it('does not treat a replayed historical pass as current verification, even when refresh fails', async () => {
    arrange({channel:'email',status:'failed',message:'请测试',tested_at:null})
    const status = vi.mocked(localApi.reportDeliveryChannelTests)
    const result = vi.spyOn(localApi,'testReportDeliveryChannel').mockResolvedValue({channel:'email',status:'passed',recipient_id:'mom',message:'这是历史测试，不代表当前邮箱已验证。',tested_at:'2026-01-01',historical_only:true,current_verified:false} as never)
    vi.spyOn(crypto,'randomUUID').mockReturnValueOnce('00000000-0000-4000-8000-000000000101').mockReturnValueOnce('00000000-0000-4000-8000-000000000102')
    render(<ParentReportDeliveryPanel childId="child-1" />)
    await screen.findByText('完成 1 项计划')
    fireEvent.click(screen.getByText('送达设置（本机受保护配置）'))
    status.mockRejectedValueOnce(new Error('状态暂时无法读取'))
    fireEvent.click(screen.getByRole('button',{name:'发送不含孩子数据的测试邮件'}))
    await waitFor(() => expect(result).toHaveBeenCalledTimes(1))
    expect(await screen.findByText('状态暂时无法读取')).toBeInTheDocument()
    expect(screen.getByText('所选邮箱尚未通过当前配置的测试。')).toBeInTheDocument()
    expect(screen.queryByText('所选邮箱已通过当前配置的测试。')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button',{name:'发送不含孩子数据的测试邮件'}))
    await waitFor(() => expect(result).toHaveBeenCalledTimes(2))
    expect(result.mock.calls[1][2]).not.toBe(result.mock.calls[0][2])
  })
  it('refreshes actual status after a historical reissue receipt', async () => {
    arrange({channel:'email',status:'unknown',message:'结果未知',tested_at:null,client_action_id:'old',recipient_id:'mom'})
    const status = vi.mocked(localApi.reportDeliveryChannelTests)
    vi.spyOn(localApi,'reissueReportDeliveryChannelTest').mockResolvedValue({channel:'email',status:'blocked',recipient_id:'mom',message:'历史回执，不代表当前验证。',tested_at:'2026-01-01',historical_only:true,current_verified:false} as never)
    render(<ParentReportDeliveryPanel childId="child-1" />)
    await screen.findByText('完成 1 项计划')
    fireEvent.click(screen.getByText('送达设置（本机受保护配置）'))
    status.mockResolvedValueOnce([{channel:'email',status:'blocked',message:'当前配置尚未验证',tested_at:null}])
    fireEvent.click(screen.getByRole('button',{name:'明确发起新的测试'}))
    await waitFor(() => expect(status).toHaveBeenCalledTimes(2))
    expect(await screen.findByText('所选邮箱尚未通过当前配置的测试。')).toBeInTheDocument()
    expect(screen.queryByText('所选邮箱已通过当前配置的测试。')).not.toBeInTheDocument()
  })
  it('uses a different persisted action for each selected authorized address', async () => {
    arrange({channel:'email',status:'failed',message:'请测试',tested_at:null})
    const test = vi.spyOn(localApi, 'testReportDeliveryChannel').mockResolvedValue({channel:'email',status:'failed',message:'暂不可用',tested_at:null})
    vi.spyOn(crypto,'randomUUID').mockReturnValueOnce('00000000-0000-4000-8000-000000000111').mockReturnValueOnce('00000000-0000-4000-8000-000000000222')
    render(<ParentReportDeliveryPanel childId="child-1" />)
    await screen.findByText('完成 1 项计划')
    fireEvent.click(screen.getByText('送达设置（本机受保护配置）'))
    fireEvent.click(screen.getByRole('button',{name:'发送不含孩子数据的测试邮件'}))
    await waitFor(() => expect(test).toHaveBeenCalledWith('child-1','email','00000000-0000-4000-8000-000000000111','mom'))
    await waitFor(() => expect(screen.getByLabelText('测试收件邮箱')).toBeEnabled())
    fireEvent.change(screen.getByLabelText('测试收件邮箱'),{target:{value:'dad'}})
    fireEvent.click(screen.getByRole('button',{name:'发送不含孩子数据的测试邮件'}))
    await waitFor(() => expect(test).toHaveBeenCalledWith('child-1','email','00000000-0000-4000-8000-000000000222','dad'))
  })
  it('does not retry an unknown operation for a different or unidentified recipient', async () => {
    arrange({channel:'email',status:'unknown',message:'结果未知',tested_at:null,client_action_id:'old',recipient_id:'mom'})
    const reissue = vi.spyOn(localApi,'reissueReportDeliveryChannelTest')
    render(<ParentReportDeliveryPanel childId="child-1" />)
    await screen.findByText('完成 1 项计划')
    fireEvent.click(screen.getByText('送达设置（本机受保护配置）'))
    const choice = screen.getByLabelText('测试收件邮箱')
    fireEvent.change(choice,{target:{value:'dad'}})
    expect(screen.getByRole('button',{name:'明确发起新的测试'})).toBeDisabled()
    expect(screen.getByRole('button',{name:'普通重试（不会再次发送）'})).toBeDisabled()
    expect(reissue).not.toHaveBeenCalled()
    fireEvent.change(choice,{target:{value:'mom'}})
    expect(screen.getByRole('button',{name:'明确发起新的测试'})).toBeEnabled()
  })
  it('keeps a legacy unknown operation without an attributable recipient blocked', async () => {
    arrange({channel:'email',status:'unknown',message:'结果未知',tested_at:null,client_action_id:'legacy',recipient_id:null})
    render(<ParentReportDeliveryPanel childId="child-1" />)
    await screen.findByText('完成 1 项计划')
    fireEvent.click(screen.getByText('送达设置（本机受保护配置）'))
    expect(screen.getByRole('button',{name:'明确发起新的测试'})).toBeDisabled()
    expect(screen.getByRole('button',{name:'普通重试（不会再次发送）'})).toBeDisabled()
    expect(screen.getByText(/若原收件人无法确认/)).toBeInTheDocument()
  })
  it('reuses the exact replacement action if its response is lost', async () => {
    arrange({channel:'email',status:'unknown',message:'结果未知',tested_at:null,client_action_id:'old',recipient_id:'mom'})
    const reissue = vi.spyOn(localApi,'reissueReportDeliveryChannelTest')
      .mockRejectedValueOnce(new Error('response lost'))
      .mockResolvedValueOnce({channel:'email',status:'passed',message:'已接受',tested_at:'2026-01-01',recipient_id:'mom'})
    render(<ParentReportDeliveryPanel childId="child-1" />)
    await screen.findByText('完成 1 项计划')
    fireEvent.click(screen.getByText('送达设置（本机受保护配置）'))
    fireEvent.click(screen.getByRole('button',{name:'明确发起新的测试'}))
    await waitFor(() => expect(reissue).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(screen.getByRole('button',{name:'明确发起新的测试'})).toBeEnabled())
    fireEvent.click(screen.getByRole('button',{name:'明确发起新的测试'}))
    await waitFor(() => expect(reissue).toHaveBeenCalledTimes(2))
    expect(reissue.mock.calls[1][3]).toBe(reissue.mock.calls[0][3])
    expect(reissue.mock.calls[1][4]).toBe('mom')
  })
  it('blocks reissue when a keep receipt cannot confirm its former recipient', async () => {
    arrange({channel:'email',status:'unknown',message:'结果未知',tested_at:null,client_action_id:'old',recipient_id:'mom'})
    vi.spyOn(localApi,'keepUnknownReportDeliveryChannelTest').mockResolvedValue({channel:'email',status:'unknown',message:'保持不重发',tested_at:'2026-01-01',client_action_id:'old',recipient_id:null,kept_without_resend:true})
    render(<ParentReportDeliveryPanel childId="child-1" />)
    await screen.findByText('完成 1 项计划')
    fireEvent.click(screen.getByText('送达设置（本机受保护配置）'))
    fireEvent.click(screen.getByRole('button',{name:'保持不重发'}))
    await waitFor(() => expect(screen.getByRole('button',{name:'明确发起新的测试'})).toBeDisabled())
  })
  it('does not offer revoked addresses for a test', async () => {
    arrange({channel:'email',status:'failed',message:'请测试',tested_at:null})
    vi.mocked(localApi.reportRecipients).mockResolvedValue({items:[...items, {recipient_id:'revoked',child_id:'child-1',channel:'email',address:'revoked@example.test',label:'已撤销',consent_status:'revoked',consented_at:null}],preferences:{quiet_start:'21:00',quiet_end:'07:00',retention_days:90,redaction_level:'standard'}})
    render(<ParentReportDeliveryPanel childId="child-1" />)
    await screen.findByText('完成 1 项计划')
    fireEvent.click(screen.getByText('送达设置（本机受保护配置）'))
    expect(screen.queryByRole('option',{name:/revoked@example.test/})).not.toBeInTheDocument()
  })
  it('does not show another recipient’s verification as the selected address', async () => {
    arrange({channel:'email',status:'passed',message:'已核验妈妈',tested_at:'2026-01-01',verified_recipient_ids:['mom']})
    render(<ParentReportDeliveryPanel childId="child-1" />)
    await screen.findByText('完成 1 项计划')
    fireEvent.click(screen.getByText('送达设置（本机受保护配置）'))
    expect(screen.getByText('所选邮箱已通过当前配置的测试。')).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('测试收件邮箱'),{target:{value:'dad'}})
    expect(screen.getByText('所选邮箱尚未通过当前配置的测试。')).toBeInTheDocument()
    expect(screen.queryByText(/邮件发送测试：已通过。/)).not.toBeInTheDocument()
  })
})
