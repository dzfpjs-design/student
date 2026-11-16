import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { localApi } from './api'
import { ControlledContentPanel } from './ControlledContentPanel'

afterEach(() => { cleanup(); vi.restoreAllMocks() })

describe('ControlledContentPanel', () => {
  it('persists parent time limits and approval decisions', async () => {
    const rule = { child_id: 'child-4', enabled: false, max_daily_minutes: 20, max_item_minutes: 10, updated_at: '2026-09-01T00:00:00Z' }
    const candidate = {
      row_id: 12, title: '分数的意义', subject_title: '数学', grade_title: '四年级',
      unit_path: '第五单元', video_duration: 600, local_video_available: 1 as const,
      approval_status: 'not_approved' as const, max_minutes: null, note: null,
    }
    vi.spyOn(localApi, 'controlledContentRule').mockResolvedValue(rule)
    vi.spyOn(localApi, 'controlledContentCandidates').mockResolvedValue([candidate])
    const updateRule = vi.spyOn(localApi, 'updateControlledContentRule').mockResolvedValue({ ...rule, enabled: true })
    const approve = vi.spyOn(localApi, 'approveControlledContent').mockResolvedValue({ ...candidate, approval_status: 'approved', max_minutes: 10 })

    render(<ControlledContentPanel childId="child-4" />)
    await screen.findByText('分数的意义')
    fireEvent.click(screen.getByLabelText('允许语音播放许可内容'))
    await waitFor(() => expect(updateRule).toHaveBeenCalledWith('child-4', {
      enabled: true, max_daily_minutes: 20, max_item_minutes: 10,
    }))
    fireEvent.click(screen.getByRole('button', { name: '许可' }))
    await waitFor(() => expect(approve).toHaveBeenCalledWith('child-4', 12, 10, expect.any(String)))
    expect(await screen.findByText(/已许可，单次最多10分钟/)).toBeInTheDocument()
  })
})
