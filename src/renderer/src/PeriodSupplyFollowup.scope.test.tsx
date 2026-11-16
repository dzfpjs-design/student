import { useLayoutEffect, useRef } from 'react'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { PeriodSupplyFollowup } from './PeriodSupplyFollowup'
import type { PeriodSupplyFollowupProps } from './PeriodSupplyFollowup'

afterEach(cleanup)

it('never exposes or opens the previous child root during the new scope layout commit', async () => {
  const read = vi.fn().mockResolvedValueOnce({
    child_id:'a',snapshot_id:'a-report',period_report_id:'p',as_of:'2026-09-08T12:00:00Z',
    status:'ready',unfulfilled_count:1,local_only:true,cloud_transmission_allowed:false,mastery_claim_allowed:false,
    items:[{historical:{root_obligation_id:'a-root',title:'仅甲孩子的原题',original_date:'2026-09-07',
      original_reason:'等待原材料',historical_status:'unfulfilled',deferral_reason:'保留',mastery_claim_allowed:false},
    current:{child_id:'a',root_obligation_id:'a-root',status:'ready',next_action:'核对原题',mastery_claim_allowed:false}}],
  }).mockImplementation(() => new Promise(() => {}))
  const opened = vi.fn().mockResolvedValue(undefined)
  const commits: string[] = []
  function Parent({child}: {child:string}) {
    const ref = useRef<HTMLDivElement>(null)
    useLayoutEffect(() => {
      if(child !== 'b') return
      commits.push(ref.current?.textContent ?? '')
      Array.from(ref.current?.querySelectorAll('button') ?? []).find(b => b.textContent?.includes('查看原事项'))?.click()
    }, [child])
    return <div ref={ref}><PeriodSupplyFollowup childId={child} snapshotId={`${child}-report`}
      request={read as PeriodSupplyFollowupProps['request']} onOpenSupply={opened}/></div>
  }
  const view = render(<Parent child="a"/>)
  fireEvent.click(screen.getByText('本期原练习与今天续行'))
  await screen.findByText('仅甲孩子的原题')
  view.rerender(<Parent child="b"/>)
  expect(commits).toHaveLength(1)
  expect(commits[0]).not.toContain('仅甲孩子的原题')
  expect(opened).not.toHaveBeenCalled()
})
