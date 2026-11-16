import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { localApi } from './api'
import { ParentActionFeedbackPanel, clearParentActionPending, parentActionPendingPrefix } from './ParentActionFeedbackPanel'
import type { ParentActionSnapshot, ParentReportFeedback } from './types'

const snapshot: ParentActionSnapshot = {
  binding_status: 'bound', snapshot_id: 'action-1', report_id: 'report-1', child_id: 'child-1', version: 1, fingerprint: 'test',
  timing: { report_date: '2026-09-06', duration_minutes: 5, when: '晚饭后', planned_at: null }, feedback: null,
  coaching: { focus: 'correction', scenario: '孩子愿意谈一道错题时', why_this_fits: '一次只检查一步', script: ['你想先看哪一步？'], avoid: [],
    action: { what: '一起核对一道错题', duration_minutes: 5, steps: ['让孩子先说'], stop_condition: '孩子拒绝时停止' } },
}

describe('bound parent action feedback', () => {
  afterEach(() => { cleanup(); localStorage.clear(); vi.restoreAllMocks() })

  it('saves and reconciles a period action through child-scoped snapshot APIs, never a daily report', async () => {
    const period: ParentActionSnapshot = { ...snapshot, report_id: null, period_report_id: 'period-week', source_kind: 'period_report', source_id: 'period-week' }
    const dailySave = vi.spyOn(localApi, 'saveParentReportFeedback')
    const dailyList = vi.spyOn(localApi, 'parentActions')
    const save = vi.spyOn(localApi, 'saveParentActionFeedback').mockRejectedValueOnce(new Error('response lost'))
    const read = vi.spyOn(localApi, 'parentAction')
    const view = render(<ParentActionFeedbackPanel snapshot={period} />)
    fireEvent.change(screen.getByLabelText('执行情况'), { target: { value: 'no' } })
    fireEvent.click(screen.getByText('保存行动反馈'))
    await screen.findByText('重试原请求')
    expect(save).toHaveBeenCalledWith('child-1', 'action-1', expect.objectContaining({ executed: false }))
    const request = save.mock.calls[0][2]
    view.unmount()
    read.mockResolvedValue({ ...period, feedback: { revision: 1, operation: 'record', executed: false, client_action_id: request.client_action_id } as ParentReportFeedback })
    render(<ParentActionFeedbackPanel snapshot={period} />)
    fireEvent.click(screen.getByText('核对已保存反馈'))
    await screen.findByText('已核对：上次反馈已保存，没有重复提交。')
    expect(read).toHaveBeenCalledWith('child-1', 'action-1')
    expect(save).toHaveBeenCalledTimes(1)
    expect(dailySave).not.toHaveBeenCalled()
    expect(dailyList).not.toHaveBeenCalled()
  })

  it('does not invent execution or default outcome ratings; binds the saved action version', async () => {
    const save = vi.spyOn(localApi, 'saveParentReportFeedback').mockResolvedValue({ revision: 1, operation: 'record', executed: true } as ParentReportFeedback)
    render(<ParentActionFeedbackPanel snapshot={snapshot} />)
    expect(screen.getByLabelText('执行情况')).toHaveValue('unknown')
    fireEvent.change(screen.getByLabelText('执行情况'), { target: { value: 'yes' } })
    expect(screen.getByLabelText('实际效果')).toHaveValue('')
    fireEvent.click(screen.getByText('保存行动反馈'))
    expect(save).not.toHaveBeenCalled()
    fireEvent.change(screen.getByLabelText('实际执行时间'), { target: { value: '2026-09-06T18:00' } })
    fireEvent.click(screen.getByText('保存行动反馈'))
    await waitFor(() => expect(save).toHaveBeenCalledTimes(1))
    expect(save.mock.calls[0][1]).toMatchObject({ action_snapshot_id: 'action-1', expected_revision: 0, executed: true, effectiveness: null, stress_rating: null })
    expect(save.mock.calls[0][1].executed_at).toBe(new Date('2026-09-06T18:00').toISOString())
    await screen.findByText(/反馈已保存到这一个行动版本/)
    expect(localStorage.getItem(parentActionPendingPrefix('child-1') + 'action-1')).toBeNull()
  })

  it('restores a lost-receipt request after remount and retries exactly the same payload', async () => {
    const save = vi.spyOn(localApi, 'saveParentReportFeedback').mockRejectedValueOnce(new Error('network'))
    const view = render(<ParentActionFeedbackPanel snapshot={snapshot} />)
    fireEvent.change(screen.getByLabelText('家庭备注'), { target: { value: '先听他说完' } })
    fireEvent.change(screen.getByLabelText('执行情况'), { target: { value: 'no' } })
    fireEvent.click(screen.getByText('保存行动反馈'))
    await screen.findByText('重试原请求')
    const request = save.mock.calls[0][1]
    view.unmount()
    render(<ParentActionFeedbackPanel snapshot={snapshot} />)
    expect(screen.getByLabelText('家庭备注')).toHaveValue('先听他说完')
    expect(screen.getByLabelText('执行情况')).toBeDisabled()
    save.mockResolvedValueOnce({ revision: 1, operation: 'record', executed: false, client_action_id: request.client_action_id } as ParentReportFeedback)
    fireEvent.click(screen.getByText('重试原请求'))
    await waitFor(() => expect(save).toHaveBeenCalledTimes(2))
    expect(save.mock.calls[1][1]).toEqual(request)
  })

  it('requires a reason for retraction and keeps children isolated during cleanup', async () => {
    const feedback = { revision: 1, operation: 'record', executed: false } as ParentReportFeedback
    const save = vi.spyOn(localApi, 'saveParentReportFeedback').mockResolvedValue({ revision: 2, operation: 'retract', executed: null } as ParentReportFeedback)
    render(<ParentActionFeedbackPanel snapshot={{ ...snapshot, feedback }} />)
    expect(screen.getByText('撤回这次反馈')).toBeDisabled()
    fireEvent.change(screen.getByLabelText('撤回原因'), { target: { value: '记错了日期' } })
    fireEvent.click(screen.getByText('撤回这次反馈'))
    await waitFor(() => expect(save).toHaveBeenCalledTimes(1))
    expect(save.mock.calls[0][1]).toMatchObject({ expected_revision: 1, operation: 'retract', executed: null, effectiveness: null, reason: '记错了日期' })
    localStorage.setItem(parentActionPendingPrefix('child-1') + 'pending', '1')
    localStorage.setItem(parentActionPendingPrefix('child-2') + 'pending', '2')
    clearParentActionPending('child-1')
    expect(localStorage.getItem(parentActionPendingPrefix('child-1') + 'pending')).toBeNull()
    expect(localStorage.getItem(parentActionPendingPrefix('child-2') + 'pending')).toBe('2')
  })
})
