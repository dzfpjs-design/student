import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { localApi } from './api'
import { ControlledGamePanel } from './ControlledGamePanel'

afterEach(() => { cleanup(); vi.restoreAllMocks(); window.desktop = undefined })

describe('ControlledGamePanel', () => {
  it('persists time limits and a parent-selected executable allowlist entry', async () => {
    const rule = { child_id: 'child-4', enabled: false, max_daily_minutes: 20, max_item_minutes: 10, updated_at: '2026-09-01T00:00:00Z' }
    vi.spyOn(localApi, 'controlledGameRule').mockResolvedValue(rule)
    vi.spyOn(localApi, 'controlledGameApprovals').mockResolvedValue([])
    const updateRule = vi.spyOn(localApi, 'updateControlledGameRule').mockResolvedValue({ ...rule, enabled: true })
    const approve = vi.spyOn(localApi, 'approveControlledGame').mockResolvedValue({
      approval_id: 'approval-1', child_id: 'child-4', title: 'MathGame',
      executable_path: 'C:\\Games\\MathGame.exe', status: 'approved', max_minutes: 10,
      note: '家长在本机选择并许可', file_size: 10, file_mtime_ns: 20,
      created_at: '2026-09-01T00:00:00Z', updated_at: '2026-09-01T00:00:00Z',
    })
    window.desktop = {
      chooseControlledGameExecutable: vi.fn().mockResolvedValue({ path: 'C:\\Games\\MathGame.exe' }),
    } as unknown as NonNullable<typeof window.desktop>

    render(<ControlledGamePanel childId="child-4" />)
    await screen.findByText(/还没有许可任何游戏/)
    fireEvent.click(screen.getByLabelText('允许语音启动许可游戏'))
    await waitFor(() => expect(updateRule).toHaveBeenCalledWith('child-4', {
      enabled: true, max_daily_minutes: 20, max_item_minutes: 10,
    }))
    fireEvent.click(screen.getByRole('button', { name: '许可本机游戏' }))
    await waitFor(() => expect(approve).toHaveBeenCalledWith('child-4', {
      title: 'MathGame', executable_path: 'C:\\Games\\MathGame.exe', max_minutes: 10,
      note: expect.any(String),
    }))
    expect(await screen.findByText(/已许可，单次最多10分钟/)).toBeInTheDocument()
  })
})
