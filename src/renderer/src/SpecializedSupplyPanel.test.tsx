import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { localApi } from './api'
import * as api from './api'
import { SpecializedSupplyPanel } from './ParentCenter'
import type { SpecializedSupplyCandidate } from './types'


describe('SpecializedSupplyPanel', () => {
  afterEach(() => { cleanup(); vi.restoreAllMocks(); localStorage.clear() })

  it.each([
    [{ task_points: ['说明邀请原因', '建议见面时间'] }, '说明邀请原因、建议见面时间'],
    [{ exact_answers: ['42'], local_rule: 'local_exact_v1' }, '42'],
    [{ keywords: ['原有关键词'] }, '原有关键词'],
    [{ task_points: null }, '答案依据暂不可读'],
  ])('keeps typed source candidates readable and routes to their exact review workflow: %j', async (answer, text) => {
    vi.spyOn(localApi, 'specializedRoadmaps').mockResolvedValue([])
    const candidate = { candidate_id: 'typed-candidate', child_id: 'child-a', track: 'pet',
      subject: '英语', stage: 'transfer', prompt: '给朋友写一封邀请信。', expected_answer: answer,
      explanation: '核对原题的任务要求。', difficulty: 2, validation: { passed: true, errors: [] },
      review_workflow: 'curriculum_review' } as unknown as SpecializedSupplyCandidate
    vi.spyOn(localApi, 'specializedSupplyCandidates').mockResolvedValue([candidate])
    const legacyReview = vi.spyOn(localApi, 'reviewSpecializedSupplyCandidate')
    const open = vi.fn().mockResolvedValue(undefined)
    render(<SpecializedSupplyPanel childId="child-a" onOpenCurriculum={open} />)
    expect(await screen.findByText(candidate.prompt)).toBeInTheDocument()
    expect(screen.getByText(text)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '批准' })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '查看资料审核' }))
    await waitFor(() => expect(open).toHaveBeenCalledWith('typed-candidate'))
    expect(legacyReview).not.toHaveBeenCalled()
  })

  it('shows domain depth and promotes reviewed content into the voice assessment catalog', async () => {
    vi.spyOn(localApi, 'specializedRoadmaps').mockResolvedValue([{
      child_id: 'child-a', track: 'competition_math', phase: 'baseline', reference_readiness: null,
      evidence_coverage: 0, evidence_domain_count: 0, total_domain_count: 10,
      domains: [], next_actions: [], generated_at: '2026-09-02T08:00:00Z',
      forecast: { status: 'insufficient_evidence', statement: '证据不足' },
    }])
    vi.spyOn(localApi, 'specializedSupplyGaps').mockResolvedValue({
      child_id: 'child-a', track: 'competition_math', target_per_domain: 3,
      ready_domain_count: 0, total_domain_count: 1,
      domains: [{ domain_id: 'competition_math.logic', subject: '数学', label: '逻辑推理', active_count: 1, pending_review_count: 1, target_count: 4, missing_count: 2, missing_stages: ['parallel', 'discrimination'], promoted_stages: [], ready: false, seed_available: true }],
    })
    const candidate = {
      candidate_id: 'candidate-specialized', job_id: 'job-specialized', child_id: 'child-a',
      track: 'competition_math' as const, subject: '数学', domain_id: 'competition_math.logic',
      seed_item_id: 'diag-logic', stage: 'transfer' as const,
      prompt: '在新的排队条件中判断三人的先后顺序，并说明两步理由。',
      expected_answer: { exact_answers: ['乙、甲、丙'], local_rule: 'local_exact_v1', answer_format: 'reviewed_exact_text_v1' }, explanation: '先固定唯一条件，再逐项排除。',
      review_workflow: 'model', difficulty: 3, validation: { passed: true, errors: [], ruleset: 'specialized_closed_exact_v1' },
      status: 'needs_review' as const, diagnostic_item_id: null, reviewed_at: null, review_note: null,
      created_at: '2026-09-02T08:00:00Z',
    }
    vi.spyOn(localApi, 'specializedSupplyCandidates').mockResolvedValueOnce([candidate]).mockResolvedValue([])
    vi.spyOn(localApi, 'examBlueprintReadiness').mockResolvedValue({
      child_id: 'child-a', track: 'competition_math', subject: '数学', ready: true,
      active_item_count: 10, required_item_count: 10, pending_review_count: 0,
      blueprint: {
        blueprint_id: 'family-competition-math-ten-domain-v1', title: '家庭数学思维十能力域测评',
        version: 'family-v1', effective_year: 2026, authority: 'family_defined',
        fidelity: 'family_defined_domain_coverage', score_boundary: '不对应竞赛官方分数。',
        source_urls: [], sections: [],
      },
      sections: [{ section_id: 'thinking', title: '数学思维', item_target: 10, active_count: 10, pending_review_count: 0, deficit: 0, pending_generation_count: 0, ready: true }],
    })
    const legacyReview = vi.spyOn(localApi, 'reviewSpecializedSupplyCandidate')
    const review = vi.spyOn(api, 'requestParentLocal').mockImplementation(async (_path, init) => {
      if (init?.method !== 'POST') return { schema: 'specialized_stage_review_v1', child_id: 'child-a', candidate_id: candidate.candidate_id,
        binding_version: 'source-version', local_only: true, cloud_transmission_allowed: false,
        qualification_scope: 'explicit_human_source_stage_review_not_child_mastery', source_prompt: '原题先判断甲乙的位置关系。',
        candidate_prompt: candidate.prompt, candidate_explanation: candidate.explanation, candidate_expected_answer: candidate.expected_answer,
        status: 'needs_review', allowed_stages: ['near_transfer', 'far_transfer'] }
      const body = JSON.parse(String(init.body))
      return { ...candidate, status: 'promoted', diagnostic_item_id: 'specialized-approved', review_action_id: body.client_action_id,
        review_note: body.note, validation: { ...candidate.validation, review_binding_version: body.expected_binding_version } }
    })

    render(<SpecializedSupplyPanel childId="child-a" />)
    expect(await screen.findByText(candidate.prompt)).toBeInTheDocument()
    expect(screen.getByText(/0\/1个能力域具备四类测量/)).toBeInTheDocument()
    expect(screen.getByText(/不发送孩子身份、学习历史或家庭数据/)).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('专项候选题1审核备注'), { target: { value: '已核对' } })
    fireEvent.click(screen.getByRole('button', { name: '核对当前原题' }))
    await waitFor(() => expect(screen.getByRole('button', { name: '批准' })).toBeEnabled())
    fireEvent.click(screen.getByRole('button', { name: '批准' }))

    await waitFor(() => expect(review.mock.calls.filter(([, init]) => init?.method === 'POST')).toHaveLength(1))
    expect(legacyReview).not.toHaveBeenCalled()
    expect(await screen.findByText(/发布记录不代表当前来源仍有效/)).toBeInTheDocument()
    expect(screen.queryByText(candidate.prompt)).not.toBeInTheDocument()
  })

  it('shows PET section deficits and starts the durable blueprint supply workflow', async () => {
    vi.spyOn(localApi, 'specializedRoadmaps').mockResolvedValue([{
      child_id: 'child-a', track: 'pet', phase: 'baseline', reference_readiness: null,
      evidence_coverage: 0, evidence_domain_count: 0, total_domain_count: 17,
      domains: [], next_actions: [], generated_at: '2026-09-02T08:00:00Z',
      forecast: { status: 'insufficient_evidence', statement: '证据不足' },
    }])
    vi.spyOn(localApi, 'specializedSupplyGaps').mockResolvedValue({
      child_id: 'child-a', track: 'pet', target_per_domain: 4,
      ready_domain_count: 0, total_domain_count: 0, domains: [],
    })
    vi.spyOn(localApi, 'specializedSupplyCandidates').mockResolvedValue([])
    const readiness = {
      child_id: 'child-a', track: 'pet' as const, subject: '英语', ready: false,
      active_item_count: 17, required_item_count: 63, pending_review_count: 0,
      blueprint: {
        blueprint_id: 'cambridge-b1-preliminary-for-schools-current-v2',
        title: 'Cambridge B1 Preliminary for Schools 当前卷型', version: 'current-v2',
        effective_year: 2026, authority: 'Cambridge University Press & Assessment',
        fidelity: 'strict_structure_reference', score_boundary: '结果仅为能力参考百分比。',
        source_urls: ['https://www.cambridgeenglish.org/'], sections: [],
      },
      sections: [
        { section_id: 'reading', title: 'Reading', item_target: 32, active_count: 7, pending_review_count: 0, deficit: 25, pending_generation_count: 25, ready: false },
        { section_id: 'listening', title: 'Listening', item_target: 25, active_count: 5, pending_review_count: 0, deficit: 20, pending_generation_count: 20, ready: false },
      ],
    }
    vi.spyOn(localApi, 'examBlueprintReadiness').mockResolvedValue(readiness)
    const prepare = vi.spyOn(localApi, 'prepareExamBlueprint').mockResolvedValue({
      created_count: 5,
      skipped: Array.from({ length: 4 }, (_, index) => ({
        domain_id: `pet.listening.blocked_${index}`,
        reason: 'SPECIALIZED_TYPED_CLOSED_SEED_REQUIRED',
      })),
      readiness,
    })

    render(<SpecializedSupplyPanel childId="child-a" />)
    expect(await screen.findByText(/17\/63道审核题/)).toBeInTheDocument()
    expect(screen.getByText(/Reading 7\/32/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '按蓝图补齐题库' }))

    await waitFor(() => expect(prepare).toHaveBeenCalledWith(
      'child-a', 'pet', '英语', 'cambridge-b1-preliminary-for-schools-current-v2',
    ))
    expect(await screen.findByText(/创建5个补题任务.*另有4个能力域/)).toBeInTheDocument()
  })
})
