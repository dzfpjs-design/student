import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { localApi } from './api'
import { ChildGrowthModelPanel } from './ParentCenter'
import type { LongitudinalChildModel } from './types'
import recorded from './test/fixtures/companionReturnActualHttp.json'

// Actual parent GET bodies, with only the unrelated optional full stage graph
// omitted. C05 values are unchanged from the answer/restart/withdrawal HTTP test.
const packets = recorded as unknown as Record<'verified' | 'restarted' | 'withdrawn' | 'other', LongitudinalChildModel>
const owner = packets.verified.child_id
const sibling = packets.other.child_id

describe('current companion-return evidence from actual parent HTTP', () => {
  beforeEach(() => {
    vi.spyOn(localApi, 'goalProgramCycles').mockResolvedValue({ items: [], next_daily_action: null })
    vi.spyOn(localApi, 'specializedRoadmaps').mockResolvedValue([])
    vi.spyOn(localApi, 'childModelCorrections').mockResolvedValue([])
  })
  afterEach(() => { cleanup(); vi.restoreAllMocks() })

  it('shows v4 proven unexecuted launches separately from actual usage', async () => {
    const model = structuredClone(packets.verified)
    model.payload.companion_balance = {...model.payload.companion_balance!,
      version: 'companion_balance_v4', not_dispatched_launches: 15,
      game_sessions: 0, video_sessions: 0}
    vi.spyOn(localApi, 'longitudinalChildModel').mockResolvedValue(model)
    render(<ChildGrowthModelPanel childId={owner} />)
    expect(await screen.findByText(/另有 15 次启动已核实未执行/)).toBeInTheDocument()
    expect(screen.getByText(/未计入视频或游戏使用次数；原记录仍保留/)).toBeInTheDocument()
    expect(screen.queryByText(/历史统计尚未按实际返回证据复核/)).not.toBeInTheDocument()
  })

  it('shows current v3 verified return without calling it habit or learning completion', async () => {
    expect(packets.verified.payload.companion_balance?.version).toBe('companion_balance_v3')
    expect(packets.verified.payload.companion_balance?.habit_success_sessions).toBe(0)
    vi.spyOn(localApi, 'longitudinalChildModel').mockResolvedValue(packets.verified)
    render(<ChildGrowthModelPanel childId={owner} />)
    expect(await screen.findByText(/原任务恢复计时回执 1 次；实际返回学习核验 1 次/)).toBeInTheDocument()
    expect(screen.getByText(packets.verified.payload.companion_balance!.guidance!)).toBeInTheDocument()
    expect(screen.queryByText(/历史统计尚未按实际返回证据复核/)).not.toBeInTheDocument()
    expect(screen.getByText(/单次返回不认证习惯改善或学习完成/)).toBeInTheDocument()
  })

  it('rereads withdrawn source as unverified while retaining its real resume receipt', async () => {
    const read = vi.spyOn(localApi, 'longitudinalChildModel')
      .mockResolvedValueOnce(packets.verified).mockResolvedValueOnce(packets.withdrawn)
    render(<ChildGrowthModelPanel childId={owner} />)
    await screen.findByText(/实际返回学习核验 1 次/)
    fireEvent.click(screen.getByRole('button', { name: '更新证据' }))
    expect(await screen.findByText(/原任务恢复计时回执 1 次；实际返回学习核验 0 次/)).toBeInTheDocument()
    expect(screen.getByText(packets.withdrawn.payload.companion_balance!.guidance!)).toBeInTheDocument()
    expect(screen.queryByText(/实际返回学习核验 1 次/)).not.toBeInTheDocument()
    expect(read.mock.calls).toEqual([[owner], [owner]])
  })

  it('restores the same verified evidence from the real post-restart response', async () => {
    const read = vi.spyOn(localApi, 'longitudinalChildModel')
      .mockResolvedValueOnce(packets.verified).mockResolvedValueOnce(packets.restarted)
    const first = render(<ChildGrowthModelPanel childId={owner} />)
    await screen.findByText(/实际返回学习核验 1 次/)
    first.unmount()
    render(<ChildGrowthModelPanel childId={owner} />)
    await screen.findByText(/原任务恢复计时回执 1 次；实际返回学习核验 1 次/)
    expect(read).toHaveBeenCalledTimes(2)
    expect(packets.restarted.payload.companion_balance?.habit_success_sessions).toBe(0)
  })

  it('uses the real keyed child boundary and ignores a late refresh from the prior child', async () => {
    let finish!: (value: LongitudinalChildModel) => void
    let ownerReads = 0
    const pending = new Promise<LongitudinalChildModel>(resolve => { finish = resolve })
    vi.spyOn(localApi, 'longitudinalChildModel').mockImplementation(async child => {
      if (child === sibling) return packets.other
      ownerReads += 1
      return ownerReads === 1 ? packets.verified : ownerReads === 2 ? pending : packets.withdrawn
    })
    // ParentCenter mounts this exact keyed boundary (growth-model-${childId}).
    const view = render(<ChildGrowthModelPanel key={`growth-model-${owner}`} childId={owner} />)
    await screen.findByText(/实际返回学习核验 1 次/)
    fireEvent.click(screen.getByRole('button', { name: '更新证据' }))
    await waitFor(() => expect(ownerReads).toBe(2))
    view.rerender(<ChildGrowthModelPanel key={`growth-model-${sibling}`} childId={sibling} />)
    await screen.findByText(/原任务恢复计时回执 0 次；实际返回学习核验 0 次/)
    await act(async () => { finish(packets.verified); await pending })
    expect(screen.queryByText(/实际返回学习核验 1 次/)).not.toBeInTheDocument()
    view.rerender(<ChildGrowthModelPanel key={`growth-model-${owner}`} childId={owner} />)
    await screen.findByText(/原任务恢复计时回执 1 次；实际返回学习核验 0 次/)
  })
})
