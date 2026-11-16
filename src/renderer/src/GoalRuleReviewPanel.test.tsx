import { act, fireEvent, render, screen, waitFor, cleanup } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { GoalRuleReviewPanel, clearGoalRuleReviewLocalState } from './GoalRuleReviewPanel'
import { ApiError, localApi } from './api'
import type { GoalRuleReview } from './goalRuleReviewTypes'

vi.mock('./api', async () => {
  const real = await vi.importActual<typeof import('./api')>('./api')
  return { ApiError: real.ApiError, localApi: { goalRuleReview: vi.fn(), confirmGoalRules: vi.fn() } }
})
const review: GoalRuleReview = {
  child_id:'child-a',program_id:'program-a',track:'pet_path',program_version:'a'.repeat(64),can_confirm:true,
  local_only:true,cloud_transmission_allowed:false,
  rules:{version:'official_goal_rules_v1',public_rule_status:'verified_reference_snapshot',binding_status:'source_changed',
    target_year:2026,reference_year:null,registration_status:'not_verified',coverage_status:'not_audited',
    exam_readiness_claim_allowed:false,required_sections:[],bound_snapshot:null,
    current_snapshot:{version:'rules-v2',snapshot_hash:'b'.repeat(64),reference:{authority:'Cambridge',
      url:'https://www.cambridgeenglish.org/exams-and-tests/preliminary/exam-format/',verified_on:'2026-09-08'},
      blueprint_snapshots:[{blueprint_id:'b1',title:'PET 参考卷型',sections:[
        {section_id:'reading',title:'阅读',duration_minutes:45,item_target:32,item_target_unit:'questions'},
        {section_id:'speaking',title:'口语',duration_minutes:12,item_target:4,item_target_unit:'parts',operational_duration_basis:'pair_upper_bound',duration_variants:[
          {candidate_count:2,min_minutes:10,max_minutes:12,label:'两人组'},
          {candidate_count:3,min_minutes:15,max_minutes:17,label:'三人组'},
        ]},
      ]}]}}
}
function receipt(value=review) {
  return {review:{...value,rules:{...value.rules,binding_status:'current'}},replayed:false,
    receipt:{revision_id:'r1',child_id:value.child_id,program_id:value.program_id,rule_hash:'b'.repeat(64),confirmed_at:'2026-09-09'}}
}
const props={childId:'child-a',programId:'program-a',track:'pet_path',onSaved:vi.fn()}
async function open() {
  fireEvent.click(screen.getByRole('button',{name:'核对规则与适用范围'}))
  await screen.findByText('PET 参考卷型')
}
beforeEach(()=>{vi.clearAllMocks();localStorage.clear();vi.mocked(localApi.goalRuleReview).mockResolvedValue(structuredClone(review));vi.mocked(localApi.confirmGoalRules).mockResolvedValue(receipt())})
afterEach(cleanup)

describe('parent rule acknowledgement',()=>{
  it('renders exact reference and confirms only checked current version, then refreshes route',async()=>{
    render(<GoalRuleReviewPanel {...props}/>); await open()
    expect(screen.getByText('阅读 · 45 分钟 · 32 题')).toBeTruthy()
    expect(screen.getByText('口语 · 两人组 10–12 分钟；三人组 15–17 分钟 · 4 部分')).toBeTruthy()
    const button=screen.getByRole('button',{name:'确认本次参考'})
    expect(button).toBeDisabled()
    expect(localApi.confirmGoalRules).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('checkbox')); fireEvent.click(button)
    await screen.findByText(/已保存这次核对/)
    expect(localApi.confirmGoalRules).toHaveBeenCalledWith('child-a','pet_path',expect.objectContaining({expected_rule_version:'rules-v2',expected_rule_hash:'b'.repeat(64),expected_program_version:'a'.repeat(64)}))
    expect(props.onSaved).toHaveBeenCalledTimes(1)
  })
  it('retries exact lost response after remount without adopting a new snapshot',async()=>{
    vi.mocked(localApi.confirmGoalRules).mockRejectedValueOnce(new Error('lost'))
    const ui=render(<GoalRuleReviewPanel {...props}/>);await open()
    fireEvent.click(screen.getByRole('checkbox'));fireEvent.click(screen.getByRole('button',{name:'确认本次参考'}))
    await screen.findByText(/尚未确认保存结果/)
    const original=vi.mocked(localApi.confirmGoalRules).mock.calls[0][2]
    ui.unmount();render(<GoalRuleReviewPanel {...props}/>)
    fireEvent.click(screen.getByRole('button',{name:'重试上次规则确认'}))
    await screen.findByText(/已保存这次核对/)
    expect(vi.mocked(localApi.confirmGoalRules).mock.calls[1][2]).toEqual(original)
  })
  it('409 requires a fresh read and fresh acknowledgement, never automatic POST',async()=>{
    vi.mocked(localApi.confirmGoalRules).mockRejectedValueOnce(new ApiError('规则已变化，请重新核对',409,'GOAL_RULE_SOURCE_CHANGED'))
    render(<GoalRuleReviewPanel {...props}/>);await open();fireEvent.click(screen.getByRole('checkbox'))
    fireEvent.click(screen.getByRole('button',{name:'确认本次参考'}));await screen.findByText('规则已变化，请重新核对')
    expect(screen.queryByRole('checkbox')).toBeNull()
    expect(screen.queryByRole('button',{name:'重试上次规则确认'})).toBeNull()
    await open();expect(screen.getByRole('checkbox')).not.toBeChecked();expect(localApi.confirmGoalRules).toHaveBeenCalledTimes(1)
  })
  it('late another-child read cannot appear in the current panel',async()=>{
    let resolve!:(v:GoalRuleReview)=>void
    vi.mocked(localApi.goalRuleReview).mockReturnValueOnce(new Promise(r=>{resolve=r}))
    const ui=render(<GoalRuleReviewPanel {...props}/>)
    fireEvent.click(screen.getByRole('button',{name:'核对规则与适用范围'}))
    ui.rerender(<GoalRuleReviewPanel {...props} childId="child-b" programId="program-b"/>)
    await act(async()=>resolve(review))
    expect(screen.queryByText('PET 参考卷型')).toBeNull();expect(screen.queryByRole('checkbox')).toBeNull()
  })
  it('unknown public qualification is still visible and no snapshot cannot be confirmed',async()=>{
    vi.mocked(localApi.goalRuleReview).mockResolvedValue({...review,can_confirm:false,rules:{...review.rules,current_snapshot:null,public_rule_status:'rules_unverified'}})
    render(<GoalRuleReviewPanel {...props}/>)
    fireEvent.click(screen.getByRole('button',{name:'核对规则与适用范围'}))
    await screen.findByText('目标规则尚待核实。')
    expect(screen.queryByRole('button',{name:'确认本次参考'})).toBeNull()
  })
  it('clears only this child journal on privacy cleanup',()=>{
    localStorage.setItem('hsc:goal-rule-confirmation:child-a:program-a:pet_path','{}')
    localStorage.setItem('hsc:goal-rule-confirmation:child-b:program-b:pet_path','{}')
    clearGoalRuleReviewLocalState('child-a')
    expect(Object.keys(localStorage)).toEqual(['hsc:goal-rule-confirmation:child-b:program-b:pet_path'])
  })
})
