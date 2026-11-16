import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { GoalRoadmapPanel } from './GoalRoadmapPanel'
import type { GoalRoadmap, GoalRoadmapNode } from './GoalRoadmapPanel.types'

vi.mock('./api', () => ({ requestParentLocal: vi.fn(async () => { throw new Error('offline') }) }))

const root: GoalRoadmapNode = {
  knowledge_node_id: 'private-root', title: '分数与小数', subject: '数学', baseline_status: 'needs_baseline',
  feedback_status: null, evidence_refs: [], latest_evidence_at: null, prerequisite_observed: false,
  mastery_claim_allowed: false, prerequisite_node_ids: [], blocked_by: [], source_ref: 'private-source', knowledge_version: 'v1',
  steps: [{ work_id: 'private-root:establish_baseline', action: 'establish_baseline', estimated_minutes: { min: 10, max: 15 },
    status: 'planned', estimate_only: true, minimum_gap_hours: 0, completion_basis: 'qualified_distinct_independent_response_not_time' }],
}

function fixture(): GoalRoadmap {
  return {
    version: 'goal_roadmap_v1', source_version: 'private-version', program_id: 'private-program', child_id: 'child-a',
    track: 'junior_exam_readiness', title: '中考能力路线', as_of_date: '2026-09-07', reference_date: '2026-09-07', target_date: '2026-12-20', status: 'estimated',
    baseline: [root], prerequisite_path: [root, { ...root, knowledge_node_id: 'private-target', title: '应用题迁移', blocked_by: ['private-root'], prerequisite_node_ids: ['private-root'], steps: [] }],
    cycles: [], milestones: {
      monthly: [{ month: '2026-09', target_date: '2026-09-30', work_ids: [root.steps[0].work_id], estimated_minutes: 15, status: 'planned', estimate_only: true }],
      weekly: [{ period_start: '2026-09-07', target_date: '2026-09-13', work_ids: [root.steps[0].work_id], estimated_minutes: 15, status: 'planned', estimate_only: true, basis: 'latest_estimated_window_not_assignment' }],
    },
    burden: { weekly_minutes: 60, weekly_sessions: 3, committed_weekly_minutes: 20, remaining_weekly_minutes: 40,
      auto_increase_budget: false, configured_budget_valid: true, capacity_basis: 'configured_goal_budget_not_verified_free_time',
      estimated_minutes: { min: 60, max: 100 }, available_minutes: 600, unallocated_minutes: 0, feasibility: 'provisional_not_guaranteed', estimate_complete: true, estimated_weeks: 2 },
    risks: [{ code: 'curriculum_coverage_unverified', message: '当前能力目录尚不能代表完整考试覆盖。' }], clarifications: [],
    next_daily_action: { action: 'establish_baseline', knowledge_node_id: root.knowledge_node_id, child_id: 'child-a', program_id: 'private-program', subject: '数学', title: root.title,
      minutes: 15, source_type: 'junior_exam_goal', source_ref: 'private-program', evidence_refs: [], requires_source_selection: true, dispatchable: true, status: 'suggested',
      completion_standard: '独立作答后核对，不按参与时长判断能力。', mastery_claim_allowed: false, cloud_transmission_allowed: false, roadmap_source_version: 'private-version' },
    negotiation_options: ['extend_deadline'], assumptions: { effort: 'unvalidated_planning_ranges_not_child_speed', freshness_days: 30, evidence_window_days: 90,
      calendar: 'uniform_weekly_capacity_estimate', retention: 'actual_scheduling_required', backward_windows: 'latest_replanning_windows', coverage: 'unverified' },
    evidence_fingerprint: 'private-fingerprint', goal_attainment: 'not_established', mastery_claim_allowed: false, local_only: true, cloud_transmission_allowed: false,
  }
}

afterEach(() => { cleanup(); vi.restoreAllMocks() })

