import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { localApi } from './api'
import { ReportDetail } from './ParentCenter'
import type { DailyReport, ParentActionSnapshot, ParentCoaching } from './types'

const original: ParentCoaching = {
  scenario: '今天一起回顾', why_this_fits: '来自已确认的学习记录', script: ['系统原话术'],
  action: { what: '系统原行动', duration_minutes: 8, steps: ['先整理'], stop_condition: '到时停止' }, avoid: [],
}
const action: ParentActionSnapshot = {
  binding_status: 'bound', snapshot_id: 'action-reviewed', child_id: 'child-a', report_id: 'report-a',
  version: 2, fingerprint: 'verified-action-version', feedback: null, execution_allowed: true,
  coaching: { ...original, script: ['家长改成的整套话术'],
    action: { what: '家长选定画出第一步', duration_minutes: 3, steps: ['画第一步'], stop_condition: '三分钟停止' } },
  timing: { report_date: '2026-09-07', when: null, duration_minutes: 3, planned_at: null },
  review_binding: { snapshot_id: 'review-a', source_version: 'review-v2', approval_event_id: 'approved-a',
    decision: 'custom', edit_fingerprint: 'edited-a', base_action_fingerprint: 'base-a' },
}
function report(parent_action: DailyReport['parent_action']): DailyReport {
  return { report_id: 'report-a', session_id: 'session-a', child_id: 'child-a', report_date: '2026-09-07',
    source: 'local_rules_v2', created_at: '2026-09-07T00:00:00Z',
    summary: { headline: '今天的学习', interpretation: '基于实际记录', facts: [], evidence_count: 0 },
    parent_coaching: original, parent_action, feedback: null,
    evidence_detail: { recalls: [], tasks: [], reflection: null, declarations: { no_recall: true, no_homework: true } } }
}
describe('reviewed parent action', () => {
  beforeEach(() => { vi.spyOn(localApi, 'parentActions').mockResolvedValue([]) })
  afterEach(() => { cleanup(); vi.restoreAllMocks() })
  it('shows the same custom script and action as the feedback version', () => {
    render(<ReportDetail report={report(action)} />)
    expect(screen.getByText('家长改成的整套话术')).toBeInTheDocument()
    expect(screen.getAllByText('家长选定画出第一步')).toHaveLength(2)
    expect(screen.getByRole('heading', { name: '今天具体行动 · 3分钟' })).toBeInTheDocument()
    expect(screen.queryByText('系统原行动')).not.toBeInTheDocument()
    expect(screen.queryByText('系统原话术')).not.toBeInTheDocument()
  })
  it('never offers execution or effect inputs for an approved skip decision', () => {
    render(<ReportDetail report={report({ ...action, execution_allowed: false,
      coaching: { ...original, script: [], action: null },
      review_binding: { ...action.review_binding!, decision: 'skip' } })} />)
    expect(screen.getByRole('heading', { name: '本次已选择跳过' })).toBeInTheDocument()
    expect(screen.queryByLabelText('执行情况')).not.toBeInTheDocument()
    expect(screen.queryByText('系统原行动')).not.toBeInTheDocument()
  })
  it('does not revive the old original action when review has been withdrawn', () => {
    render(<ReportDetail report={report({ binding_status: 'unknown', reason: 'review_approval_required' })} />)
    expect(screen.getByText('这份行动的审核或来源已变化，请在报告审核中重新核对采用方式。历史反馈保留，但不用于当前行动效果判断。')).toHaveAttribute('role', 'status')
    expect(screen.queryByLabelText('执行情况')).not.toBeInTheDocument()
    expect(screen.queryByText('系统原行动')).not.toBeInTheDocument()
  })
})
