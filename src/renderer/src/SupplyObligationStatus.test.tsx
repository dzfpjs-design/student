import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { SupplyObligationStatus, type SupplyObligation } from './SupplyObligationStatus'

afterEach(cleanup)
const item: SupplyObligation = {
  obligation_id: 'local-original-obligation', title: 'PET 写作练习', status: 'active',
  message: '已补题并安排，正在练习。', plan_item_id: 'local-exact-task', task_status: 'active',
  local_only: true, cloud_transmission_allowed: false, mastery_claim_allowed: false,
}

describe('current original obligation, never a mastery claim', () => {
  it('shows the authoritative arranged status, not the immutable old missing decision', () => {
    const { container } = render(<SupplyObligationStatus obligations={[item]} />)
    expect(screen.getByText('已补题并安排，正在练习。')).toBeTruthy()
    expect(screen.queryByText('待补材料')).toBeNull()
    expect(container.textContent).not.toContain('local-exact-task')
    expect(container.textContent).not.toContain('local-original-obligation')
  })
  it('reads paused recovery and source withdrawal without choosing a replacement', () => {
    const { rerender } = render(<SupplyObligationStatus obligations={[{ ...item, status: 'paused', message: '原练习已安排并暂停，进度保留，可以继续原任务。' }]} />)
    expect(screen.getByText('进度已保留')).toBeTruthy()
    rerender(<SupplyObligationStatus obligations={[{ ...item, status: 'source_changed', message: '题目来源已变化，原进度保留。' }]} />)
    expect(screen.getByText('材料需重核')).toBeTruthy()
    expect(screen.queryAllByRole('button')).toHaveLength(0)
  })
  it('does not equate practice completion with mastery', () => {
    render(<SupplyObligationStatus obligations={[{ ...item, status: 'practice_completed', message: '这项练习已完成；练习完成不等于已经掌握。' }]} />)
    expect(screen.getByText('这项练习已完成；练习完成不等于已经掌握。')).toBeTruthy()
  })
  it('keeps old clients without the projection quiet', () => {
    const { container } = render(<SupplyObligationStatus />)
    expect(container.textContent).toBe('')
  })
  it('labels retained unanswered work without pretending it is scheduled or missing a question', () => {
    render(<SupplyObligationStatus obligations={[{ ...item, status: 'needs_plan', plan_item_id: null,
      task_status: null, message: '原题还没回答，进度保留，尚未安排新的练习。' }]} />)
    expect(screen.getByText('待重新安排')).toBeTruthy()
    expect(screen.getByText('原题还没回答，进度保留，尚未安排新的练习。')).toBeTruthy()
    expect(screen.queryByText('已安排')).toBeNull()
    expect(screen.queryByText('待补材料')).toBeNull()
    expect(screen.queryAllByRole('button')).toHaveLength(0)
  })
})