describe('GoalRoadmapPanel', () => {
  it('separates a verified reference, personal registration and changed rules without invented coverage', () => {
    const roadmap = fixture()
    roadmap.exam_rules = { version: 'official_goal_rules_v1', public_rule_status: 'verified_reference_snapshot',
      binding_status: 'current', target_year: 2027, reference_year: null, registration_status: 'not_verified',
      coverage_status: 'not_audited_against_official_requirements', exam_readiness_claim_allowed: false,
      current_snapshot: { reference: { authority: 'Cambridge', url: 'https://www.cambridgeenglish.org/exams-and-tests/qualifications/preliminary/format/', verified_on: '2026-09-08' } },
      required_sections: [{ section_id: 'reading', title: 'Reading', blueprint_id: 'private-pet', duration_minutes: 45 }] }
    const { rerender } = render(<GoalRoadmapPanel childId="child-a" roadmap={roadmap} />)
    const panel = screen.getByRole('region', { name: '考试规则依据' })
    expect(within(panel).getByText('公开规则参考已核实')).toBeInTheDocument()
    expect(within(panel).getByText(/本人报名场次尚未核实/)).toBeInTheDocument()
    expect(within(panel).getByRole('link', { name: 'Cambridge · 2026-09-08 核对' })).toHaveAttribute('href', roadmap.exam_rules.current_snapshot!.reference.url)
    expect(within(panel).getByText('阅读 · 45 分钟')).toBeInTheDocument()
    expect(panel.textContent).not.toMatch(/100%|private-|已报名|预计通过/)
    roadmap.exam_rules = { ...roadmap.exam_rules, binding_status: 'source_changed', public_rule_status: 'target_year_unverified', reference_year: 2026, required_sections: [] }
    rerender(<GoalRoadmapPanel childId="child-a" roadmap={roadmap} />)
    expect(within(panel).getByText('2027 年规则尚未核实；已有参考仅适用 2026 年。')).toBeInTheDocument()
    expect(within(panel).getByText(/原绑定规则已变化/)).toBeInTheDocument()
    expect(within(panel).queryByText('阅读')).not.toBeInTheDocument()
  })

  it('does not render an untrusted rule link or expose another child rule snapshot', () => {
    const roadmap = fixture()
    roadmap.exam_rules = { version: 'official_goal_rules_v1', public_rule_status: 'event_rules_unverified', binding_status: 'unbound', target_year: null, reference_year: null,
      registration_status: 'not_verified', coverage_status: 'not_audited', exam_readiness_claim_allowed: false, required_sections: [],
      current_snapshot: { reference: { authority: 'Untrusted event', url: 'javascript:alert(1)', verified_on: '2026-09-08' } } }
    const { rerender } = render(<GoalRoadmapPanel childId="child-a" roadmap={roadmap} />)
    expect(screen.getByText('具体赛事规则尚未核实。')).toBeInTheDocument()
    expect(screen.queryByRole('link')).not.toBeInTheDocument()
    rerender(<GoalRoadmapPanel childId="child-b" roadmap={roadmap} />)
    expect(screen.queryByRole('region', { name: '考试规则依据' })).not.toBeInTheDocument()
  })
  it('shows dates, unknown baseline, named prerequisites and budget without mastery percentages or internal IDs', () => {
    render(<GoalRoadmapPanel childId="child-a" roadmap={fixture()} />)
    expect(screen.getByText('2026-12-20')).toBeInTheDocument()
    expect(screen.getAllByText('基线未知').length).toBeGreaterThan(0)
    expect(screen.getByText('先核对：分数与小数')).toBeInTheDocument()
    expect(screen.getByText('每周上限 60 分钟 · 3 次')).toBeInTheDocument()
    expect(screen.getByText('已占用 20 分钟 · 本周剩余 40 分钟')).toBeInTheDocument()
    expect(screen.getByText(/参与时长不代表能力/)).toBeInTheDocument()
    const content = document.body.cloneNode(true) as HTMLElement
    content.querySelectorAll('style').forEach((style) => style.remove())
    expect(content.textContent).not.toMatch(/private-|100%|已掌握|预计通过/)
  })

  it('does not guess a date or manufacture monthly windows when no date is known', () => {
    const roadmap = fixture()
    roadmap.target_date = null; roadmap.status = 'needs_clarification'; roadmap.milestones = { monthly: [], weekly: [] }
    roadmap.clarifications = [{ field: 'target_date', reason: '请确认实际目标日期。' }]
    roadmap.burden.available_minutes = null; roadmap.burden.feasibility = 'unknown'
    render(<GoalRoadmapPanel childId="child-a" roadmap={roadmap} />)
    expect(screen.getByText('目标日期待确认')).toBeInTheDocument()
    expect(screen.getByText('请确认实际目标日期。')).toBeInTheDocument()
    expect(screen.getByText('日期未确认，暂不列出月周里程碑。')).toBeInTheDocument()
    expect(screen.queryByText('2026-12-20')).not.toBeInTheDocument()
  })

  it('labels missing qualified sources as blocked, never as an assigned diagnostic', () => {
    const roadmap = fixture()
    roadmap.next_daily_action!.dispatchable = false; roadmap.next_daily_action!.status = 'qualified_source_needed'
    render(<GoalRoadmapPanel childId="child-a" roadmap={roadmap} />)
    expect(screen.getByText('缺合格题源，暂不安排诊断')).toBeInTheDocument()
    expect(screen.queryByText('可供规划选择，尚未分配题目')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /开始|训练/ })).not.toBeInTheDocument()
  })

  it('renders monthly and weekly work with human-readable actions, as estimates not assignments', () => {
    render(<GoalRoadmapPanel childId="child-a" roadmap={fixture()} />)
    fireEvent.click(screen.getByText('每周估算'))
    const weekly = screen.getByRole('region', { name: '每周里程碑' })
    expect(within(weekly).getByText('2026-09-07 至 2026-09-13')).toBeInTheDocument()
    expect(within(weekly).getByText('分数与小数 · 建立基线')).toBeInTheDocument()
    expect(screen.getByText(/倒排窗口不是已分配任务/)).toBeInTheDocument()
  })

  it('hides stale child data immediately on switch, including when loading or failed', () => {
    const { rerender } = render(<GoalRoadmapPanel childId="child-a" roadmap={fixture()} />)
    rerender(<GoalRoadmapPanel childId="child-b" roadmap={fixture()} loading />)
    expect(screen.queryByText('中考能力路线')).not.toBeInTheDocument()
    rerender(<GoalRoadmapPanel childId="child-b" roadmap={fixture()} error="network" />)
    expect(screen.queryByText('分数与小数')).not.toBeInTheDocument()
    expect(screen.queryByText('2026-12-20')).not.toBeInTheDocument()
  })

  it('hides unrefreshed suggestions on error and allows an explicit retry only', () => {
    const retry = vi.fn()
    render(<GoalRoadmapPanel childId="child-a" roadmap={fixture()} error="network" onRetry={retry} />)
    expect(screen.getByRole('alert')).toHaveTextContent('路线暂未核实')
    expect(screen.queryByText('分数与小数')).not.toBeInTheDocument()
    expect(retry).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: '重新读取路线' }))
    expect(retry).toHaveBeenCalledOnce()
  })

  it('renders empty and loading without inventing a plan or persisting anything', () => {
    const fetch = vi.spyOn(globalThis, 'fetch')
    const save = vi.spyOn(Storage.prototype, 'setItem')
    const { rerender } = render(<GoalRoadmapPanel childId="child-a" roadmap={null} loading />)
    expect(screen.getByRole('status')).toHaveTextContent('正在核对能力路线')
    rerender(<GoalRoadmapPanel childId="child-a" roadmap={undefined} />)
    expect(screen.getByText('尚无可核对的能力路线')).toBeInTheDocument()
    expect(fetch).not.toHaveBeenCalled(); expect(save).not.toHaveBeenCalled()
  })

  it('updates evidence withdrawal, stale status and source version without retaining the old action', () => {
    const { rerender } = render(<GoalRoadmapPanel childId="child-a" roadmap={fixture()} />)
    const changed = fixture()
    changed.source_version = 'new-version'; changed.prerequisite_path = [{ ...root, baseline_status: 'stale_recheck' }]
    changed.baseline = changed.prerequisite_path; changed.next_daily_action = null
    changed.risks.push({ code: 'stale_evidence', message: '证据已过核对窗口，需要重新作答。' })
    rerender(<GoalRoadmapPanel childId="child-a" roadmap={changed} />)
    expect(screen.getByText('证据待更新')).toBeInTheDocument()
    expect(screen.getByText('当前没有可自动安排的动作')).toBeInTheDocument()
    expect(screen.queryByText('应用题迁移')).not.toBeInTheDocument()
  })

  it('keeps over-capacity and incomplete estimates explicit without prescribing extra time', () => {
    const roadmap = fixture()
    roadmap.burden.feasibility = 'estimated_over_capacity'; roadmap.burden.unallocated_minutes = 45; roadmap.burden.estimate_complete = false
    render(<GoalRoadmapPanel childId="child-a" roadmap={roadmap} />)
    expect(screen.getByText('估算超出预算，需要协商')).toBeInTheDocument()
    expect(screen.getByText(/45 分钟估算工作未排入/)).toBeInTheDocument()
    expect(screen.getByText(/目录或前置来源未核齐/)).toBeInTheDocument()
    expect(screen.getByText(/不自动增加学习时间/)).toBeInTheDocument()
  })

  it.each(['child_id', 'program_id', 'roadmap_source_version'] as const)('rejects a next action with a different %s', (field) => {
    const roadmap = fixture()
    roadmap.next_daily_action![field] = 'different-binding'
    render(<GoalRoadmapPanel childId="child-a" roadmap={roadmap} />)
    expect(screen.getByText('动作绑定已变化，需要重新核对')).toBeInTheDocument()
    expect(screen.queryByText('可供规划选择，尚未分配题目')).not.toBeInTheDocument()
  })

  it('keeps the baseline unknown when time budget is fully used', () => {
    const roadmap = fixture()
    const { rerender } = render(<GoalRoadmapPanel childId="child-a" roadmap={roadmap} />)
    const used = structuredClone(roadmap)
    used.burden.committed_weekly_minutes = 60; used.burden.remaining_weekly_minutes = 0; used.next_daily_action = null
    rerender(<GoalRoadmapPanel childId="child-a" roadmap={used} />)
    expect(screen.getAllByText('基线未知').length).toBe(2)
    expect(screen.getByText('已占用 60 分钟 · 本周剩余 0 分钟')).toBeInTheDocument()
    expect(screen.getByText('当前没有可自动安排的动作')).toBeInTheDocument()
  })

  it('does not silently roll an invalid date into another month or expose orphan work IDs', () => {
    const roadmap = fixture()
    roadmap.target_date = '2026-02-30'
    const { rerender } = render(<GoalRoadmapPanel childId="child-a" roadmap={roadmap} />)
    expect(screen.getByText('目标日期待确认')).toBeInTheDocument()
    roadmap.target_date = '2026-12-20'; roadmap.milestones.monthly[0].work_ids = ['private-deleted-node']
    rerender(<GoalRoadmapPanel childId="child-a" roadmap={roadmap} />)
    expect(screen.getByText('待核对的能力步骤')).toBeInTheDocument()
    expect(screen.queryByText('private-deleted-node')).not.toBeInTheDocument()
  })

  it('rejects unknown schema versions instead of describing an old payload as current', () => {
    const roadmap = { ...fixture(), version: 'goal_roadmap_v99' } as unknown as GoalRoadmap
    render(<GoalRoadmapPanel childId="child-a" roadmap={roadmap} />)
    expect(screen.getByText('路线与当前孩子或版本不匹配，需要重新核对。')).toBeInTheDocument()
    expect(screen.queryByText('中考能力路线')).not.toBeInTheDocument()
  })

  it('shows retained evidence without certifying the goal and preserves the actual retention gap', () => {
    const roadmap = fixture()
    roadmap.prerequisite_path = [{ ...root, baseline_status: 'retention_observed', latest_evidence_at: '2026-09-06T09:00:00+00:00',
      steps: [{ ...root.steps[0], action: 'delayed_check', minimum_gap_hours: 24 }] }]
    roadmap.status = 'paused'
    render(<GoalRoadmapPanel childId="child-a" roadmap={roadmap} />)
    expect(screen.getByText('已有延迟复测证据')).toBeInTheDocument()
    expect(screen.getByText(/至少间隔 24 小时，执行时再核对/)).toBeInTheDocument()
    expect(screen.getByText('路线已暂停，不自动安排任务。')).toBeInTheDocument()
    expect(screen.getByText(/目标达成尚未证实/)).toBeInTheDocument()
  })
})
