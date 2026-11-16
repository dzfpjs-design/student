import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { requestParentLocal } from './api'
import { GoalRoadmapPanel } from './GoalRoadmapPanel'
import type { GoalRoadmap } from './GoalRoadmapPanel.types'

function evidence() {
  return {
    version: 'goal_section_evidence_v1', child_id: 'child-a', as_of_date: '2026-09-09',
    time_basis: 'current_source_and_receipt_at_read_not_end_of_day', context_version: 'private-context',
    rule_snapshot_hash: 'private-rules', rule_binding_status: 'current', public_rule_status: 'verified_reference_snapshot',
    status: 'scope_inventory_not_official_coverage', exam_readiness_claim_allowed: false,
    local_only: true, cloud_transmission_allowed: false,
    unavailable_item_ids: ['private-unavailable'], unmapped_item_ids: [], unmapped_or_unavailable_opportunity_count: 2,
    sections: [{
      section_id: 'speaking', title: 'Speaking', blueprint_id: 'private-blueprint', part_count: 4, item_target: null,
      official_part_mapping: 'missing', coverage_status: 'missing_reviewed_part_mapping',
      scope_status: 'practice_scope_available', mastery_claim_allowed: false,
      sources: [
        { diagnostic_item_id: 'private-item', source_qualification: 'parent_reviewed_source', answer_type: 'speaking', task_family: 'picture_description' },
        { diagnostic_item_id: 'private-item-2', source_qualification: 'shipped_reference_source', answer_type: 'read_aloud', task_family: null },
      ], opportunities: [{}, {}, {}], qualified_evidence: [{}, {}],
      available_source_count: 2, reviewed_source_count: 1, opportunity_count: 3, qualified_evidence_count: 2,
      independent_correct_count: 1, distinct_qualified_questions: 1, distinct_independent_correct_questions: 1,
    }],
  }
}

function roadmap(section: unknown = evidence()): GoalRoadmap {
  return {
    version: 'goal_roadmap_v1', child_id: 'child-a', program_id: 'private-program', source_version: 'private-source',
    track: 'pet_path', title: 'PET 能力路线', as_of_date: '2026-09-09', reference_date: '2026-09-09',
    target_date: null, status: 'needs_clarification', prerequisite_path: [], baseline: [], cycles: [],
    milestones: { monthly: [], weekly: [] }, risks: [], clarifications: [], next_daily_action: null,
    negotiation_options: [], burden: { configured_budget_valid: false, feasibility: 'unknown', estimated_minutes: { min: 0, max: 0 }, unallocated_minutes: 0, available_minutes: null }, assumptions: {},
    section_evidence: section,
  } as unknown as GoalRoadmap
}

vi.mock('./api', () => ({ requestParentLocal: vi.fn(async () => { throw new Error('offline') }) }))
afterEach(() => { cleanup(); vi.mocked(requestParentLocal).mockReset().mockRejectedValue(new Error('offline')) })

