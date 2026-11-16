import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from './api'
import { PeriodSupplyFollowup } from './PeriodSupplyFollowup'
import type { PeriodSupplyFollowupProps } from './PeriodSupplyFollowup'

afterEach(cleanup)
const data=(child='a',snapshot='s',status='practice_completed')=>({child_id:child,snapshot_id:snapshot,period_report_id:'p',as_of:'2026-09-08T12:00:00Z',status:'ready',unfulfilled_count:status==='practice_completed'?0:1,local_only:true,cloud_transmission_allowed:false,mastery_claim_allowed:false,items:[{historical:{root_obligation_id:'r',title:'原阅读练习',original_date:'2026-09-07',original_reason:'等待原题',historical_status:'unfulfilled',deferral_reason:'跨日保留',mastery_claim_allowed:false},current:{child_id:child,root_obligation_id:'r',status,next_action:status==='practice_completed'?'不再重复启动':'先核对今天计划',mastery_claim_allowed:false}}]})
const request=(fn:ReturnType<typeof vi.fn>)=>fn as PeriodSupplyFollowupProps['request']
const open=()=>fireEvent.click(screen.getByText('本期原练习与今天续行'))

describe('period original obligations',()=>{
  it('loads only on entry and separates frozen unfulfilled from actual completed',async()=>{
    const read=vi.fn().mockResolvedValue(data()),onOpen=vi.fn().mockRejectedValue(Error('failed'))
    render(<PeriodSupplyFollowup childId="a" snapshotId="s" request={request(read)} onOpenSupply={onOpen}/>)
    expect(read).not.toHaveBeenCalled();open()
    expect(await screen.findByText('已有练习完成记录')).toBeTruthy()
    expect(screen.getByText(/当时尚无完成记录/)).toBeTruthy()
    expect(screen.getByText(/当前有0项/)).toBeTruthy()
    fireEvent.click(screen.getByText('查看原事项'))
    expect(await screen.findByRole('alert')).toHaveTextContent('未能准确打开原事项')
    expect(onOpen).toHaveBeenCalledWith('r')
    expect(read.mock.calls[0][1]).toEqual({cache:'no-store'})
  })
  it('keeps unknown distinct from zero and fetches current after a remount',async()=>{
    const read=vi.fn().mockResolvedValue({...data(),status:'unavailable',items:[],unfulfilled_count:null})
    const first=render(<PeriodSupplyFollowup childId="a" periodReportId="p" request={request(read)}/>);open()
    expect(await screen.findByText(/不表示当时没有待办/)).toBeTruthy()
    first.unmount();read.mockResolvedValue(data('a','s','needs_plan'))
    render(<PeriodSupplyFollowup childId="a" periodReportId="p" request={request(read)}/>);open()
    expect(await screen.findByText('待核对今天计划')).toBeTruthy()
    expect(read).toHaveBeenLastCalledWith('/v1/parent/children/a/period-reports/p/supply-followups',{cache:'no-store'})
  })
  it('ignores late sibling result and refuses a mismatched original root',async()=>{
    let resolve!:(value:unknown)=>void
    const read=vi.fn().mockImplementationOnce(()=>new Promise(r=>{resolve=r})).mockResolvedValue(data('b','t'))
    const view=render(<PeriodSupplyFollowup childId="a" snapshotId="s" request={request(read)}/>);open()
    await waitFor(()=>expect(read).toHaveBeenCalledTimes(1))
    view.rerender(<PeriodSupplyFollowup childId="b" snapshotId="t" request={request(read)}/>)
    await screen.findByText('已有练习完成记录')
    await act(async()=>{resolve(data());await Promise.resolve()})
    expect(screen.queryByRole('alert')).toBeNull()
    read.mockResolvedValue({...data('b','t'),items:[{...data('b','t').items[0],current:{...data('b','t').items[0].current,root_obligation_id:'wrong'}}]})
    fireEvent.click(screen.getByLabelText('刷新当前续行状态'))
    expect(await screen.findByRole('alert')).toHaveTextContent('当前原事项未能读全')
    expect(screen.queryByText('已有练习完成记录')).toBeNull()
  })
  it('clears deleted or unauthorized detail, never writes to retry',async()=>{
    const read=vi.fn().mockResolvedValue(data())
    render(<PeriodSupplyFollowup childId="a" snapshotId="s" request={request(read)}/>);open()
    await screen.findByText('已有练习完成记录')
    read.mockRejectedValue(new ApiError('gone',404))
    fireEvent.click(screen.getByLabelText('刷新当前续行状态'))
    expect(await screen.findByRole('alert')).toHaveTextContent('已删除')
    expect(screen.queryByText('原阅读练习')).toBeNull()
    expect(read.mock.calls.every(c=>!c[1]?.method)).toBe(true)
  })
})
