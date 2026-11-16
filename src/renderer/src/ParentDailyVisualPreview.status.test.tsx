import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { localApi } from './api'
import { ParentDailyVisualPreview } from './ParentDailyVisualPreview'

afterEach(() => { cleanup(); vi.restoreAllMocks() })
const base = {mode:'combined' as const,sending_safety_ready:true,last_check:{status:'waiting_for_cutoff'},next_day:'2026-11-05',result_unknown:false,last_sent_day:null}

it('shows the persisted sent day and next scheduled day', async () => {
  vi.spyOn(localApi,'combinedDailyStatus').mockResolvedValue({...base,last_sent_day:'2026-11-04'})
  render(<ParentDailyVisualPreview childId="a" />)
  expect(await screen.findByText(/已发送至 2026-11-04/)).toBeTruthy()
  expect(screen.getByText(/下一份：2026-11-05/)).toBeTruthy()
})

it('prioritizes unknown results over an older sent day', async () => {
  vi.spyOn(localApi,'combinedDailyStatus').mockResolvedValue({...base,last_sent_day:'2026-11-04',result_unknown:true})
  render(<ParentDailyVisualPreview childId="a" />)
  expect(await screen.findByText(/发送结果未知/)).toBeTruthy()
  expect(screen.queryByText(/已发送至/)).toBeNull()
})

it('explains an actual blocking reason without claiming a send', async () => {
  vi.spyOn(localApi,'combinedDailyStatus').mockResolvedValue({...base,last_check:{status:'blocked',message:'发件邮箱尚未完成测试。'}})
  render(<ParentDailyVisualPreview childId="a" />)
  expect(await screen.findByText('发件邮箱尚未完成测试。')).toBeTruthy()
  expect(screen.queryByText(/已发送至/)).toBeNull()
})

it('shows cleanup without claiming a send or inviting a retry', async () => {
  vi.spyOn(localApi,'combinedDailyStatus').mockResolvedValue({...base,last_check:{status:'blocked',message:'日报发送进程仍在结束，系统正在核对结果，暂不重复发送。'}})
  render(<ParentDailyVisualPreview childId="a" />)
  expect(await screen.findByText(/发送进程仍在结束/)).toBeTruthy()
  expect(screen.queryByText(/本次综合日报已发送/)).toBeNull()
})

it('shows the confirmed send while the cursor is catching up', async () => {
  vi.spyOn(localApi,'combinedDailyStatus').mockResolvedValue({...base,last_check:{status:'sent'}})
  render(<ParentDailyVisualPreview childId="a" />)
  expect(await screen.findByText(/本次综合日报已发送/)).toBeTruthy()
})