describe('GoalRoadmapPanel section evidence', () => {
  it('mounts distinct evidence levels without claiming official parts or revealing private fields', () => {
    render(<GoalRoadmapPanel childId="child-a" roadmap={roadmap()} />)
    const panel = screen.getByRole('region', { name: '练习与评分证据' })
    expect(within(panel).getByRole('heading', { name: '口语' })).toBeInTheDocument()
    for (const text of ['可用练习题源', '实际作答', '可靠评分', '独立正确', '家长审核题源 1 道', '内置参考题源 1 道']) {
      expect(within(panel).getByText(text)).toBeInTheDocument()
    }
    expect(within(panel).getByText('3 次')).toBeInTheDocument()
    expect(within(panel).getByText('官方题型覆盖尚未核验')).toBeInTheDocument()
    expect(within(panel).getByText(/另有 2 次历史作答/)).toBeInTheDocument()
    const visible = panel.cloneNode(true) as HTMLElement
    visible.querySelectorAll('style').forEach((style) => style.remove())
    expect(visible.textContent).not.toMatch(/private-|100%|考试准备好|已掌握|picture_description/)
  })

  it('immediately replaces current counts after same-child source withdrawal and fresh remount', () => {
    const { rerender, unmount } = render(<GoalRoadmapPanel childId="child-a" roadmap={roadmap()} />)
    const withdrawn = evidence()
    Object.assign(withdrawn.sections[0], { sources: [], opportunities: [], qualified_evidence: [],
      available_source_count: 0, reviewed_source_count: 0, opportunity_count: 0, qualified_evidence_count: 0,
      independent_correct_count: 0, distinct_qualified_questions: 0, distinct_independent_correct_questions: 0, scope_status: 'missing' })
    withdrawn.context_version = 'withdrawn'
    rerender(<GoalRoadmapPanel childId="child-a" roadmap={roadmap(withdrawn)} />)
    expect(screen.queryByText('家长审核题源 1 道')).not.toBeInTheDocument()
    expect(screen.getByText('暂无当前可用题源')).toBeInTheDocument()
    unmount()
    render(<GoalRoadmapPanel childId="child-a" roadmap={roadmap(JSON.parse(JSON.stringify(withdrawn)))} />)
    expect(screen.getByText('暂无当前可用题源')).toBeInTheDocument()
  })

  it('hides another child section even within an otherwise matching roadmap', () => {
    const wrong = evidence(); wrong.child_id = 'child-b'
    const { rerender } = render(<GoalRoadmapPanel childId="child-a" roadmap={roadmap(wrong)} />)
    expect(screen.queryByText('家长审核题源 1 道')).not.toBeInTheDocument()
    expect(screen.getByText('练习证据尚未核实，请重新读取路线。')).toBeInTheDocument()
    rerender(<GoalRoadmapPanel childId="child-b" roadmap={roadmap()} />)
    expect(screen.queryByRole('region', { name: '练习与评分证据' })).not.toBeInTheDocument()
  })

  it.each([undefined, null, [], { version: 'future' }])('does not manufacture zero counts for absent or invalid evidence %j', async (invalid) => {
    const value = roadmap(); Object.assign(value, { section_evidence: invalid })
    render(<GoalRoadmapPanel childId="child-a" roadmap={value} />)
    expect(await screen.findByText('练习证据尚未核实，请重新读取路线。')).toBeInTheDocument()
    const panel = screen.getByRole('region', { name: '练习与评分证据' })
    expect(panel.textContent).not.toMatch(/0 次|0 道/)
  })

  it('does not show unverified counts when the score summary contradicts its receipts', () => {
    const invalid = evidence(); invalid.sections[0].qualified_evidence_count = 9
    render(<GoalRoadmapPanel childId="child-a" roadmap={roadmap(invalid)} />)
    expect(screen.getByText('练习证据尚未核实，请重新读取路线。')).toBeInTheDocument()
    expect(screen.queryByText('9 次')).not.toBeInTheDocument()
  })

  it('has no old evidence during refresh or an error and does not create a separate fetch loop', () => {
    const { rerender } = render(<GoalRoadmapPanel childId="child-a" roadmap={roadmap()} />)
    rerender(<GoalRoadmapPanel childId="child-a" roadmap={roadmap()} loading />)
    expect(screen.queryByText('家长审核题源 1 道')).not.toBeInTheDocument()
    rerender(<GoalRoadmapPanel childId="child-a" roadmap={roadmap()} error="offline" />)
    expect(screen.queryByRole('region', { name: '练习与评分证据' })).not.toBeInTheDocument()
  })

  it('reads the explicit parent roadmap because ordinary cycles do not contain section evidence', async () => {
    const value = roadmap(); delete value.section_evidence
    vi.mocked(requestParentLocal).mockResolvedValueOnce(roadmap())
    render(<GoalRoadmapPanel childId="child-a" roadmap={value} />)
    expect(await screen.findByText('家长审核题源 1 道')).toBeInTheDocument()
    expect(requestParentLocal).toHaveBeenCalledWith('/v1/parent/children/child-a/goal-programs/pet_path/roadmap',
      expect.objectContaining({ cache: 'no-store', signal: expect.any(AbortSignal) }))
    expect(requestParentLocal).toHaveBeenCalledTimes(1)
    vi.mocked(requestParentLocal).mockRejectedValueOnce(new Error('withdrawal refresh offline'))
    fireEvent.click(screen.getByRole('button', { name: '刷新练习证据' }))
    expect(screen.queryByText('家长审核题源 1 道')).not.toBeInTheDocument()
    expect(await screen.findByText('练习证据尚未核实，请重新读取路线。')).toBeInTheDocument()
  })

  it('rejects late old-child and same-child old-program requests without replacing the new evidence', async () => {
    let resolve!: (value: GoalRoadmap) => void
    vi.mocked(requestParentLocal).mockImplementationOnce(() => new Promise<GoalRoadmap>((done) => { resolve = done }))
    const original = roadmap(); delete original.section_evidence
    const { rerender } = render(<GoalRoadmapPanel childId="child-a" roadmap={original} />)
    const next = { ...original, program_id: 'new-program' }
    vi.mocked(requestParentLocal).mockResolvedValueOnce({ ...roadmap(), program_id: 'new-program', section_evidence: undefined })
    rerender(<GoalRoadmapPanel childId="child-a" roadmap={next} />)
    await act(async () => { resolve(roadmap()) })
    expect(screen.queryByText('家长审核题源 1 道')).not.toBeInTheDocument()
    expect(vi.mocked(requestParentLocal).mock.calls[0][1]?.signal?.aborted).toBe(true)
    vi.mocked(requestParentLocal).mockResolvedValueOnce(roadmap())
    rerender(<GoalRoadmapPanel childId="child-b" roadmap={{ ...original, child_id: 'child-b' }} />)
    await act(async () => {})
    expect(screen.queryByText('家长审核题源 1 道')).not.toBeInTheDocument()
  })

  it('does not flash the previous result when revisiting a same-child goal before its new read completes', async () => {
    const original = roadmap(); delete original.section_evidence
    vi.mocked(requestParentLocal).mockResolvedValueOnce(roadmap())
    const { rerender } = render(<GoalRoadmapPanel childId="child-a" roadmap={original} />)
    expect(await screen.findByText('家长审核题源 1 道')).toBeInTheDocument()
    vi.mocked(requestParentLocal).mockImplementation(() => new Promise(() => {}))
    rerender(<GoalRoadmapPanel childId="child-a" roadmap={{ ...original, program_id: 'second-goal' }} />)
    rerender(<GoalRoadmapPanel childId="child-a" roadmap={original} />)
    expect(screen.queryByText('家长审核题源 1 道')).not.toBeInTheDocument()
    expect(requestParentLocal).toHaveBeenCalledTimes(3)
  })

  it('leaves official applicability unknown when a future or unsupported target has no sections', () => {
    const unavailable = evidence(); unavailable.sections = []
    unavailable.status = 'rule_applicability_unverified'
    render(<GoalRoadmapPanel childId="child-a" roadmap={roadmap(unavailable)} />)
    expect(screen.getByText('考试规则适用范围尚未核实，暂不能按分项列出练习证据。')).toBeInTheDocument()
    expect(screen.queryByText('独立正确')).not.toBeInTheDocument()
  })

  it.each(['source', 'rules'])('rejects a late same-child same-goal response whose %s snapshot changed', async (change) => {
    let resolve!: (value: GoalRoadmap) => void
    vi.mocked(requestParentLocal).mockImplementationOnce(() => new Promise<GoalRoadmap>((done) => { resolve = done }))
    const original = roadmap(); delete original.section_evidence
    original.exam_rules = { version: 'official_goal_rules_v1', binding_status: 'current', public_rule_status: 'verified_reference_snapshot',
      current_snapshot: { snapshot_hash: 'private-rules', reference: { authority: 'Cambridge', url: 'https://www.cambridgeenglish.org/', verified_on: '2026-09-09' } },
      required_sections: [], target_year: 2026, reference_year: 2026, registration_status: 'not_verified', coverage_status: 'not_audited',
      exam_readiness_claim_allowed: false } as unknown as GoalRoadmap['exam_rules']
    render(<GoalRoadmapPanel childId="child-a" roadmap={original} />)
    const result = roadmap()
    if (change === 'source') result.source_version = 'changed-source'
    else {
      const changed = evidence(); changed.rule_snapshot_hash = 'changed-rules'
      Object.assign(result, { section_evidence: changed })
    }
    await act(async () => { resolve(result) })
    expect(screen.queryByText('家长审核题源 1 道')).not.toBeInTheDocument()
    expect(screen.getByText('路线已更新，需重新读取完整路线。')).toBeInTheDocument()
  })

  it('shows only loading, not an unverified error, while the first read or a retry is pending', async () => {
    let reject!: (error: Error) => void
    vi.mocked(requestParentLocal).mockImplementationOnce(() => new Promise((_resolve, fail) => { reject = fail }))
    const original = roadmap(); delete original.section_evidence
    render(<GoalRoadmapPanel childId="child-a" roadmap={original} />)
    expect(screen.getByRole('status')).toHaveTextContent('正在读取当前练习证据')
    expect(screen.queryByText('练习证据尚未核实，请重新读取路线。')).not.toBeInTheDocument()
    await act(async () => { reject(new Error('offline')) })
    expect(screen.getByText('练习证据尚未核实，请重新读取路线。')).toBeInTheDocument()
    vi.mocked(requestParentLocal).mockImplementationOnce(() => new Promise(() => {}))
    fireEvent.click(screen.getByRole('button', { name: '刷新练习证据' }))
    expect(screen.getByRole('status')).toHaveTextContent('正在读取当前练习证据')
    expect(screen.queryByText('练习证据尚未核实，请重新读取路线。')).not.toBeInTheDocument()
  })
})
