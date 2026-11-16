import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { webcrypto } from 'node:crypto'
import { ApiError, localApi } from './api'
import { operationIdentity, readPrivacyIntent, type PrivacyOperationIdentity } from './privacyOperationRecovery'
import { BehaviorEvidencePanel, ChildGrowthModelPanel, DesktopSettingsPanel, DeviceSetupPanel, LearningOverview, mergeQualificationResult, MistakeWeaknessPanel, ModelOperationsPanel, ModelSettingsPanel, OperationsHealthPanel, ParentCenter, ParentWorkNavigator, PeriodReportsPanel, PrivacyOperations, ReportDetail, SafetyAlertsPanel, SensorReleaseVerificationPanel, StrategyEvolutionPanel, VariantCandidateReviewPanel, WeeklyGoalsPanel } from './ParentCenter'
import { qualificationEvidenceLabel } from './deviceQualification'
import * as mistakePrintApi from './mistakeBookPrintApi'
import { changeParentDraftAuthority } from './parentDraftAuthority'
import type { ChildProfile, GoalLearningOutcome, GoalProgramCycle, LongitudinalChildModel, ParentDashboard, WeeklyGoal } from './types'
import type { BackupManifest, BootstrapState, ChildSafetyEvent, DailyReport, DeviceBinding, DeviceQualificationSummary, LearningState, MasteryHistoryItem, ModelCallAudit, PeriodReport, PrivacyPreferences, SensorReleaseReview, StrategyExperiment, SystemHealth } from './types'

const unconfiguredModel: BootstrapState['model'] = {
  provider: 'qwen', model: 'qwen3.7-plus', voice_intent_model: 'qwen3.8-flash', configured: false,
  fallback_provider: null, fallback_model: null, fallback_configured: false
}

describe('ParentCenter local printing', () => {
  afterEach(() => { cleanup();vi.restoreAllMocks();vi.unstubAllGlobals() })

  it('opens only the selected child’s print view after PIN unlock and closes it on lock', async () => {
    const children: ChildProfile[] = [
      { child_id: 'print-child-a', nickname: '甲同学', grade: '四年级', curriculum_region: '', status: 'active' },
      { child_id: 'print-child-b', nickname: '乙同学', grade: '初三', curriculum_region: '', status: 'active' },
    ]
    const dashboard: ParentDashboard = {
      today: { generated_at: '', items: [] }, children: [], device_bindings: [],
      device_qualification: { bindings_ready: false, profiles_ready: false, profile_missing: { camera: [], document_camera: [] },
        acceptance_status: 'pending', required: [], recent_runs: [] },
      privacy: { cloud_model_text_allowed: false, cloud_audio_allowed: false, raw_media_storage_allowed: false,
        model_improvement_allowed: false, model_call_retention_days: 30, tutor_turn_retention_days: 30, report_retention_days: 30, updated_at: '' },
      health: { database_integrity: 'ok', database_size_bytes: 4096, disk_free_bytes: 100000,
        pending_report_jobs: 0, pending_agent_turns: 0, pending_deletion_jobs: 0, model_failures_last_7_days: 0, active_sensor_leases: 0,
        last_backup: null, maintenance: { daily_backup: null, retention_cleanup: null }, recent_maintenance_failures: [],
        raw_media_files_stored: 0, devices: [], device_incidents: [], recent_model_failures: [], recent_report_failures: [],
        recent_agent_failures: [], recent_deletion_failures: [] },
      sensor_release_reviews: [], sensor_release_platform_eligible: false, safety_events: [],
    }
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(null, { status: 503 })))
    vi.spyOn(localApi, 'parentStatus').mockResolvedValue({ configured: true, locked_seconds: 0 })
    vi.spyOn(localApi, 'parentUnlock').mockResolvedValue(undefined)
    vi.spyOn(localApi, 'parentDashboard').mockResolvedValue(dashboard)
    const snapshot = vi.spyOn(mistakePrintApi, 'loadMistakePrintSnapshot').mockImplementation(async childId => ({
      version: 1, child_id: childId, total: 0, generated_at: '2026-11-14T01:00:00Z', items: [],
    }))

    render(<ParentCenter children={children} model={unconfiguredModel} onClose={() => undefined} />)
    const pin = await screen.findByLabelText('家长 PIN')
    expect(screen.queryByRole('button', { name: '打印错题本' })).not.toBeInTheDocument()
    fireEvent.change(pin, { target: { value: '1111' } })
    fireEvent.click(screen.getByRole('button', { name: '解锁' }))
    const chooser = await screen.findByRole('group', { name: '选择孩子' })
    fireEvent.click(within(chooser).getByRole('button', { name: /乙同学/ }))
    fireEvent.click(screen.getByRole('button', { name: '打印错题本' }))
    expect(await screen.findByText('乙同学 · 初三 · A4 纸张')).toBeInTheDocument()
    await screen.findByText('匹配 0 题 · 已选 0 题')
    expect(snapshot).toHaveBeenCalledExactlyOnceWith('print-child-b', expect.any(AbortSignal))

    fireEvent.click(screen.getByRole('button', { name: '返回家长中心' }))
    expect(screen.getByRole('button', { name: /乙同学/ })).toHaveAttribute('aria-pressed', 'true')
    fireEvent.click(screen.getByRole('button', { name: '打印错题本' }))
    await screen.findByText('匹配 0 题 · 已选 0 题')
    act(() => changeParentDraftAuthority('locked'))
    expect(await screen.findByLabelText('家长 PIN')).toBeInTheDocument()
    expect(screen.queryByRole('region', { name: '错题本打印' })).not.toBeInTheDocument()
    expect(snapshot.mock.calls.map(args => args[0])).toEqual(['print-child-b', 'print-child-b'])
  })
})

describe('ModelSettingsPanel', () => {
  afterEach(() => {
    cleanup()
    window.desktop = undefined
    vi.restoreAllMocks()
  })

  it('restores the configured endpoint and task-specific models', () => {
    window.desktop = { saveModelSettings: vi.fn() } as unknown as NonNullable<typeof window.desktop>
    render(<ModelSettingsPanel initial={{
      provider: 'qwen', configured: true,
      model: 'qwen3.7-plus', voice_intent_model: 'qwen3.8-flash',
      base_url: 'https://model.example/compatible-mode/v1',
      fallback_provider: null, fallback_model: null, fallback_base_url: null, fallback_configured: false,
    }} />)

    expect(screen.getByLabelText('学习辅导模型')).toHaveValue('qwen3.7-plus')
    expect(screen.getByLabelText('实时意图模型')).toHaveValue('qwen3.8-flash')
    expect(screen.getByLabelText('API地址')).toHaveValue('https://model.example/compatible-mode/v1')
    expect(screen.getByRole('button', { name: '保存并启用' })).toBeEnabled()
  })

  it('starts new Qwen configuration with the models that passed the fixed gates', () => {
    render(<ModelSettingsPanel initial={unconfiguredModel} />)

    expect(screen.getByLabelText('学习辅导模型')).toHaveValue('qwen3.7-plus')
    expect(screen.getByLabelText('实时意图模型')).toHaveValue('qwen3.8-flash')
  })

  it('allows a distinct Qwen paygo endpoint as fallback', async () => {
    const saveModelSettings = vi.fn().mockResolvedValue({ configured: true })
    window.desktop = { saveModelSettings } as unknown as NonNullable<typeof window.desktop>
    render(<ModelSettingsPanel initial={unconfiguredModel} />)

    fireEvent.change(screen.getByLabelText('API Key'), { target: { value: 'token-plan-secret' } })
    fireEvent.change(screen.getByLabelText('API地址'), { target: { value: 'https://token-plan.example/compatible-mode/v1' } })
    fireEvent.click(screen.getByLabelText('启用备用模型端点'))
    fireEvent.change(screen.getByLabelText('备用服务商'), { target: { value: 'qwen' } })
    fireEvent.change(screen.getByLabelText('备用API地址'), { target: { value: 'https://dashscope.aliyuncs.com/compatible-mode/v1' } })
    fireEvent.change(screen.getByLabelText('备用API Key'), { target: { value: 'paygo-secret' } })
    fireEvent.click(screen.getByRole('button', { name: '保存并启用' }))

    await waitFor(() => expect(saveModelSettings).toHaveBeenCalledWith(expect.objectContaining({
      provider: 'qwen',
      fallback: expect.objectContaining({ provider: 'qwen', baseUrl: 'https://dashscope.aliyuncs.com/compatible-mode/v1' }),
    })))
  })

  it('requires a second action before removing persisted model credentials', async () => {
    const clearModelSettings = vi.fn().mockResolvedValue({ configured: false })
    const onRuntimeRestart = vi.fn()
    window.desktop = { clearModelSettings } as unknown as NonNullable<typeof window.desktop>
    render(<ModelSettingsPanel onRuntimeRestart={onRuntimeRestart} initial={{
      provider: 'qwen', configured: true,
      model: 'qwen3.7-plus', voice_intent_model: 'qwen3.8-flash',
      base_url: 'https://model.example/compatible-mode/v1',
      fallback_provider: null, fallback_model: null, fallback_base_url: null, fallback_configured: false,
    }} />)

    fireEvent.click(screen.getByRole('button', { name: '停用云模型' }))
    expect(clearModelSettings).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: '确认停用并删除密钥' }))

    expect(await screen.findByText(/首选与备用模型密钥已经删除/)).toBeInTheDocument()
    expect(clearModelSettings).toHaveBeenCalledTimes(1)
    expect(onRuntimeRestart).toHaveBeenCalledTimes(1)
    expect(screen.getByText('配置云端模型')).toBeInTheDocument()
  })
})

describe('ParentCenter PIN lockout', () => {
  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
  })

  it('shows the persisted countdown and disables PIN attempts while locked', async () => {
    vi.spyOn(localApi, 'parentStatus').mockResolvedValue({ configured: true, locked_seconds: 62 })

    render(<ParentCenter children={[]} model={unconfiguredModel} onClose={() => undefined} />)

    expect(await screen.findByText(/01:02 后可再试/)).toBeInTheDocument()
    expect(screen.getByLabelText('家长 PIN')).toBeDisabled()
    expect(screen.getByRole('button', { name: '暂时锁定' })).toBeDisabled()
    expect(screen.getByText(/应用重启不会提前解除/)).toBeInTheDocument()
  })

  it('refreshes the authoritative lock duration after the fifth invalid attempt', async () => {
    const status = vi.spyOn(localApi, 'parentStatus')
      .mockResolvedValueOnce({ configured: true, locked_seconds: 0 })
      .mockResolvedValueOnce({ configured: true, locked_seconds: 300 })
    vi.spyOn(localApi, 'parentUnlock').mockRejectedValue(
      new ApiError('尝试次数过多，请约5分钟后再试。', 429, 'PARENT_LOCKED', false, undefined, { locked_seconds: 300 })
    )

    render(<ParentCenter children={[]} model={unconfiguredModel} onClose={() => undefined} />)
    const input = await screen.findByLabelText('家长 PIN')
    fireEvent.change(input, { target: { value: '1111' } })
    fireEvent.click(screen.getByRole('button', { name: '解锁' }))

    expect(await screen.findByText(/05:00 后可再试/)).toBeInTheDocument()
    expect(status).toHaveBeenCalledTimes(2)
    expect(screen.getByLabelText('家长 PIN')).toBeDisabled()
  })
})

describe('VariantCandidateReviewPanel', () => {
  afterEach(() => { cleanup(); vi.restoreAllMocks() })

  it('shows answer evidence and promotes a candidate only after a parent approves it', async () => {
    const candidate = {
      candidate_id: 'candidate-1', job_id: 'job-1', mistake_id: 'mistake-1', child_id: 'child-a',
      knowledge_node_id: 'node-1', stage: 'near_transfer',
      prompt: '方程 x²+6x+9=0 的判别式是多少？', answer_type: 'text' as const,
      expected_answer: { keywords: ['0', '两个相等实数根'] }, explanation: '判别式36-36=0。',
      difficulty: 2, validation: { passed: true, errors: [], ruleset: 'variant_local_gate_v1' },
      status: 'needs_review' as const, diagnostic_item_id: null, reviewed_at: null, review_note: null,
      created_at: '2026-09-01T10:00:00Z',
    }
    vi.spyOn(localApi, 'variantCandidates').mockResolvedValue([candidate])
    const review = vi.spyOn(localApi, 'reviewVariantCandidate').mockResolvedValue({
      ...candidate, status: 'promoted', diagnostic_item_id: 'diagnostic-1', reviewed_at: '2026-09-01T10:01:00Z',
    })

    render(<VariantCandidateReviewPanel childId="child-a" />)
    expect(await screen.findByText(candidate.prompt)).toBeInTheDocument()
    expect(screen.getByText(/0、两个相等实数根/)).toBeInTheDocument()
    expect(screen.getByText(/判别式36-36=0/)).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('候选题1审核备注'), { target: { value: '题意和答案已核对' } })
    fireEvent.click(screen.getByRole('button', { name: '批准进入复测' }))

    await waitFor(() => expect(review).toHaveBeenCalledWith('candidate-1', 'approve', '题意和答案已核对'))
    expect(await screen.findByText(/开放答案评价不计入可靠掌握/)).toBeInTheDocument()
    expect(screen.queryByText(candidate.prompt)).not.toBeInTheDocument()
  })

  it('shows the paper source and disables approval when its evidence was withdrawn', async () => {
    vi.spyOn(localApi, 'variantCandidates').mockResolvedValue([{
      candidate_id: 'paper-candidate', job_id: 'paper-job', mistake_id: 'paper-mistake', child_id: 'child-a',
      knowledge_node_id: 'node-1', stage: 'near_transfer', prompt: '计算18与7的和乘以2。', answer_type: 'text',
      expected_answer: { keywords: ['50'], exact_answers: ['50'], local_rule: 'local_exact_v1' },
      source_context: { prompt: '计算(37+13)乘以4。', kind: 'document_camera', valid: false, cloud_transmission_allowed: false },
      explanation: '先算加法再算乘法。', difficulty: 1, validation: { passed: true, errors: [], ruleset: 'variant_local_gate_v1' },
      status: 'needs_review', diagnostic_item_id: null, reviewed_at: null, review_note: null, created_at: '2026-09-06',
    }])
    render(<VariantCandidateReviewPanel childId="child-a" />)
    expect(await screen.findByText(/计算\(37\+13\)乘以4/)).toBeInTheDocument()
    expect(screen.getByText('可接受的完整答案：')).toBeInTheDocument()
    expect(screen.getByRole('alert')).toHaveTextContent('来源已修改或撤回')
    expect(screen.getByRole('button', { name: '批准进入复测' })).toBeDisabled()
    expect(screen.getByRole('button', { name: '拒绝这道题' })).toBeEnabled()
  })
})

describe('ChildGrowthModelPanel', () => {
  afterEach(() => { cleanup(); vi.restoreAllMocks() })

  it.each([undefined, 'companion_balance_v2'])('renders evidence maturity and separates activity receipts (%s)', async (version) => {
    vi.spyOn(localApi, 'childModelCorrections').mockResolvedValue([])
    vi.spyOn(localApi, 'longitudinalChildModel').mockResolvedValue({
      snapshot_id: 'snapshot-1', child_id: 'child-a', model_version: 'evidence_child_model_v1', evidence_fingerprint: 'sha256', is_current: true, created_at: '2026-09-01T08:00:00Z',
      payload: {
        as_of: '2026-09-01T08:00:00Z', evidence_level: 'emerging',
        learning: { evidenced_node_count: 8, subjects: [{ subject: '数学', node_count: 5, weak_nodes: 2, stable_nodes: 1, due_reviews: 1, average_mastery: .61, average_hint_dependency: .3, average_confidence: .7 }] },
        mistakes: { status_counts: { remediating: 1 }, top_clusters: [] },
        execution: { window_days: 14, sessions: 3, planned_tasks: 6, completed_tasks: 4, deferred_tasks: 1, actual_minutes: 90, completion_rate: .667 },
        behavior: { observed_days: 0, signals: {}, uncertainty: '没有可用观察。' }, tutor_feedback: { total: 2, understood: 1, still_stuck: 1 },
        support_strategy: { explanation_pacing: 'short_step_then_restate', task_segment_minutes: 20, mistake_priority: 'due_or_relapsed_first', require_independent_check: true, reason: '根据证据选择。' },
        companion_balance: { version, window_days: 14, activity_sessions: 1, video_sessions: 1, game_sessions: 1,
          activity_minutes: 2, video_minutes: 3, game_minutes: 5, total_minutes: 10, total_sessions: 3,
          average_daily_minutes: 1, child_ended_sessions: 1, bounded_end_sessions: 1, return_to_learning_sessions: 99,
          self_report_sessions: 2, task_resume_receipt_sessions: 1, verified_child_return_sessions: 0,
          boundary_status: 'return_evidence_missing', guidance: version ? '还没有核验实际回到学习。' : '旧版本不可靠的返回结论' },
        limitations: ['没有记录不等于不会。', '不做人格或能力标签。'],
      },
    })
    vi.spyOn(localApi, 'goalProgramCycles').mockResolvedValue({ items: [{
      cycle_id: 'cycle-1', program_id: 'program-1', child_id: 'child-a', track: 'pet_path', title: 'PET阶段能力', target_statement: '六年级上学期前通过PET。', target_horizon: {}, deadline: null, source_type: 'pet_daily', week_start: '2026-08-31', week_end: '2026-09-06', target_minutes: 100, target_sessions: 5, completed_minutes: 40, completed_sessions: 2, remaining_minutes: 60, remaining_sessions: 3, status: 'on_track', focus: { subject: '英语', title: '阅读理解' }, evidence_watermark: { evidence_items: 2, latest: '2026-09-01T08:00:00Z' },
    }], next_daily_action: null })
    vi.spyOn(localApi, 'specializedRoadmaps').mockResolvedValue([{
      child_id: 'child-a', track: 'pet', phase: 'baseline', reference_readiness: null,
      evidence_coverage: .24, evidence_domain_count: 4, total_domain_count: 17,
      forecast: { status: 'insufficient_evidence', statement: '证据覆盖不足，先完成基线诊断，不预测考试结果或获奖结果。' },
      domains: [{ domain_id: 'pet.listening.gist', subject: '英语', label: '听力整体理解', reference_score: 0,
        reference_score_basis: 'qualified_independent_observations', status: 'weak', evidence_type: 'qualified_independent_observations',
        assessment_at: '2026-08-31T08:00:00Z', learning_evidence_count: 2, assessment_reference_score: 100,
        evidence_conflict: true, priority_status: 'recheck_needed', priority_reason: '当前有效作答仍需核对；旧参考分不覆盖该证据。' }], generated_at: '2026-09-01T08:00:00Z',
      whole_exam_analysis: { latest_exam: version ? { assessment_type: 'qualified_exam_assessment',
        subject: '英语', occurred_at: '2026-09-01T08:00:00Z', score: 1, max_score: 2, percentage: 50,
        score_scope: 'current_exam_reference_only', mastery_claim_allowed: false } : null,
        weak_domains: [], developing_domains: [], interpretation: '当前参考分仍需与独立复测分别核对。', exam_reference_weak_domains: [{ domain_id: 'pet.listening.gist', reference_score: 50 }],
        latest_exam_review: { score_status: version ? 'qualified' : 'needs_review', verified_items: 1,
          answered_items: 2, total_items: 2, current_evidence_status: 'current' } },
      next_actions: [{ priority: 1, domain_id: 'pet.listening.gist', subject: '英语', title: '基线诊断 · 听力整体理解', minutes: 12, action_type: 'baseline', completion_standard: '保存证据' }],
    }])

    render(<ChildGrowthModelPanel childId="child-a" />)
    expect(await screen.findByText(/历史记录正在积累/)).toBeInTheDocument()
    expect(screen.getByText('PET阶段能力')).toBeInTheDocument()
    expect(screen.getByText(/40\/100 分钟/)).toBeInTheDocument()
    expect(screen.getByText(/数学/)).toBeInTheDocument()
    expect(screen.getByText(/不做人格或能力标签/)).toBeInTheDocument()
    expect(screen.getByText(/4\/17 个能力域/)).toBeInTheDocument()
    expect(screen.queryByText(/99 次留下返回/)).not.toBeInTheDocument()
    if (version) {
      expect(screen.getByText(/原任务恢复计时回执 1 次；实际返回学习核验 0 次/)).toBeInTheDocument()
    } else {
      expect(screen.getByText(/历史统计尚未按实际返回证据复核/)).toBeInTheDocument()
      expect(screen.queryByText('旧版本不可靠的返回结论')).not.toBeInTheDocument()
    }
    expect(screen.getByText(/基线诊断 · 听力整体理解/)).toBeInTheDocument()
    fireEvent.click(screen.getByText('需要重新核对的依据'))
    expect(screen.getByText(/历史参考 100 分/)).toHaveTextContent('当前合格作答参考 0 分')
    expect(screen.getByText(/参考域覆盖/)).toBeInTheDocument()
    if (version) {
      expect(screen.getByText('最近整卷训练参考：').parentElement).toHaveTextContent('1/2（50%）；薄弱能力域 1 个')
    } else {
      expect(screen.getByText(/最近整卷原回答已保存/)).toHaveTextContent('2/2 题，1 题有当前有效评分')
      expect(screen.queryByText('最近整卷训练参考：')).not.toBeInTheDocument()
    }
  })

  it('persists and revokes a bounded parent strategy correction without editing evidence', async () => {
    const model = {
      snapshot_id: 'snapshot-2', child_id: 'child-a', model_version: 'evidence_child_model_v2', evidence_fingerprint: 'sha256-2', is_current: true, created_at: '2026-09-02T08:00:00Z',
      payload: {
        as_of: '2026-09-02T08:00:00Z', evidence_level: 'cold_start' as const,
        learning: { evidenced_node_count: 0, subjects: [] },
        mistakes: { status_counts: {}, top_clusters: [] },
        execution: { window_days: 14, sessions: 0, planned_tasks: 0, completed_tasks: 0, deferred_tasks: 0, actual_minutes: 0, completion_rate: null },
        behavior: { observed_days: 0, signals: {} }, tutor_feedback: { total: 0, understood: 0, still_stuck: 0 },
        support_strategy: { explanation_pacing: 'standard_layered', task_segment_minutes: 20, interaction_tone: 'gentle', mistake_priority: 'due_or_relapsed_first', require_independent_check: true, reason: '按当前证据。' },
        limitations: ['不改写学习证据。'],
      },
    }
    const correction = {
      correction_id: 'correction-1', child_id: 'child-a', target_key: 'task_segment_minutes' as const,
      corrected_value: 15, reason: '长时段后容易遗漏步骤。', status: 'active' as const,
      expires_at: '2026-10-02T08:00:00Z', created_at: '2026-09-02T08:00:00Z',
      revoked_at: null, revoked_reason: null, client_action_id: 'action-1',
    }
    vi.spyOn(localApi, 'longitudinalChildModel').mockResolvedValue(model)
    vi.spyOn(localApi, 'goalProgramCycles').mockResolvedValue({ items: [], next_daily_action: null })
    vi.spyOn(localApi, 'specializedRoadmaps').mockResolvedValue([])
    const list = vi.spyOn(localApi, 'childModelCorrections').mockResolvedValueOnce([]).mockResolvedValueOnce([correction]).mockResolvedValueOnce([{ ...correction, status: 'revoked', revoked_at: '2026-09-03T08:00:00Z', revoked_reason: '家长撤销' }])
    const create = vi.spyOn(localApi, 'createChildModelCorrection').mockResolvedValue({
      correction,
      model: { ...model, payload: { ...model.payload, support_strategy: { ...model.payload.support_strategy, task_segment_minutes: 15, parent_correction_count: 1 }, parent_corrections: [correction] } },
    })
    const revoke = vi.spyOn(localApi, 'revokeChildModelCorrection').mockResolvedValue({ correction: { ...correction, status: 'revoked' }, model })

    render(<ChildGrowthModelPanel childId="child-a" />)
    await screen.findByText(/冷启动/)
    fireEvent.change(screen.getByLabelText('校正项目'), { target: { value: 'task_segment_minutes' } })
    fireEvent.change(screen.getByLabelText('校正值'), { target: { value: '15' } })
    fireEvent.change(screen.getByLabelText('校正依据'), { target: { value: '长时段后容易遗漏步骤。' } })
    fireEvent.click(screen.getByRole('button', { name: '保存并应用' }))

    await waitFor(() => expect(create).toHaveBeenCalledWith('child-a', expect.objectContaining({ target_key: 'task_segment_minutes', value: 15, reason: '长时段后容易遗漏步骤。' })))
    expect(await screen.findByText(/15 分钟（生效中）/)).toBeInTheDocument()
    expect(screen.getByText(/学习证据和掌握度未被改动/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '撤销' }))
    await waitFor(() => expect(revoke).toHaveBeenCalledWith('correction-1', expect.stringContaining('恢复')))
    expect(await screen.findByText(/已撤销这条校正/)).toBeInTheDocument()
    expect(list).toHaveBeenCalledTimes(3)
  })
})

describe('ParentWorkNavigator', () => {
  afterEach(() => { cleanup(); vi.restoreAllMocks() })

  it('aggregates child-specific review work and navigates to the selected work surface', async () => {
    vi.spyOn(localApi, 'ocrReviewQueue').mockResolvedValue([{ child_id: 'child-a' } as never, { child_id: 'child-a' } as never])
    vi.spyOn(localApi, 'variantCandidates').mockResolvedValue([{ child_id: 'child-a' } as never])
    vi.spyOn(localApi, 'specializedSupplyCandidates').mockResolvedValue([])
    const scrollIntoView = vi.fn()
    const target = document.createElement('div')
    target.id = 'parent-work-ocr'
    target.scrollIntoView = scrollIntoView
    document.body.appendChild(target)

    render(<ParentWorkNavigator childId="child-a" safetyEvents={[{
      safety_event_id: 'safety-1', child_id: 'child-a', child_nickname: '小学生', risk_level: 'R2', category: 'fear_or_bullying', status: 'pending',
      guidance: { summary: '需要家长确认', script: [], action: { what: '当面确认', duration_minutes: 10, steps: [], stop_condition: '安全确认' }, avoid: [] },
      created_at: '2026-09-02T08:00:00Z', acknowledged_at: null,
    }]} />)

    expect(await screen.findByRole('button', { name: /2OCR 校对/ })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /1安全提醒/ })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /1错题变式审核/ })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /2OCR 校对/ }))
    expect(scrollIntoView).toHaveBeenCalledWith({ behavior: 'smooth', block: 'start' })
    target.remove()
  })
})

describe('BehaviorEvidencePanel', () => {
  afterEach(() => { cleanup(); vi.restoreAllMocks() })

  it('explains a pending automatic stop and its retry without claiming it finished', async () => {
    vi.spyOn(localApi, 'behaviorReplay').mockResolvedValue({ child_id: 'child-a', session_id: null, events: [], states: [], interventions: [] })
    vi.spyOn(localApi, 'behaviorBaseline').mockResolvedValue({ status: 'insufficient_history', days_observed: 1, minimum_days: 3 })
    vi.spyOn(localApi, 'behaviorPersonalization').mockResolvedValue({ minimum_feedback_samples: 4, boundary: '只会降低提醒频率。', items: [] })
    vi.spyOn(localApi, 'behaviorHabitExperiments').mockResolvedValue({ child_id: 'child-a', boundary: '本地实验', items: [{
      experiment_id: 'goal', child_id: 'child-a', target_state: 'blocked_candidate', title: '休息提醒',
      child_action: '自行暂停', success_signal: '自愿反馈', duration_days: 7, baseline: {}, status: 'active',
      stop_pending: true, stop_retrying: true, proposed_at: '2026-11-01T00:00:00Z', started_at: '2026-11-01T00:00:00Z',
      ends_at: '2026-11-08T00:00:00Z', closed_at: null, outcome: null
    }] })
    render(<BehaviorEvidencePanel childId="child-a" />)
    fireEvent.click(screen.getByRole('button', { name: '查看证据' }))
    expect(await screen.findByText('提醒已暂停；停止暂未完成，系统会重试')).toBeInTheDocument()
    expect(screen.queryByText('已停止')).not.toBeInTheDocument()
  })

  it('shows a stale reminder is paused while withdrawal is pending', async () => {
    vi.spyOn(localApi, 'behaviorReplay').mockResolvedValue({child_id:'child-a',session_id:null,events:[],states:[],interventions:[{
      intervention_id:'stale-one',decided_at:'2026-08-28T10:00:00Z',candidate_state:'blocked_candidate',
      message:'旧提醒',feedback:null,outcome:null,status:'active',withdrawal_pending:true
    }]})
    vi.spyOn(localApi, 'behaviorBaseline').mockResolvedValue({status:'insufficient_history',days_observed:1,minimum_days:3})
    vi.spyOn(localApi, 'behaviorPersonalization').mockResolvedValue({minimum_feedback_samples:4,boundary:'本地',items:[]})
    render(<BehaviorEvidencePanel childId="child-a" />)
    fireEvent.click(screen.getByRole('button', {name:'查看证据'}))
    expect(await screen.findByText(/提醒已暂停，正在完成撤回/)).toBeInTheDocument()
    expect(screen.queryByText(/等待孩子反馈/)).not.toBeInTheDocument()
  })

  it('shows feedback has been saved while reminder withdrawal is pending', async () => {
    vi.spyOn(localApi, 'behaviorReplay').mockResolvedValue({child_id:'child-a',session_id:null,events:[],states:[],interventions:[{
      intervention_id:'stale-feedback',decided_at:'2026-08-28T10:00:00Z',candidate_state:'blocked_candidate',
      message:'旧提醒',feedback:'accepted',outcome:null,status:'active',withdrawal_pending:true
    }]})
    vi.spyOn(localApi, 'behaviorBaseline').mockResolvedValue({status:'insufficient_history',days_observed:1,minimum_days:3})
    vi.spyOn(localApi, 'behaviorPersonalization').mockResolvedValue({minimum_feedback_samples:4,boundary:'本地',items:[]})
    render(<BehaviorEvidencePanel childId="child-a" />)
    fireEvent.click(screen.getByRole('button', {name:'查看证据'}))
    expect(await screen.findByText(/已记录孩子反馈，提醒正在撤回/)).toBeInTheDocument()
  })

  it('explains that a reminder was withdrawn because its source changed', async () => {
    vi.spyOn(localApi, 'behaviorReplay').mockResolvedValue({child_id:'child-a',session_id:null,events:[],states:[],interventions:[{
      intervention_id:'stale-one',decided_at:'2026-08-28T10:00:00Z',candidate_state:'blocked_candidate',
      message:'旧提醒',feedback:null,outcome:null,status:'withdrawn',withdrawal_reason:'habit_support_source_changed'
    }]})
    vi.spyOn(localApi, 'behaviorBaseline').mockResolvedValue({status:'insufficient_history',days_observed:1,minimum_days:3})
    vi.spyOn(localApi, 'behaviorPersonalization').mockResolvedValue({minimum_feedback_samples:4,boundary:'本地',items:[]})
    render(<BehaviorEvidencePanel childId="child-a" />)
    fireEvent.click(screen.getByRole('button', {name:'查看证据'}))
    expect(await screen.findByText(/提醒依据已变化，已撤回/)).toBeInTheDocument()
  })

  it('loads parent-authorized local summaries only when the parent asks to view them', async () => {
    const replay = vi.spyOn(localApi, 'behaviorReplay').mockResolvedValue({
      child_id: 'child-a', session_id: null, events: [],
      states: [{ state_id: 'state-1', event_id: 'event-1', occurred_at: '2026-08-28T10:00:00Z', state: 'blocked_candidate', probability: 0.68, effective_probability: .64, alternatives: ['可能在心算；请与孩子确认。'], evidence: { event_type: 'work.stall', source: 'local_vision', missing_sources: [] } }],
      interventions: [{ intervention_id: 'i-1', decided_at: '2026-08-28T10:02:00Z', candidate_state: 'blocked_candidate', message: '要不要把卡住的一步圈出来？', feedback: 'declined', outcome: { state: 'writing', probability: .7, observed_at: '2026-08-28T10:07:00Z' } }]
    })
    vi.spyOn(localApi, 'behaviorBaseline').mockResolvedValue({ status: 'insufficient_history', days_observed: 1, minimum_days: 3 })
    vi.spyOn(localApi, 'behaviorPersonalization').mockResolvedValue({ minimum_feedback_samples: 4, boundary: '只会降低提醒频率。', items: [] })

    render(<BehaviorEvidencePanel childId="child-a" />)
    expect(replay).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: '查看证据' }))

    expect(await screen.findByText('卡顿候选')).toBeInTheDocument()
    expect(screen.getByText('可能在心算；请与孩子确认。')).toBeInTheDocument()
    expect(screen.getByText(/孩子拒绝/)).toBeInTheDocument()
    expect(screen.getByText(/提示后的可观察结果：持续书写（70%）/)).toBeInTheDocument()
    expect(screen.getByText(/不证明提醒造成变化/)).toBeInTheDocument()
    expect(screen.getByText(/至少 3 天后才建立个人基线/)).toBeInTheDocument()
  })

  it('lets a parent retract a local observation and refreshes the readable evidence', async () => {
    const snapshot = {
      child_id: 'child-a', session_id: null,
      events: [{ event_id: 'event-1', occurred_at: '2026-08-28T10:00:00Z', event_type: 'work.stall', source: 'local_vision', confidence: .7, missing_sources: ['child_self_report'], alternative_explanations: ['可能在心算。'] }],
      states: [{ state_id: 'state-1', event_id: 'event-1', occurred_at: '2026-08-28T10:00:00Z', state: 'blocked_candidate', probability: .68, effective_probability: .63, alternatives: ['可能在心算。'], evidence: { event_type: 'work.stall', source: 'local_vision', missing_sources: ['child_self_report'] } }],
      interventions: []
    }
    vi.spyOn(localApi, 'behaviorReplay').mockResolvedValue(snapshot)
    vi.spyOn(localApi, 'behaviorBaseline').mockResolvedValue({ status: 'insufficient_history', days_observed: 1, minimum_days: 3 })
    vi.spyOn(localApi, 'behaviorPersonalization').mockResolvedValue({ minimum_feedback_samples: 4, boundary: '只会降低提醒频率。', items: [] })
    const retract = vi.spyOn(localApi, 'retractBehaviorEvidence').mockResolvedValue({ state_id: 'state-1', status: 'retracted' })
    render(<BehaviorEvidencePanel childId="child-a" />)
    fireEvent.click(screen.getByRole('button', { name: '查看证据' }))
    expect(await screen.findByText('记录到卡顿候选')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '这条观察有误' }))
    fireEvent.change(screen.getByLabelText('回撤观察说明'), { target: { value: '孩子在心算' } })
    expect(screen.getByText(/回放会保留这条审计记录并标记为已回撤/)).toBeInTheDocument()
    expect(screen.getByText(/不再计入个人基线、提醒或后续决策/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '确认回撤并重算基线' }))
    await waitFor(() => expect(retract).toHaveBeenCalledWith('child-a', 'state-1', '孩子在心算'))
  })

  it('labels a corrected observation reminder as withdrawn instead of awaiting feedback', async () => {
    vi.spyOn(localApi, 'behaviorReplay').mockResolvedValue({ child_id: 'child-a', session_id: null, events: [], states: [], interventions: [{ intervention_id: 'withdrawn-1', decided_at: '2026-08-28T10:02:00Z', candidate_state: 'blocked_candidate', message: '要不要圈出卡住的一步？', feedback: null, outcome: null, status: 'withdrawn', withdrawal_reason: 'corrected_observation' }] })
    vi.spyOn(localApi, 'behaviorBaseline').mockResolvedValue({ status: 'insufficient_history', days_observed: 1, minimum_days: 3 })
    vi.spyOn(localApi, 'behaviorPersonalization').mockResolvedValue({ minimum_feedback_samples: 4, boundary: '只会降低提醒频率。', items: [] })
    render(<BehaviorEvidencePanel childId="child-a" />)
    fireEvent.click(screen.getByRole('button', { name: '查看证据' }))
    expect(await screen.findByText(/已因孩子或家长纠正撤回/)).toBeInTheDocument()
    expect(screen.getByText(/不作为学习结论/)).toBeInTheDocument()
  })

  it('keeps an expired reminder as an audit record instead of promising feedback is still possible', async () => {
    vi.spyOn(localApi, 'behaviorReplay').mockResolvedValue({ child_id: 'child-a', session_id: null, events: [], states: [], interventions: [{ intervention_id: 'expired-1', decided_at: '2026-08-28T10:02:00Z', candidate_state: 'blocked_candidate', message: '要不要圈出卡住的一步？', feedback: null, outcome: null, status: 'expired' }] })
    vi.spyOn(localApi, 'behaviorBaseline').mockResolvedValue({ status: 'insufficient_history', days_observed: 1, minimum_days: 3 })
    vi.spyOn(localApi, 'behaviorPersonalization').mockResolvedValue({ minimum_feedback_samples: 4, boundary: '只会降低提醒频率。', items: [] })
    render(<BehaviorEvidencePanel childId="child-a" />)
    fireEvent.click(screen.getByRole('button', { name: '查看证据' }))
    expect(await screen.findByText(/反馈时间已过，不会再次追问/)).toBeInTheDocument()
    expect(screen.getByText(/不再允许反馈或参与后续决策/)).toBeInTheDocument()
  })

  it('shows bounded feedback samples and lets a parent reset future reminder preferences', async () => {
    vi.spyOn(localApi, 'behaviorReplay').mockResolvedValue({ child_id: 'child-a', session_id: null, events: [], states: [], interventions: [] })
    vi.spyOn(localApi, 'behaviorBaseline').mockResolvedValue({ status: 'insufficient_history', days_observed: 0, minimum_days: 3 })
    vi.spyOn(localApi, 'behaviorPersonalization').mockResolvedValue({ minimum_feedback_samples: 4, boundary: '只会降低提醒频率，不会强化观察。', items: [{ candidate_state: 'blocked_candidate', sample_size: 4, accepted: 1, declined: 3, decline_rate: .75, effect_samples: 1, helpful_effects: 1, strategy: 'low_frequency_ask_first', cooldown_minutes: 20, daily_cap: 2, adjusted: true, explanation: ['拒绝率较高。'] }] })
    const reset = vi.spyOn(localApi, 'resetBehaviorPersonalization').mockResolvedValue({ minimum_feedback_samples: 4, boundary: '只会降低提醒频率，不会强化观察。', items: [] })
    render(<BehaviorEvidencePanel childId="child-a" />)
    fireEvent.click(screen.getByRole('button', { name: '查看证据' }))
    expect(await screen.findByText(/孩子反馈 4\/4 次：接受 1，拒绝 3/)).toBeInTheDocument()
    expect(screen.getByText(/间隔至少 20 分钟、每天最多 2 次，仍先询问/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '重置提示偏好' }))
    await waitFor(() => expect(reset).toHaveBeenCalledWith('child-a'))
  })

  it('renders daily observable trends and intervention results without diagnostic or causal claims', async () => {
    vi.spyOn(localApi, 'behaviorReplay').mockResolvedValue({ child_id: 'child-a', session_id: null, events: [], states: [], interventions: [] })
    vi.spyOn(localApi, 'behaviorBaseline').mockResolvedValue({
      status: 'ready', days_observed: 6, switch_count_baseline: { mean: 2, stddev: 1 },
      habit_trends: {
        seated_count: { status: 'ready', prior_daily_mean: 1, recent_daily_mean: 2, delta: 1, direction: 'increasing' },
        away_count: { status: 'ready', prior_daily_mean: 2, recent_daily_mean: 1, delta: -1, direction: 'decreasing' },
        writing_count: { status: 'ready', prior_daily_mean: 1, recent_daily_mean: 1, delta: 0, direction: 'stable' },
      },
      interpretation_boundary: 'Counts summarize observable local events.'
    })
    vi.spyOn(localApi, 'behaviorPersonalization').mockResolvedValue({ minimum_feedback_samples: 4, boundary: '只会降低提醒频率。', items: [{ candidate_state: 'blocked_candidate', sample_size: 4, accepted: 2, declined: 2, decline_rate: .5, effect_samples: 3, helpful_effects: 2, strategy: 'default_ask_first', cooldown_minutes: 10, daily_cap: 3, adjusted: false, explanation: ['样本仍按温和策略处理。'] }] })
    render(<BehaviorEvidencePanel childId="child-a" />)
    fireEvent.click(screen.getByRole('button', { name: '查看证据' }))
    expect(await screen.findByText('日级可观察趋势')).toBeInTheDocument()
    expect(screen.getByText(/在座：近期增加（近期日均 2）/)).toBeInTheDocument()
    expect(screen.getByText(/离座候选：近期减少（近期日均 1）/)).toBeInTheDocument()
    expect(screen.getByText(/不代表情绪、人格或因果/)).toBeInTheDocument()
    expect(screen.getByText(/后续可观察结果 3 次，其中 2 次/)).toBeInTheDocument()
  })

  it('keeps readable evidence when personalization loading fails and offers retry', async () => {
    vi.spyOn(localApi, 'behaviorReplay').mockResolvedValue({ child_id: 'child-a', session_id: null, events: [], states: [], interventions: [] })
    vi.spyOn(localApi, 'behaviorBaseline').mockResolvedValue({ status: 'insufficient_history', days_observed: 0, minimum_days: 3 })
    const personalization = vi.spyOn(localApi, 'behaviorPersonalization').mockRejectedValueOnce(new Error('temporary')).mockResolvedValue({ minimum_feedback_samples: 4, boundary: '只会降低提醒频率。', items: [] })
    render(<BehaviorEvidencePanel childId="child-a" />)
    fireEvent.click(screen.getByRole('button', { name: '查看证据' }))
    expect(await screen.findByText('还没有可观察事件。')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '重试读取' }))
    await waitFor(() => expect(personalization).toHaveBeenCalledTimes(2))
  })
})

describe('SafetyAlertsPanel', () => {
  afterEach(() => vi.restoreAllMocks())

  it('shows action guidance without a child transcript and persists parent acknowledgement', async () => {
    const event: ChildSafetyEvent = {
      safety_event_id: 'safety-1', child_id: 'child-a', child_nickname: '四年级同学',
      risk_level: 'R2', category: 'fear_or_bullying', status: 'pending',
      guidance: {
        summary: '孩子表达了被欺负或明显恐惧，需要家长及时线下确认。',
        script: ['谢谢你愿意让我知道。', '你现在安全吗？'],
        action: { what: '当天完成安全确认', duration_minutes: 15, steps: ['先确认即时安全', '共同选择可信任大人'], stop_condition: '危险正在发生时联系当地紧急服务。' },
        avoid: [{ do_not_say: '你是不是也招惹别人了？', say_instead: '我先确认你的安全。' }]
      },
      created_at: '2026-08-26T12:00:00Z', acknowledged_at: null
    }
    const acknowledged = { ...event, status: 'acknowledged' as const, acknowledged_at: '2026-08-26T12:05:00Z' }
    vi.spyOn(localApi, 'acknowledgeSafetyEvent').mockResolvedValue(acknowledged)
    const onEventsChange = vi.fn()

    render(<SafetyAlertsPanel childId="child-a" events={[event]} onEventsChange={onEventsChange} />)

    expect(screen.getByText('今天及时沟通')).toBeInTheDocument()
    expect(screen.getByText('当天完成安全确认')).toBeInTheDocument()
    expect(screen.getByText('谢谢你愿意让我知道。')).toBeInTheDocument()
    expect(screen.queryByText(/有人在学校欺负我/)).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '我已看到，会线下沟通' }))
    await waitFor(() => expect(onEventsChange).toHaveBeenCalledWith([acknowledged]))
    expect(localApi.acknowledgeSafetyEvent).toHaveBeenCalledWith('safety-1')
  })
})

describe('ReportDetail', () => {
  it('shows the evidence used to personalize executable parent coaching', () => {
    const report: DailyReport = {
      report_id: 'report-1', session_id: 'session-1', child_id: 'child-1', report_date: '2026-08-26', source: 'local_rules_v2',
      summary: { headline: '完成1/1项计划', facts: ['数学任务已记录'], interpretation: '只汇总事实。', evidence_count: 2 },
      parent_coaching: {
        focus: 'correction',
        evidence_basis: ['应用题订正被孩子标记为仍需订正', '孩子自述困难：数量关系'],
        scenario: '今晚只处理孩子指出的一处不确定。',
        why_this_fits: '先承认孩子能发现问题。',
        script: ['我看到你主动标记了需要订正。'],
        action: { what: '重看第二问', duration_minutes: 6, steps: ['请孩子指出第一处'], stop_condition: '到时停止。' },
        avoid: [{ do_not_say: '怎么又错了？', say_instead: '我们只看第一处不确定。' }]
      },
      evidence_detail: {
        recalls: [],
        tasks: [{ subject: '数学', title: '应用题订正', status: 'completed', expected_minutes: 20, actual_minutes: 18, task_type: 'correction', completion_evidence_type: 'needs_correction', completion_note: null, completion_checklist: [], completion_artifact_id: null, completion_artifact_status: null, completion_artifact_confidence: null, completion_artifact_raw_stored: false, completed_at: '2026-08-26T12:00:00Z' }],
        reflection: { learned_text: '', difficult_text: '数量关系', feeling: '', next_action: '重看第二问', skipped: false },
        declarations: { no_recall: false, no_homework: false }
      },
      feedback: null,
      created_at: '2026-08-26T12:00:00Z'
    }

    report.evidence_detail!.tasks.push({ ...report.evidence_detail!.tasks[0], title: '错题延迟复测',
      completion_evidence_type: 'review_evidence' })
    render(<ReportDetail report={report} />)
    expect(screen.getByText(/复测已记录，不等于长期掌握/)).toBeInTheDocument()

    expect(screen.getByText('本次建议依据')).toBeInTheDocument()
    expect(screen.getByText('应用题订正被孩子标记为仍需订正')).toBeInTheDocument()
    expect(screen.getByText('孩子自述困难：数量关系')).toBeInTheDocument()
    expect(screen.getByText('怎么又错了？')).toBeInTheDocument()
    expect(screen.getByText('我们只看第一处不确定。')).toBeInTheDocument()
  })

  it('renders a conservative fallback for legacy reports missing action steps', () => {
    const legacy = {
      report_id: 'legacy-report', session_id: 'legacy-session', child_id: 'child-1', report_date: '2026-08-26', source: 'local_rules_v1',
      summary: { headline: '旧版日报', facts: ['仅保留已记录事实'], interpretation: '旧版记录。', evidence_count: 1 },
      parent_coaching: { script: ['先确认孩子愿不愿意聊。'], avoid: [], action: { what: '确认下一步', duration_minutes: 5 } },
      evidence_detail: { recalls: [], tasks: [], reflection: null, declarations: { no_recall: false, no_homework: false } },
      feedback: null, created_at: '2026-08-26T12:00:00Z'
    } as unknown as DailyReport

    render(<ReportDetail report={legacy} />)

    expect(screen.getByText('旧版报告未记录分步行动；请只执行上面的简短行动，不自行补加要求。')).toBeInTheDocument()
    expect(screen.getByText('确认下一步')).toBeInTheDocument()
  })
})

const state = (childId: string): LearningState => ({
  child_id: childId,
  algorithm_version: 'mastery_rules_v2',
  knowledge_version: 'starter_v2',
  pet_baseline: null,
  items: [{
    knowledge_node_id: 'node-shared',
    track: 'school',
    subject: '数学',
    code: 'MATH.TEST',
    title: '方程检验',
    description: '测试节点',
    mastery_probability: 0.53,
    evidence_count: 1,
    hint_dependency: 0,
    status_confidence: 0.2,
    last_practiced_at: '2026-08-26T08:00:00+00:00',
    last_independent_correct_at: '2026-08-26T08:00:00+00:00',
    forgetting_rate: 0.18,
    next_review_at: '2026-08-28T08:00:00+00:00',
    prerequisites: [],
    next_paths: []
  }]
})

const currentEvidence = (): NonNullable<LearningState['items'][number]['current_evidence']> => ({
  status: 'needs_evidence', readiness: 'needs_evidence', attempt_count: 0,
  independent_attempt_count: 0, supported_attempt_count: 0, independent_correct: 0,
  distinct_items: 0, independent_distinct_items: 0, retention_success_count: 0,
  evidence_refs: [], retention_basis_refs: [], latest_evidence_at: null, mastery_claim_allowed: false
})

const history = (summary: string): MasteryHistoryItem => ({
  algorithm_version: 'mastery_rules_v2',
  created_at: '2026-08-26T08:00:00+00:00',
  evidence_type: 'diagnostic_answer',
  correctness: 1,
  hint_level: 0,
  independent: 1,
  confidence: 1,
  evidence_summary: summary,
  occurred_at: '2026-08-26T08:00:00+00:00',
  old_state: {
    mastery_probability: 0.35,
    evidence_count: 0,
    hint_dependency: 0,
    status_confidence: 0,
    last_practiced_at: null,
    last_independent_correct_at: null,
    forgetting_rate: 0.2,
    next_review_at: null,
    algorithm_version: null
  },
  new_state: {
    mastery_probability: 0.53,
    evidence_count: 1,
    hint_dependency: 0,
    status_confidence: 0.2,
    last_practiced_at: '2026-08-26T08:00:00+00:00',
    last_independent_correct_at: '2026-08-26T08:00:00+00:00',
    forgetting_rate: 0.18,
    next_review_at: '2026-08-28T08:00:00+00:00',
    algorithm_version: 'mastery_rules_v2'
  }
})

describe('LearningOverview', () => {
  afterEach(() => { cleanup(); vi.restoreAllMocks() })

  it('uses current receipts instead of stale high mastery and updates after withdrawal', () => {
    const learning = state('child-a')
    learning.items[0].mastery_probability = 0.99
    learning.items[0].current_evidence = { ...currentEvidence(), status: 'independent_observed',
      attempt_count: 2, independent_attempt_count: 2, independent_correct: 2, distinct_items: 2,
      independent_distinct_items: 2, evidence_refs: ['answer-a', 'answer-b'] }
    const { rerender } = render(<LearningOverview state={learning} />)
    expect(screen.getByText('已有独立作答')).toBeInTheDocument()
    expect(screen.getByText('2次独立正确 · 0次辅助作答 · 0次延迟复测')).toBeInTheDocument()
    expect(screen.queryByText('99%')).not.toBeInTheDocument()
    rerender(<LearningOverview state={{ ...learning, items: [{ ...learning.items[0], current_evidence: currentEvidence() }] }} />)
    expect(screen.getByText('待核实')).toBeInTheDocument()
    expect(screen.queryByText('已有独立作答')).not.toBeInTheDocument()
    expect(screen.queryByText('99%')).not.toBeInTheDocument()
  })

  it('describes prerequisite evidence without asserting the ability is mastered', () => {
    const learning = state('child-a')
    learning.items[0].prerequisites = [
      { knowledge_node_id: 'prerequisite-a', title: '等式性质', track: 'school', mastery_probability: 0.8, status_confidence: 0.7, readiness: 'ready' },
      { knowledge_node_id: 'prerequisite-b', title: '分数运算', track: 'school', mastery_probability: null, status_confidence: null, readiness: 'needs_evidence' }
    ]
    render(<LearningOverview state={learning} />)
    expect(screen.getByText('前置：等式性质（有核实证据）、分数运算（待取证）')).toBeInTheDocument()
    expect(screen.queryByText(/已具备/)).not.toBeInTheDocument()
  })

  it('shows a human-readable mastery trail and clears cached evidence when switching children', async () => {
    const load = vi.spyOn(localApi, 'masteryHistory')
      .mockResolvedValueOnce([history('甲孩子独立完成诊断题')])
      .mockResolvedValueOnce([history('乙孩子独立完成诊断题')])
    const { rerender } = render(<LearningOverview key="child-a" state={state('child-a')} />)

    fireEvent.click(screen.getByTitle('查看掌握度依据'))
    await screen.findByText('甲孩子独立完成诊断题')
    expect(screen.getByText(/35% → 53%/)).toBeInTheDocument()

    rerender(<LearningOverview key="child-b" state={state('child-b')} />)
    expect(screen.queryByText('甲孩子独立完成诊断题')).not.toBeInTheDocument()
    fireEvent.click(screen.getByTitle('查看掌握度依据'))
    await screen.findByText('乙孩子独立完成诊断题')
    await waitFor(() => expect(load).toHaveBeenLastCalledWith('child-b', 'node-shared'))
  })

  it('shows PET baseline completion as reference evidence, not a pass prediction', () => {
    render(<LearningOverview state={{
      ...state('child-primary-4'),
      pet_baseline: {
        definition_version: 'pet_baseline_v1', status: 'in_progress', completed_tasks: 3,
        required_tasks: 5, evidence_count: 4, reference_only: true,
        boundary: '完成仅表示听说读写规定任务已有证据，不代表达到PET通过水平。',
        dimensions: [
          { key: 'reading', label: '阅读', score_kind: 'objective', completed_tasks: 1, required_tasks: 1, status: 'completed', reference_score: 100, tasks: [] },
          { key: 'listening', label: '听力', score_kind: 'objective', completed_tasks: 1, required_tasks: 1, status: 'completed', reference_score: 100, tasks: [] },
          { key: 'writing', label: '写作', score_kind: 'reference', completed_tasks: 1, required_tasks: 1, status: 'completed', reference_score: 76, tasks: [] },
          { key: 'speaking', label: '口语', score_kind: 'reference', completed_tasks: 0, required_tasks: 2, status: 'not_started', reference_score: null, tasks: [] }
        ]
      }
    }} />)

    expect(screen.getByText('3/5项已有证据 · 参考评估')).toBeInTheDocument()
    expect(screen.getByText('完成仅表示听说读写规定任务已有证据，不代表达到PET通过水平。')).toBeInTheDocument()
    expect(screen.queryByText(/通过概率/)).not.toBeInTheDocument()
  })
})

describe('MistakeWeaknessPanel', () => {
  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
  })

  it('shows evidence, relapse and the next review in parent language', async () => {
    vi.spyOn(localApi, 'weaknessClusters').mockResolvedValue([{
      cluster_id: 'cluster-1', knowledge_node_id: 'node-1', subject: '数学', title: '根的判别式',
      diagnosis_status: 'confirmed_gap', occurrence_count: 3, relapse_count: 1,
      updated_at: '2026-08-27T08:00:00Z', mastery_probability: 0.42,
      hint_dependency: 0.25, status_confidence: 0.8, next_review_at: '2026-08-29T08:00:00Z'
    }])

    const { container } = render(<MistakeWeaknessPanel childId="child-a" />)
    const panel = within(container)

    expect(await panel.findByText('根的判别式')).toBeInTheDocument()
    expect(panel.getByText('历史状态：已确认薄弱点')).toBeInTheDocument()
    expect(panel.getByText('3')).toBeInTheDocument()
    expect(panel.getByText('1')).toBeInTheDocument()
    expect(panel.getByText('42%')).toBeInTheDocument()
    expect(panel.getByText('历史掌握估计')).toBeInTheDocument()
    expect(panel.getByText(/下次复测/)).toBeInTheDocument()
    expect(panel.queryByText('cluster-1')).not.toBeInTheDocument()
  })

  it('does not reuse stable historical percentages after current evidence is withdrawn', async () => {
    vi.spyOn(localApi, 'weaknessClusters').mockResolvedValue([{
      cluster_id: 'old-stable', knowledge_node_id: 'node-1', subject: '数学', title: '等式性质',
      diagnosis_status: 'stable', occurrence_count: 3, relapse_count: 1, updated_at: '2026-08-27T08:00:00Z',
      mastery_probability: 0.99, hint_dependency: 0.25, status_confidence: 0.9, next_review_at: null,
      current_evidence: currentEvidence()
    }])
    render(<MistakeWeaknessPanel childId="child-a" />)
    expect(await screen.findByText('待核实')).toBeInTheDocument()
    expect(screen.getByText('当前缺少可核验作答，先核实，不据此判断会或不会。')).toBeInTheDocument()
    expect(screen.queryByText('99%')).not.toBeInTheDocument()
    expect(screen.queryByText('近期稳定')).not.toBeInTheDocument()
    expect(screen.queryByText('当前不需要追加练习。')).not.toBeInTheDocument()
  })

  it('offers a retry when the evidence query fails', async () => {
    const load = vi.spyOn(localApi, 'weaknessClusters')
      .mockRejectedValueOnce(new Error('offline'))
      .mockResolvedValueOnce([])
    const { container } = render(<MistakeWeaknessPanel childId="child-a" />)
    const panel = within(container)

    expect(await panel.findByText('薄弱点证据暂时无法读取。')).toBeInTheDocument()
    fireEvent.click(panel.getByRole('button', { name: '重试' }))
    await panel.findByText('暂时没有已形成的薄弱点')
    expect(load).toHaveBeenCalledTimes(2)
  })
})

describe('DesktopSettingsPanel', () => {
  afterEach(() => {
    vi.restoreAllMocks()
    delete window.desktop
  })

  it('offers a visible repair action and verifies the resulting registration', async () => {
    const setAutoLaunch = vi.fn().mockResolvedValue({ openAtLogin: true })
    window.desktop = {
      getAutoLaunch: vi.fn().mockResolvedValue({ openAtLogin: false }),
      setAutoLaunch,
    } as unknown as typeof window.desktop

    render(<DesktopSettingsPanel />)
    expect(await screen.findByText('当前未启用自动启动。可以直接修复。')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '修复自动启动' }))

    await waitFor(() => expect(setAutoLaunch).toHaveBeenCalledWith(true))
    expect(await screen.findByText('自动启动已修复并启用。')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '修复自动启动' })).not.toBeInTheDocument()
  })
})

describe('OperationsHealthPanel', () => {
  afterEach(() => { window.desktop = undefined; vi.restoreAllMocks() })

  it('shows the local diagnostic boundary and opens its folder for a parent', async () => {
    const health: SystemHealth = {
      database_integrity: 'ok', database_size_bytes: 4096, disk_free_bytes: 100000,
      pending_report_jobs: 0, pending_agent_turns: 0, pending_deletion_jobs: 0,
      model_failures_last_7_days: 0, active_sensor_leases: 0, last_backup: null,
      maintenance: { daily_backup: null, retention_cleanup: null }, recent_maintenance_failures: [],
      raw_media_files_stored: 0, devices: [], device_incidents: [], recent_model_failures: [],
      recent_report_failures: [], recent_agent_failures: [], recent_deletion_failures: [],
    }
    const revealDiagnosticFolder = vi.fn().mockResolvedValue({ opened: true })
    const getDiagnosticSummary = vi.fn().mockResolvedValue({
      rootPath: 'C:\\Users\\family\\AppData\\Roaming\\diagnostics',
      logPath: 'C:\\Users\\family\\AppData\\Roaming\\diagnostics\\logs\\runtime.jsonl',
      crashDumpPath: 'C:\\Users\\family\\AppData\\Roaming\\diagnostics\\crashes',
      currentLogBytes: 2048, rotatedLogCount: 2, maxFileBytes: 10485760, maxRotatedFiles: 5,
      rawMediaRecorded: false, existingLogsVerified: true, crashDumpsInspected: false, uploadEnabled: false,
    })
    window.desktop = { getDiagnosticSummary, revealDiagnosticFolder } as unknown as NonNullable<typeof window.desktop>

    render(<OperationsHealthPanel initial={health} onHealthChange={() => undefined} onReturnToChild={() => undefined} />)

    expect(await screen.findByText('本地问题记录')).toBeInTheDocument()
    expect(screen.getByText(/2\.0 KB/)).toBeInTheDocument()
    expect(screen.getByText(/新诊断事件仅记录筛选后的运行信息/)).toBeInTheDocument()
    expect(screen.getByText(/崩溃转储尚未检查/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '打开诊断目录' }))
    await waitFor(() => expect(revealDiagnosticFolder).toHaveBeenCalledTimes(1))
  })

  it('keeps old observation quality as history outside current device status', () => {
    const health: SystemHealth = {
      database_integrity: 'ok', database_size_bytes: 4096, disk_free_bytes: 100000,
      pending_report_jobs: 0, pending_agent_turns: 0, pending_deletion_jobs: 0,
      model_failures_last_7_days: 0, active_sensor_leases: 1, last_backup: null,
      maintenance: { daily_backup: null, retention_cleanup: null }, recent_maintenance_failures: [],
      raw_media_files_stored: 0, devices: [{ device_type: 'camera', status: 'ready', last_seen_at: '2026-08-27T12:00:00Z', application_reading: 1, last_release_reason: null, binding_label: '观察摄像头' }],
      device_incidents: [], recent_model_failures: [], recent_report_failures: [], recent_agent_failures: [], recent_deletion_failures: [],
      observation_quality: [{ child_nickname: '初三同学', session_id: 'session-dark', sample_count: 6, usable_sample_count: 0, usable_ratio: 0, latest_state: 'unknown', latest_reason: '画面过暗或过亮，不能可靠判断。', latest_observed_at: '2026-08-27T12:00:00Z' }],
    }

    render(<OperationsHealthPanel initial={health} onHealthChange={() => undefined} onReturnToChild={() => undefined} />)

    expect(screen.queryByText('应用正在本地读取')).not.toBeInTheDocument()
    expect(screen.getByRole('heading', { name: '历史观察质量记录' })).toBeInTheDocument()
    expect(screen.getByText('0/6个状态样本可用')).toBeInTheDocument()
    expect(screen.getByText(/持续观察已停用/)).toBeInTheDocument()
  })

  it('shows historical corrections without offering an observation restart', () => {
    const health: SystemHealth = {
      database_integrity: 'ok', database_size_bytes: 4096, disk_free_bytes: 100000,
      pending_report_jobs: 0, pending_agent_turns: 0, pending_deletion_jobs: 0,
      model_failures_last_7_days: 0, active_sensor_leases: 0, last_backup: null,
      maintenance: { daily_backup: null, retention_cleanup: null }, recent_maintenance_failures: [],
      raw_media_files_stored: 0, devices: [], device_incidents: [], recent_model_failures: [], recent_report_failures: [], recent_agent_failures: [], recent_deletion_failures: [],
      observation_quality: [{
        child_nickname: '四年级同学', session_id: 'session-not-rendered', sample_count: 0,
        usable_sample_count: 0, usable_ratio: 0, latest_state: 'unknown',
        latest_reason: '画面太暗，请调整后重试。', latest_observed_at: '2026-08-28T12:00:00Z',
        qualification_status: 'unavailable', scene_quality: 'too_dark', person_baseline: 'not_evaluated',
        qualification_source: 'renderer', qualification_attempt_count: 2, correction_count_7d: 2,
        auto_shadowed: true, observation_mode: 'shadow',
      }],
    }

    render(<OperationsHealthPanel initial={health} onHealthChange={() => undefined} onReturnToChild={() => undefined} />)

    expect(screen.getByText('当时画面不可用')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /重新检测/ })).not.toBeInTheDocument()
    expect(screen.getByText('近7天曾收到2次孩子或家长纠正。')).toBeInTheDocument()
    expect(screen.getByText('曾因纠正自动暂停提醒和长期行为推断。')).toBeInTheDocument()
    expect(screen.queryByText('session-not-rendered')).not.toBeInTheDocument()
  })

  it('shows the latest protected database upgrade without exposing a backup path', () => {
    const health: SystemHealth = {
      database_integrity: 'ok', database_size_bytes: 4096, disk_free_bytes: 100000,
      database_upgrade: {
        status: 'completed', source_schema_version: 47, target_schema_version: 48,
        created_at: '2026-08-26T08:00:00Z', completed_at: '2026-08-26T08:00:01Z',
        error_type: null, snapshot_available: true,
      },
      pending_report_jobs: 0, pending_agent_turns: 0, model_failures_last_7_days: 0,
      active_sensor_leases: 0, last_backup: null,
      maintenance: { daily_backup: null, retention_cleanup: null },
      pending_deletion_jobs: 0, recent_maintenance_failures: [], raw_media_files_stored: 0, devices: [],
      device_incidents: [], recent_model_failures: [], recent_report_failures: [], recent_agent_failures: [], recent_deletion_failures: [],
    }

    render(<OperationsHealthPanel initial={health} onHealthChange={() => undefined} onReturnToChild={() => undefined} />)

    expect(screen.getByText('数据库升级 v47 → v48')).toBeInTheDocument()
    expect(screen.getByText('升级完成，升级前恢复点已保留。')).toBeInTheDocument()
    expect(screen.getByText('已保护')).toBeInTheDocument()
    expect(document.body.textContent).not.toContain('upgrade-backups')
  })

  it('shows a path-safe deletion failure and retries it from the operator workflow', async () => {
    const failure = {
      deletion_id: 'delete-job-001', scope: 'media', target_type: 'capture_artifact' as const,
      error_code: 'FILE_DELETE_FAILED', error_message: '仍有受管理媒体文件未能删除，请关闭占用程序后重试。',
      remaining_file_count: 1, updated_at: '2026-08-26T08:10:00Z', child_nickname: '小学生',
    }
    const health: SystemHealth = {
      database_integrity: 'ok', database_size_bytes: 4096, disk_free_bytes: 100000,
      pending_report_jobs: 0, pending_agent_turns: 0, pending_deletion_jobs: 0,
      model_failures_last_7_days: 0, active_sensor_leases: 0, last_backup: null,
      maintenance: { daily_backup: null, retention_cleanup: null }, recent_maintenance_failures: [],
      raw_media_files_stored: 1, devices: [], device_incidents: [], recent_model_failures: [],
      recent_report_failures: [], recent_agent_failures: [], recent_deletion_failures: [failure],
    }
    const recovered = { ...health, raw_media_files_stored: 0, recent_deletion_failures: [] }
    const retry = vi.spyOn(localApi, 'retryDeletion').mockResolvedValue({
      status: 'completed', deletion_id: failure.deletion_id, remaining_file_count: 0,
    })
    vi.spyOn(localApi, 'systemHealth').mockResolvedValue(recovered)
    const onHealthChange = vi.fn()

    render(<OperationsHealthPanel initial={health} onHealthChange={onHealthChange} onReturnToChild={() => undefined} />)
    expect(screen.getByText(/1个本地文件/)).toBeInTheDocument()
    expect(document.body.textContent).not.toContain('occupied.jpg')
    fireEvent.click(screen.getByRole('button', { name: '重试并验证删除' }))

    await waitFor(() => expect(retry).toHaveBeenCalledWith(failure.deletion_id))
    expect(await screen.findByText('删除任务已完成并通过残留验证。')).toBeInTheDocument()
    expect(onHealthChange).toHaveBeenCalledWith(recovered)
  })
})

describe('PeriodReportsPanel', () => {
  afterEach(() => { cleanup(); vi.restoreAllMocks() })

  it('shows automatic calendar-period evidence and refreshes the same report in place', async () => {
    const report: PeriodReport = {
      period_report_id: 'period-week-1', child_id: 'child-a', report_type: 'weekly',
      period_start: '2026-08-24', period_end: '2026-08-30', source: 'local_aggregate_v2',
      content: {
        headline: '1天有完整学习记录', as_of_date: '2026-08-27',
        coverage: { recorded_days: 1, recorded_sessions: 2, expected_calendar_days: 7, evidence_count: 6, sufficient_for_trend: false },
        daily_signals: [{ date: '2026-08-27', headline: '2次学习会话', session_count: 2 }],
        interpretation: '记录天数不足以判断稳定趋势；本报告只汇总已经发生的学习会话。',
        next_action: null, parent_script: [],
      },
      created_at: '2026-08-27T08:00:00Z', updated_at: '2026-08-27T08:10:00Z',
    }
    const refreshed = {
      ...report,
      content: { ...report.content, coverage: { ...report.content.coverage, recorded_sessions: 3, evidence_count: 8 } },
      updated_at: '2026-08-27T09:00:00Z',
    }
    const refresh = vi.spyOn(localApi, 'generatePeriodReport').mockResolvedValue(refreshed)
    const onGenerated = vi.fn()

    const view = render(<PeriodReportsPanel childId="child-a" initial={[report]} onGenerated={onGenerated} />)

    expect(screen.getByText('自动阶段报告')).toBeInTheDocument()
    expect(screen.getByText(/1天 · 2次会话/)).toBeInTheDocument()
    expect(screen.getByText(/截至 2026-08-27/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '刷新本周' }))

    await waitFor(() => expect(refresh).toHaveBeenCalledWith('child-a', 'weekly'))
    expect(onGenerated).toHaveBeenCalledWith(refreshed)
    expect(await screen.findByText(/1天 · 3次会话/)).toBeInTheDocument()
    expect(within(view.container).getAllByRole('article')).toHaveLength(1)
  })
})

describe('PrivacyOperations deletion recovery rotation', () => {
  afterEach(() => { cleanup(); localStorage.clear(); window.desktop = undefined; vi.restoreAllMocks(); vi.unstubAllGlobals() })

  function deletionProtocol(committed: boolean, referenceId: string) {
    vi.stubGlobal('crypto', webcrypto)
    const generation = '50617198-ed7b-4865-a98f-5b7c1b44532e'
    let original: PrivacyOperationIdentity | undefined
    vi.spyOn(localApi, 'privacyOperationGeneration').mockResolvedValue({ generation })
    const receipt = vi.spyOn(localApi, 'privacyOperationReceipt').mockImplementation(async requestId => {
      const intent = readPrivacyIntent()!
      expect(intent.requestId).toBe(requestId)
      expect(intent.childId).toBe('child-a')
      expect(intent.scope).toBe('learning')
      expect(intent.generation).toBe(generation)
      original = operationIdentity(intent)
      expect(vi.mocked(localApi.deleteChildData).mock.calls.at(-1)).toEqual(['child-a', 'learning', '确认删除', original])
      const result = vi.mocked(localApi.deleteChildData).mock.results.at(-1)!
      expect(result.type).toBe('return')
      if (committed) await result.value
      else await expect(result.value).rejects.toBeInstanceOf(ApiError)
      return { request_id: requestId, kind: 'delete', child_id: 'child-a', scope: 'learning',
        origin_generation: generation, result_generation: committed ? generation : null, current_generation: generation,
        cleanup_manifest_sha256: intent.manifestSha256, status: committed ? 'committed' : 'failed', cleanup_allowed: committed,
        reference: { type: 'deletion_job', id: referenceId }, local_only: true, cloud_transmission_allowed: false }
    })
    return { receipt, get operation() { return original } }
  }

  it('creates a password-encrypted portable bundle and reveals it without exposing the path', async () => {
    const health: SystemHealth = {
      database_integrity: 'ok', database_size_bytes: 4096, disk_free_bytes: 100000,
      pending_report_jobs: 0, pending_agent_turns: 0, pending_deletion_jobs: 0,
      model_failures_last_7_days: 0, active_sensor_leases: 0, last_backup: null,
      maintenance: { daily_backup: null, retention_cleanup: null }, recent_maintenance_failures: [],
      raw_media_files_stored: 0, devices: [], device_incidents: [], recent_model_failures: [],
      recent_report_failures: [], recent_agent_failures: [], recent_deletion_failures: [],
    }
    const preferences: PrivacyPreferences = {
      cloud_model_text_allowed: true, cloud_audio_allowed: false,
      raw_media_storage_allowed: false, model_improvement_allowed: false,
      model_call_retention_days: 90, tutor_turn_retention_days: 180,
      report_retention_days: 730, updated_at: '2026-08-26T08:00:00Z',
    }
    const revealLocalFile = vi.fn().mockResolvedValue({ revealed: true as const })
    window.desktop = { revealLocalFile } as unknown as NonNullable<typeof window.desktop>
    vi.spyOn(localApi, 'listBackups').mockResolvedValue([])
    vi.spyOn(localApi, 'captureArtifacts').mockResolvedValue([])
    const create = vi.spyOn(localApi, 'createPortableBackup').mockResolvedValue({
      portable_backup_id: 'portable-1', path: '/private/family.hscbackup', size_bytes: 8192,
      sha256: 'a'.repeat(64), media_file_count: 2, format: 'hsc_portable_backup_v1',
    })

    render(<PrivacyOperations childId="child-a" initial={preferences} health={health} onHealthChange={vi.fn()} />)
    fireEvent.change(screen.getByLabelText('迁移备份口令'), { target: { value: 'correct horse battery staple' } })
    fireEvent.click(screen.getByRole('button', { name: '生成迁移包' }))

    await waitFor(() => expect(create).toHaveBeenCalledWith('correct horse battery staple'))
    expect(revealLocalFile).toHaveBeenCalledWith('/private/family.hscbackup')
    expect(await screen.findByText(/2个媒体文件/)).toBeInTheDocument()
    expect(screen.getByText(/恢复后不会自动重发历史报告/)).toBeInTheDocument()
    expect(screen.getByText(/通知通道的密码需在新电脑重新配置/)).toBeInTheDocument()
    expect(document.body.textContent).not.toContain('/private/family.hscbackup')
  })

  it('removes stale recovery choices and shows the post-deletion recovery point immediately', async () => {
    const oldBackup: BackupManifest = {
      backup_id: 'backup-old', path: '/private/old.db', database_version: 47,
      size_bytes: 4096, sha256: 'a'.repeat(64), integrity_status: 'ok',
      reason: 'manual', created_at: '2026-08-26T08:00:00Z',
    }
    const replacement: BackupManifest = {
      ...oldBackup, backup_id: 'backup-new', path: '/private/new.db',
      reason: 'post_deletion', created_at: '2026-08-26T08:02:00Z',
    }
    const health: SystemHealth = {
      database_integrity: 'ok', database_size_bytes: 4096, disk_free_bytes: 100000,
      pending_report_jobs: 0, pending_agent_turns: 0, model_failures_last_7_days: 0,
      active_sensor_leases: 0, last_backup: null,
      maintenance: { daily_backup: null, retention_cleanup: null },
      pending_deletion_jobs: 0, recent_maintenance_failures: [], raw_media_files_stored: 0, devices: [],
      device_incidents: [], recent_model_failures: [], recent_report_failures: [], recent_agent_failures: [], recent_deletion_failures: [],
    }
    const preferences: PrivacyPreferences = {
      cloud_model_text_allowed: true, cloud_audio_allowed: false,
      raw_media_storage_allowed: false, model_improvement_allowed: false,
      model_call_retention_days: 90, tutor_turn_retention_days: 180,
      report_retention_days: 730, updated_at: '2026-08-26T08:00:00Z',
    }
    vi.spyOn(localApi, 'listBackups').mockResolvedValueOnce([oldBackup]).mockResolvedValueOnce([replacement])
    vi.spyOn(localApi, 'captureArtifacts').mockResolvedValue([])
    vi.spyOn(localApi, 'deletionPreview').mockResolvedValue({ learning: 2 })
    const deleteData = vi.spyOn(localApi, 'deleteChildData').mockResolvedValue({
      deleted_counts: { learning_evidence: 2 },
      recovery_rotation: {
        status: 'completed', invalidated_backup_count: 1, removed_backup_bundle_files: 1,
        removed_upgrade_snapshot_files: 2, replacement_backup_id: 'backup-new',
      },
    })
    vi.spyOn(localApi, 'systemHealth').mockResolvedValue({ ...health, last_backup: replacement })
    const onHealthChange = vi.fn()

    const protocol = deletionProtocol(true, 'delete-job-001')
    render(<PrivacyOperations childId="child-a" initial={preferences} health={health} onHealthChange={onHealthChange} />)
    await screen.findByRole('option', { name: /家长手动/ })
    fireEvent.change(screen.getByLabelText('删除确认'), { target: { value: '确认删除' } })
    fireEvent.click(screen.getByRole('button', { name: '删除' }))

    await waitFor(() => {
      expect(protocol.operation).toBeDefined()
      expect(deleteData).toHaveBeenCalledWith('child-a', 'learning', '确认删除', protocol.operation)
    })
    expect(await screen.findByRole('option', { name: /删除后保护/ })).toBeInTheDocument()
    expect(screen.queryByRole('option', { name: /家长手动/ })).not.toBeInTheDocument()
    expect(screen.getByText(/1个旧恢复点已失效，并已生成删除后的新恢复点/)).toBeInTheDocument()
    expect(onHealthChange).toHaveBeenCalled()
  })

  it('refreshes operator health immediately when deletion needs a retry', async () => {
    const failure = {
      deletion_id: 'delete-job-002', scope: 'learning', target_type: 'child_scope' as const,
      error_code: 'RECOVERY_POINT_ROTATION_FAILED', error_message: '含旧数据的恢复点没有全部完成轮换，请重试。',
      remaining_file_count: 0, updated_at: '2026-08-26T08:20:00Z', child_nickname: '四年级同学',
    }
    const health: SystemHealth = {
      database_integrity: 'ok', database_size_bytes: 4096, disk_free_bytes: 100000,
      pending_report_jobs: 0, pending_agent_turns: 0, pending_deletion_jobs: 0,
      model_failures_last_7_days: 0, active_sensor_leases: 0, last_backup: null,
      maintenance: { daily_backup: null, retention_cleanup: null }, recent_maintenance_failures: [],
      raw_media_files_stored: 0, devices: [], device_incidents: [], recent_model_failures: [],
      recent_report_failures: [], recent_agent_failures: [], recent_deletion_failures: [],
    }
    const failedHealth = { ...health, recent_deletion_failures: [failure] }
    const preferences: PrivacyPreferences = {
      cloud_model_text_allowed: true, cloud_audio_allowed: false,
      raw_media_storage_allowed: false, model_improvement_allowed: false,
      model_call_retention_days: 90, tutor_turn_retention_days: 180,
      report_retention_days: 730, updated_at: '2026-08-26T08:00:00Z',
    }
    vi.spyOn(localApi, 'listBackups').mockResolvedValue([])
    vi.spyOn(localApi, 'captureArtifacts').mockResolvedValue([])
    vi.spyOn(localApi, 'deletionPreview').mockResolvedValue({ learning: 1 })
    vi.spyOn(localApi, 'deleteChildData').mockRejectedValue(new ApiError(
      '数据删除尚未完全验证，任务已保留，请在运行状态中重试。',
      500, failure.error_code, true, 'retry_deletion', { deletion_id: failure.deletion_id },
    ))
    vi.spyOn(localApi, 'systemHealth').mockResolvedValue(failedHealth)
    const onHealthChange = vi.fn()

    const protocol = deletionProtocol(false, failure.deletion_id)
    const view = render(<PrivacyOperations childId="child-a" initial={preferences} health={health} onHealthChange={onHealthChange} />)
    fireEvent.change(within(view.container).getByLabelText('删除确认'), { target: { value: '确认删除' } })
    fireEvent.click(within(view.container).getByRole('button', { name: '删除' }))

    expect(await screen.findByText(/数据删除尚未完全验证/)).toBeInTheDocument()
    await waitFor(() => expect(onHealthChange).toHaveBeenCalledWith(failedHealth))
    expect(protocol.receipt).toHaveBeenCalledWith(protocol.operation!.request_id)
  })
})

describe('device qualification evidence', () => {
  it('renders human-readable runtime evidence without exposing device identifiers', () => {
    const label = qualificationEvidenceLabel({
      run_id: 'private-run-id', mode: 'camera_stability', status: 'passed', expected_seconds: 3600,
      failure_code: null, started_at: '2026-08-26T08:00:00Z', completed_at: '2026-08-26T09:00:02Z',
      metrics: {
        devices: {
          camera: {
            width: 1920, height: 1080, frame_rate: 29.8, samples: 3599,
            frozen_samples: 1, max_consecutive_frozen: 1,
            content_change_samples: 1700, low_variation_samples: 1898,
          }
        },
        validation: { policy_version: 'device_qualification_v2', server_elapsed_seconds: 3602.2 }
      }
    })
    expect(label).toContain('观察摄像头 1920×1080 · 29.8fps')
    expect(label).toContain('采样3599/冻结1（最长1秒）')
    expect(label).toContain('服务端复核3602.2秒')
    expect(label).not.toContain('private-run-id')
  })

  it('keeps a completed result visible when the parent summary refresh needs reauthentication', () => {
    const current: DeviceQualificationSummary = {
      bindings_ready: true, profiles_ready: true, acceptance_status: 'pending', recent_runs: [],
      profile_missing: { camera: [], document_camera: [] },
      required: [
        { mode: 'dual_concurrency', status: 'passed', completed_at: '2026-08-26T08:30:00Z' },
        { mode: 'camera_stability', status: 'not_run', completed_at: null },
        { mode: 'document_stability', status: 'passed', completed_at: '2026-08-26T09:30:00Z' },
      ],
    }
    const merged = mergeQualificationResult(current, {
      run_id: 'camera-run', mode: 'camera_stability', status: 'passed', expected_seconds: 3600,
      metrics: { devices: { camera: { samples: 3572 } } }, failure_code: null,
      started_at: '2026-08-26T10:00:00Z', completed_at: '2026-08-26T11:00:00Z',
    })

    expect(merged.required.find((item) => item.mode === 'camera_stability')).toMatchObject({
      status: 'passed', completed_at: '2026-08-26T11:00:00Z',
    })
    expect(merged.acceptance_status).toBe('passed')
    expect(merged.recent_runs[0].run_id).toBe('camera-run')
  })
})

describe('DeviceSetupPanel capability profiles', () => {
  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
  })

  it('lists and binds only the named USB document camera', async () => {
    const media = navigator.mediaDevices
    const enumerateDevices = vi.fn().mockResolvedValue([
      { kind: 'videoinput', deviceId: 'built-in', groupId: 'internal', label: 'Integrated Camera' },
      { kind: 'videoinput', deviceId: 'room-camera', groupId: 'room', label: 'L523' },
      { kind: 'videoinput', deviceId: 'usb-hd', groupId: 'external', label: 'Usb HDCamera' },
    ])
    const track = { label: 'Usb HDCamera', getSettings: () => ({ deviceId: 'usb-hd', groupId: 'external' }), stop: vi.fn() }
    const getUserMedia = vi.fn().mockResolvedValue({ getVideoTracks: () => [track], getTracks: () => [track] })
    Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: { enumerateDevices, getUserMedia } })
    try {
      const qualification: DeviceQualificationSummary = { bindings_ready: false, profiles_ready: false,
        acceptance_status: 'pending', recent_runs: [], profile_missing: { camera: [], document_camera: [] }, required: [] }
      const save = vi.spyOn(localApi, 'saveDeviceBindings').mockResolvedValue([])
      vi.spyOn(localApi, 'deviceQualification').mockResolvedValue(qualification)
      render(<DeviceSetupPanel initial={[]} initialQualification={qualification} sessionActive={false} />)
      fireEvent.click(screen.getByRole('button', { name: '扫描高拍仪' }))
      await waitFor(() => expect(screen.getByLabelText('选择高拍仪')).toHaveValue('usb-hd'))
      expect(getUserMedia).not.toHaveBeenCalled()
      expect(screen.queryByRole('option', { name: 'Integrated Camera' })).not.toBeInTheDocument()
      expect(screen.queryByRole('option', { name: 'L523' })).not.toBeInTheDocument()
      fireEvent.click(screen.getByRole('button', { name: '验证并保存高拍仪' }))
      await screen.findByText('高拍仪已验证并绑定。')
      expect(getUserMedia).toHaveBeenCalledExactlyOnceWith({ audio: false, video: { deviceId: { exact: 'usb-hd' }, width: { ideal: 2560 }, height: { ideal: 1440 } } })
      expect(track.stop).toHaveBeenCalledTimes(1)
      expect(save).toHaveBeenCalledExactlyOnceWith([expect.objectContaining({ device_type: 'document_camera', label: 'Usb HDCamera' })])
    } finally {
      Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: media })
    }
  })

  it('does not open unlabeled video inputs to probe permission', async () => {
    const media = navigator.mediaDevices
    const getUserMedia = vi.fn()
    const enumerateDevices = vi.fn().mockResolvedValue([
      { kind: 'videoinput', deviceId: 'unknown-camera', groupId: '', label: '' },
    ])
    Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: { enumerateDevices, getUserMedia } })
    try {
      const qualification: DeviceQualificationSummary = { bindings_ready: false, profiles_ready: false,
        acceptance_status: 'pending', recent_runs: [], profile_missing: { camera: [], document_camera: [] }, required: [] }
      render(<DeviceSetupPanel initial={[]} initialQualification={qualification} sessionActive={false} />)
      fireEvent.click(screen.getByRole('button', { name: '扫描高拍仪' }))
      expect(await screen.findByText(/未识别到可绑定的高拍仪/)).toBeInTheDocument()
      expect(getUserMedia).not.toHaveBeenCalled()
      expect(screen.getByRole('button', { name: '验证并保存高拍仪' })).toBeDisabled()
    } finally {
      Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: media })
    }
  })

  it('keeps the old room binding out of settings, qualification, and updates', () => {
    const camera: DeviceBinding = {
      device_type: 'camera', system_id_hash: 'camera-only-hash-012345', label: 'L523',
      capabilities: { manufacturer: '', model: 'L523', driver_version: '', usb_vid: '1D6B', usb_pid: '0102', capture_protocol: 'unverified', ptz_capability: 'unverified', audio_capability: 'unverified' },
      verified_at: '2026-08-26T08:00:00Z', updated_at: '2026-08-26T08:00:00Z',
    }
    const qualification: DeviceQualificationSummary = {
      bindings_ready: false, profiles_ready: false, acceptance_status: 'pending', recent_runs: [],
      profile_missing: { camera: ['manufacturer', 'driver_version', 'capture_protocol', 'ptz_capability', 'audio_capability'], document_camera: ['manufacturer', 'model'] },
      required: [
        { mode: 'dual_concurrency', status: 'not_run', completed_at: null },
        { mode: 'camera_stability', status: 'not_run', completed_at: null },
        { mode: 'document_stability', status: 'not_run', completed_at: null },
      ],
    }
    render(<DeviceSetupPanel initial={[camera]} initialQualification={qualification} sessionActive={false} />)

    expect(screen.queryByText('L523')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('设备验收模式')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('选择观察摄像头')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '开始验收' })).toBeDisabled()
    expect(screen.getByRole('button', { name: '保存能力档案' })).toBeDisabled()
  })

  it('restores and saves human-reviewed capability data without reopening devices', async () => {
    const bindings: DeviceBinding[] = [
      {
        device_type: 'camera', system_id_hash: 'camera-hash-0123456789', label: 'Room Camera',
        capabilities: { manufacturer: 'Camera Vendor', model: 'Room One', driver_version: '1.2.3', usb_vid: '1234', usb_pid: 'ABCD', capture_protocol: 'uvc_media_foundation', ptz_capability: 'device_auto_tracking_only', audio_capability: 'built_in_verified' },
        verified_at: '2026-08-26T08:00:00Z', updated_at: '2026-08-26T08:00:00Z',
      },
      {
        device_type: 'document_camera', system_id_hash: 'document-hash-012345', label: 'Desk Scanner',
        capabilities: { manufacturer: '', model: 'Desk Scanner', driver_version: '', usb_vid: '', usb_pid: '', capture_protocol: 'unverified', ptz_capability: 'fixed_no_control', audio_capability: 'none_verified' },
        verified_at: '2026-08-26T08:00:00Z', updated_at: '2026-08-26T08:00:00Z',
      },
    ]
    const qualification: DeviceQualificationSummary = {
      bindings_ready: true, profiles_ready: false, acceptance_status: 'pending', recent_runs: [],
      profile_missing: { camera: [], document_camera: ['manufacturer', 'driver_version', 'capture_protocol', 'hardware_identity'] },
      required: [
        { mode: 'dual_concurrency', status: 'not_run', completed_at: null },
        { mode: 'camera_stability', status: 'not_run', completed_at: null },
        { mode: 'document_stability', status: 'not_run', completed_at: null },
      ],
    }
    const save = vi.spyOn(localApi, 'saveDeviceBindings').mockResolvedValue(bindings)
    vi.spyOn(localApi, 'deviceQualification').mockResolvedValue(qualification)
    render(<DeviceSetupPanel initial={bindings} initialQualification={qualification} sessionActive={false} />)
    expect(screen.getByText(/汇博士 X90L/)).toHaveTextContent('SKU 10184877516348')
    expect(screen.queryByLabelText('观察摄像头厂商')).not.toBeInTheDocument()
    expect(screen.getByText(/待核对：厂商、驱动版本、接入协议/)).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('高拍仪厂商'), { target: { value: 'Scanner Vendor' } })
    fireEvent.click(screen.getByRole('button', { name: '保存能力档案' }))

    await waitFor(() => expect(save).toHaveBeenCalledTimes(1))
    const payload = save.mock.calls[0][0]
    expect(payload).toHaveLength(1)
    expect(payload[0]).toMatchObject({ device_type: 'document_camera', system_id_hash: 'document-hash-012345' })
    expect(payload[0].capabilities).toMatchObject({ manufacturer: 'Scanner Vendor', purchase_sku: '10184877516348', purchase_listing_name: expect.stringContaining('X90L') })
    expect(JSON.stringify(payload)).not.toContain('pnp_device_id')
  })
})

describe('SensorReleaseVerificationPanel', () => {
  it('requires and persists system checks for every device used by the selected session', async () => {
    const reviews: SensorReleaseReview[] = [{
      session_id: 'session-release-001',
      child_id: 'child-a',
      child_nickname: '小航',
      ended_at: '2026-08-26T10:20:00Z',
      sensor_release: {
        opened_leases: 2,
        active_leases: 0,
        released_leases: 2,
        application_release_confirmed: true,
        evidence_level: 'application_record_only',
        os_release_verified: false,
        release_verification: null,
        by_device: {
          camera: { opened: 1, active: 0, released: 1, last_release_reason: 'session_completed', last_released_at: '2026-08-26T10:20:00Z' },
          microphone: { opened: 1, active: 0, released: 1, last_release_reason: 'session_completed', last_released_at: '2026-08-26T10:20:00Z' },
        },
      },
    }]
    const save = vi.spyOn(localApi, 'recordSensorReleaseVerification').mockResolvedValue({
      result: 'passed',
      used_device_types: ['camera', 'microphone'],
      reopened_device_types: ['camera', 'microphone'],
      device_manager_healthy: true,
      windows_privacy_indicator_clear: true,
      physical_indicator_status: 'clear',
      platform_name: 'Windows',
      platform_version: '10.0.26100',
      verified_at: '2026-08-26T10:30:00Z',
    })

    render(<SensorReleaseVerificationPanel initial={reviews} platformEligible />)
    expect(screen.getByText('待系统级复核')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('checkbox', { name: /Windows 设备管理器/ }))
    fireEvent.click(screen.getByRole('checkbox', { name: /Windows 隐私使用指示/ }))
    fireEvent.click(screen.getByRole('checkbox', { name: /重新打开观察摄像头/ }))
    fireEvent.click(screen.getByRole('checkbox', { name: /重新打开麦克风/ }))
    fireEvent.change(screen.getByRole('textbox', { name: '备注（选填）' }), { target: { value: '已在相机和录音机重开' } })
    fireEvent.click(screen.getByRole('button', { name: '记录本次复核' }))

    await waitFor(() => expect(save).toHaveBeenCalledWith('session-release-001', expect.objectContaining({
      reopened_device_types: expect.arrayContaining(['camera', 'microphone']),
      device_manager_healthy: true,
      windows_privacy_indicator_clear: true,
      notes: '已在相机和录音机重开',
    })))
    expect(await screen.findByText('当次会话的 Windows 传感器释放已复核通过。')).toBeInTheDocument()
    expect(screen.getByText('系统释放已验证')).toBeInTheDocument()
  })

  it('keeps system-level recording disabled outside the target Windows runtime', () => {
    const review: SensorReleaseReview = {
      session_id: 'session-release-non-windows', child_id: 'child-a', child_nickname: '小航', ended_at: '2026-08-26T10:20:00Z',
      sensor_release: {
        opened_leases: 1, active_leases: 0, released_leases: 1,
        application_release_confirmed: true, evidence_level: 'application_record_only', os_release_verified: false,
        release_verification: null,
        by_device: { camera: { opened: 1, active: 0, released: 1, last_release_reason: 'session_completed', last_released_at: '2026-08-26T10:20:00Z' } },
      },
    }
    const rendered = render(<SensorReleaseVerificationPanel initial={[review]} platformEligible={false} />)
    const panel = within(rendered.container)
    expect(panel.getByRole('button', { name: '记录本次复核' })).toBeDisabled()
    expect(panel.getByText(/不能登记系统级通过/)).toBeInTheDocument()
  })
})

describe('WeeklyGoalsPanel', () => {
  it('shows persisted progress and the stop-after-completion boundary', () => {
    render(<WeeklyGoalsPanel goals={[{
      weekly_goal_id: 'weekly-pet-001',
      child_id: 'child-a',
      week_start: '2026-08-24',
      week_end: '2026-08-30',
      track: 'pet',
      target_minutes: 50,
      target_sessions: 5,
      completed_minutes: 50,
      completed_sessions: 5,
      remaining_minutes: 0,
      remaining_sessions: 0,
      status: 'completed',
      generator: 'weekly_goal_rules_v1',
      knowledge_version: 'starter_v2',
      created_at: '2026-08-24T00:00:00Z',
      updated_at: '2026-08-26T00:00:00Z',
    }]} />)

    expect(screen.getByText('PET 听说读写')).toBeInTheDocument()
    expect(screen.getByText('5/5次 · 50/50分钟')).toBeInTheDocument()
    expect(screen.getByText('执行量达标')).toBeInTheDocument()
    expect(screen.getByText(/学习效果待验证/)).toBeInTheDocument()
    expect(screen.getByText(/排程说明尚未提供/)).toBeInTheDocument()
    expect(screen.getByLabelText('PET 听说读写分钟执行量100%')).toBeInTheDocument()
    expect(screen.queryByText('本周目标已完成')).not.toBeInTheDocument()
  })
})

const goalOutcomeFixture = (overrides: Partial<GoalLearningOutcome> = {}): GoalLearningOutcome => ({
  status: 'supported_only', next_action: 'fade_hint_then_check', reason: '最近正确作答仍使用提示。',
  track: 'pet', evidence_version: 'feedback-v1', evidence_fingerprint: 'fingerprint-private',
  window_days: 90, evidence_scope: 'current_valid_evidence', goal_attainment: 'not_established',
  mastery_claim_allowed: false, cloud_transmission_allowed: false,
  nodes: [{ knowledge_node_id: 'node-private', track: 'pet', subject: '英语', title: '阅读定位',
    status: 'supported_only', next_action: 'fade_hint_then_check', reason: '需要减少提示再验证。',
    evidence_refs: ['receipt-private'], independent_correct: 7, attempt_count: 9,
    retention_success_count: 1, distinct_items: 5, independent_distinct_items: 2 }],
  ...overrides,
})

const goalCycleFixture = (overrides: Partial<GoalProgramCycle> = {}): GoalProgramCycle => ({
  cycle_id: 'goal-cycle-private', program_id: 'goal-program-private', child_id: 'child-a',
  track: 'pet_path', title: 'PET 阶段目标', target_statement: '持续发展英语能力。',
  target_horizon: {}, deadline: null, source_type: 'pet_daily',
  week_start: '2026-08-31', week_end: '2026-09-06', target_minutes: 50, target_sessions: 5,
  completed_minutes: 50, completed_sessions: 5, remaining_minutes: 0, remaining_sessions: 0,
  status: 'completed', evidence_watermark: { evidence_items: 5, latest: null },
  focus: { subject: '英语', title: '阅读定位', reason: '最近复测需要重新核对阅读步骤。' },
  ...overrides,
})

function mockGoalPanel(cycles: GoalProgramCycle[], feedback?: LongitudinalChildModel['payload']['learning']['reliable_feedback']): void {
  vi.spyOn(localApi, 'childModelCorrections').mockResolvedValue([])
  vi.spyOn(localApi, 'specializedRoadmaps').mockResolvedValue([])
  vi.spyOn(localApi, 'goalProgramCycles').mockResolvedValue({ items: cycles, next_daily_action: null })
  vi.spyOn(localApi, 'longitudinalChildModel').mockResolvedValue({
    snapshot_id: 'model-private', child_id: 'child-a', model_version: 'model-v1',
    evidence_fingerprint: 'model-fingerprint-private', is_current: true, created_at: '2026-09-01T08:00:00Z',
    payload: {
      as_of: '2026-09-01T08:00:00Z', evidence_level: feedback?.nodes.length === 0 ? 'cold_start' : 'established',
      learning: { evidenced_node_count: 12, subjects: [{ subject: '英语', node_count: 12, weak_nodes: 2,
        stable_nodes: 10, due_reviews: 1, average_mastery: .9, average_hint_dependency: .1, average_confidence: .9 }],
        reliable_feedback: feedback },
      mistakes: { status_counts: {}, top_clusters: [] },
      execution: { window_days: 14, sessions: 6, planned_tasks: 8, completed_tasks: 8,
        deferred_tasks: 0, actual_minutes: 100, completion_rate: 1 },
      behavior: { observed_days: 0, signals: {} }, tutor_feedback: { total: 0, understood: 0, still_stuck: 0 },
      support_strategy: { explanation_pacing: 'standard_layered', task_segment_minutes: 15,
        mistake_priority: 'due_or_relapsed_first', require_independent_check: true, reason: '历史安排。' },
      limitations: ['记录覆盖不代表当前掌握。'],
    },
  })
}

describe('Goal progress evidence presentation', () => {
  afterEach(() => { cleanup(); vi.restoreAllMocks() })

  it('keeps completed legacy cycles explicitly unverified', async () => {
    mockGoalPanel([goalCycleFixture()])
    render(<ChildGrowthModelPanel childId="child-a" />)
    expect(await screen.findByText('执行量达标')).toBeInTheDocument()
    const cycle = within(screen.getByText('PET 阶段目标').closest('article')!)
    expect(cycle.getByText(/学习效果待验证/)).toBeInTheDocument()
    expect(cycle.getByText(/预算说明尚未提供/)).toBeInTheDocument()
    expect(cycle.getByText(/排程说明尚未提供/)).toBeInTheDocument()
    expect(cycle.queryByText(/已掌握|本周达标|本周目标已完成/)).not.toBeInTheDocument()
  })

  it('shows separate outcome, focus and exhausted budget when no next action exists', async () => {
    mockGoalPanel([goalCycleFixture({ status: 'on_track', completed_sessions: 3, remaining_sessions: 2,
      participation_status: 'on_track', learning_outcome: goalOutcomeFixture(), budget_status: 'exhausted',
      scheduling_status: 'budget_exhausted', scheduling_reason: '参与次数尚差2次，不自动加时，待家长调整节奏。' })])
    render(<ChildGrowthModelPanel childId="child-a" />)
    expect(await screen.findByText('执行进度正常')).toBeInTheDocument()
    expect(screen.getByText(/仍需提示支持/)).toBeInTheDocument()
    expect(screen.getByText(/减少提示，再检查独立作答/)).toBeInTheDocument()
    expect(screen.getByText(/最近复测需要重新核对阅读步骤/)).toBeInTheDocument()
    expect(screen.getByText(/本周分钟预算已用尽/)).toBeInTheDocument()
    expect(screen.getByText(/参与次数尚差2次/)).toBeInTheDocument()
    expect(screen.getByText(/不等同于本周新增提升/)).toBeInTheDocument()
    expect(screen.queryByText(/下一个可执行行动/)).not.toBeInTheDocument()
    expect(document.body.textContent).not.toMatch(/fingerprint-private|receipt-private|node-private|supported_only|budget_exhausted/)
  })

  it.each([
    ['recheck_needed', '作答仍需核对'], ['supported_only', '仍需提示支持'],
    ['independent_observed', '已观察到独立作答'], ['retention_observed', '已观察到延迟保持'],
    ['insufficient_evidence', '证据不足，学习效果待验证'],
  ] as const)('renders weekly %s independently of activity completion', (status, label) => {
    const cycle = goalCycleFixture({ participation_status: 'quota_met', learning_outcome: goalOutcomeFixture({ status }),
      budget_status: 'exhausted', scheduling_status: 'quota_met', scheduling_reason: '执行达标不代表学习达成。' })
    const goal: WeeklyGoal = { ...cycle, weekly_goal_id: 'weekly-private', track: 'pet',
      generator: 'rules', knowledge_version: 'v1', created_at: '', updated_at: '' }
    render(<WeeklyGoalsPanel goals={[goal]} />)
    expect(screen.getByText('执行量达标')).toBeInTheDocument()
    expect(screen.getByText(new RegExp(label))).toBeInTheDocument()
    expect(screen.getByText(/停止自动追加/)).toBeInTheDocument()
    expect(document.body.textContent).not.toMatch(/本周目标已完成|fingerprint-private|receipt-private/)
  })

  it('shows historical cutoff and replaces reasons on evidence refresh', async () => {
    const initial = goalCycleFixture({ learning_outcome: goalOutcomeFixture({
      evidence_scope: 'as_of_week_end', as_of: '2026-08-30T23:59:59.999999+00:00',
    }) })
    mockGoalPanel([initial])
    render(<ChildGrowthModelPanel childId="child-a" />)
    expect(await screen.findByText(/证据截止 2026-08-30（UTC）/)).toBeInTheDocument()
    vi.mocked(localApi.goalProgramCycles).mockResolvedValue({ items: [{ ...initial,
      focus: { title: null, status: 'needs_baseline', reason: '旧来源已撤回，重新建立基线。' },
      learning_outcome: goalOutcomeFixture({ status: 'insufficient_evidence', next_action: 'needs_baseline',
        reason: '有效证据已不足。', nodes: [] }),
    }], next_daily_action: null })
    fireEvent.click(screen.getByRole('button', { name: '更新证据' }))
    expect(await screen.findByText(/旧来源已撤回，重新建立基线/)).toBeInTheDocument()
    expect(screen.queryByText(/最近复测需要重新核对阅读步骤/)).not.toBeInTheDocument()
    expect(screen.queryByText(/证据截止 2026-08-30/)).not.toBeInTheDocument()
  })

  it('labels raw aggregates historical and expands independently qualified evidence', async () => {
    const outcome = goalOutcomeFixture()
    mockGoalPanel([], { version: 'v1', evidence_fingerprint: 'feedback-private', window_days: 90,
      status: 'evidence_available', nodes: outcome.nodes })
    render(<ChildGrowthModelPanel childId="child-a" />)
    expect(await screen.findByText(/历史记录覆盖较多/)).toBeInTheDocument()
    expect(screen.getByRole('columnheader', { name: '历史较高估计' })).toBeInTheDocument()
    expect(screen.getByText('历史估计，仅供回看，不代表当前掌握')).toBeInTheDocument()
    expect(screen.queryByText('稳定节点')).not.toBeInTheDocument()
    expect(screen.queryByText(/已有较稳定证据/)).not.toBeInTheDocument()
    const summary = screen.getByText('有效作答证据（1 个知识点）')
    expect(summary.closest('details')).not.toHaveAttribute('open')
    fireEvent.click(summary)
    expect(summary.closest('details')).toHaveAttribute('open')
    expect(screen.getByText('需要减少提示再验证。')).toBeInTheDocument()
    expect(screen.getByText(/下一步：减少提示，再检查独立作答/)).toBeInTheDocument()
    expect(screen.getByText(/独立答对的不同题目：2 道；延迟保持成功：1 次。同题重复不算迁移/)).toBeInTheDocument()
    expect(document.body.textContent).not.toMatch(/node-private|receipt-private|feedback-private|independent_distinct_items/)
  })

  it.each([true, false])('keeps missing or empty reliable feedback unverified (empty=%s)', async (empty) => {
    mockGoalPanel([], empty ? { version: 'v1', evidence_fingerprint: 'none-private', window_days: 90,
      status: 'needs_baseline', nodes: [] } : undefined)
    render(<ChildGrowthModelPanel childId="child-a" />)
    const summary = await screen.findByText(empty ? '有效作答证据（0 个知识点）' : '有效作答证据（待提供）')
    fireEvent.click(summary)
    expect(screen.getByText(empty ? /尚无符合条件的有效作答/ : /尚未提供有效作答分析，学习效果待验证/)).toBeInTheDocument()
    if (empty) expect(screen.getByText(/冷启动，记录不足/)).toBeInTheDocument()
    expect(screen.queryByText('已观察到独立作答')).not.toBeInTheDocument()
  })
})

const strategy = (eligibleTurns: number): StrategyExperiment => ({
  experiment_id: 'strategy-001',
  child_id: 'child-a',
  strategy_key: 'tutor_hint_pacing',
  baseline_version: 'tutor_layered_v2',
  candidate_version: 'tutor_brief_reflective_v1',
  status: 'shadow',
  replay_metrics: { tutor_feedback_count: 5, tutor_understood_rate: 0.8, eligible_tutor_turns: 5, allowlist_passed: true },
  shadow_metrics: { eligible_turns: eligibleTurns, feedback_count: 0, understood_rate: null, policy_replacements: 0 },
  active_metrics: {},
  phase_metrics: { eligible_turns: eligibleTurns, feedback_count: 0, understood_rate: null, policy_replacements: 0 },
  rollback_reason: null,
  frozen_boundaries: ['提示等级与答案边界', '教育安全规则', '隐私上传规则', '家庭时长与家长约束'],
  transitions: [],
  created_at: '2026-08-26T08:00:00Z',
  updated_at: '2026-08-26T08:00:00Z',
  activated_at: null,
  ended_at: null,
})

describe('StrategyEvolutionPanel', () => {
  afterEach(() => vi.restoreAllMocks())

  it('keeps activation disabled until three shadow turns and persists activation through the API', async () => {
    const activate = vi.spyOn(localApi, 'activateStrategy').mockResolvedValue({ ...strategy(3), status: 'active' })
    vi.spyOn(localApi, 'strategyExperiments').mockResolvedValue([{ ...strategy(3), status: 'active' }])
    const { rerender } = render(<StrategyEvolutionPanel childId="child-a" initial={[strategy(2)]} />)

    expect(screen.getByRole('button', { name: '启用候选' })).toBeDisabled()
    expect(screen.getByText('还需1个适用讲题回合才能启用。影子期不改变孩子收到的内容。')).toBeInTheDocument()

    rerender(<StrategyEvolutionPanel childId="child-a" initial={[strategy(3)]} />)
    fireEvent.click(screen.getByRole('button', { name: '启用候选' }))

    await waitFor(() => expect(activate).toHaveBeenCalledWith('strategy-001'))
    expect(await screen.findByText('候选策略已启用，系统会持续监测真实反馈。')).toBeInTheDocument()
    expect(screen.getByText('已启用')).toBeInTheDocument()
  })
})

const configuredModel: BootstrapState['model'] = {
  provider: 'qwen', model: 'qwen-plus', configured: true,
  fallback_provider: 'deepseek', fallback_model: 'deepseek-chat', fallback_configured: true,
}

const modelCall = (overrides: Partial<ModelCallAudit> = {}): ModelCallAudit => ({
  purpose: 'tutor_hint_level_2',
  provider: 'deepseek',
  model: 'deepseek-chat',
  status: 'completed',
  latency_ms: 820,
  prompt_tokens: 120,
  completion_tokens: 45,
  input_price_cny_per_million: 2,
  output_price_cny_per_million: 8,
  estimated_cost_cny: 0.0006,
  verification_status: 'not_applicable_hint',
  safety_status: 'passed',
  fallback_used: 1,
  error_code: null,
  privacy_summary: {
    policy_version: 'cloud_text_privacy_v1', transmitted: true,
    redaction_count: 2, redaction_types: ['known_child_name', 'mobile_phone'],
    transmitted_recall_count: 1, omitted_sensitive_context_count: 0,
    blocked_categories: [],
  },
  created_at: '2026-08-26T08:00:00Z',
  completed_at: '2026-08-26T08:00:01Z',
  ...overrides,
})

describe('ModelOperationsPanel', () => {
  afterEach(() => { cleanup(); vi.restoreAllMocks() })

  it('shows unified daily limits and saves background-task enforcement', async () => {
    vi.spyOn(localApi, 'modelCalls').mockResolvedValue([])
    vi.spyOn(localApi, 'modelPricing').mockResolvedValue([])
    const runtimeStatus = {
      policy: { enforcement_mode: 'monitor' as const, daily_cost_limit_cny: 20, daily_token_limit: 2_000_000, failure_threshold: 4, cooldown_seconds: 120, updated_at: '2026-09-02T08:00:00Z' },
      today: { calls: 12, tokens: 3200, estimated_cost_cny: 0.18, failures: 1, fallbacks: 2, cost_limit_reached: false, token_limit_reached: false },
      routes: [{ provider: 'deepseek', model: 'deepseek-chat', status: 'healthy' as const, consecutive_failures: 0, last_error_code: null, blocked_until: null, last_success_at: '2026-09-02T08:00:00Z', updated_at: '2026-09-02T08:00:00Z' }],
      purpose_quality: [{ purpose: 'mistake_variant_generation', calls: 6, failures: 1, rejected: 3, fallbacks: 1, average_latency_ms: 820, estimated_cost_cny: 0.03, bad_result_rate: 0.5, state: 'blocked' as const }],
    }
    vi.spyOn(localApi, 'modelRuntimePolicy').mockResolvedValue(runtimeStatus)
    const save = vi.spyOn(localApi, 'saveModelRuntimePolicy').mockImplementation(async (values) => ({
      ...runtimeStatus, policy: { ...values, updated_at: '2026-09-02T09:00:00Z' },
    }))
    render(<ModelOperationsPanel childId="child-a" initialModel={configuredModel} />)

    expect(await screen.findByText(/今日 12 次调用/)).toBeInTheDocument()
    expect(screen.getByText(/deepseek \/ deepseek-chat：正常/)).toBeInTheDocument()
    expect(screen.getByText(/质量门已暂停后台调用/)).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('模型上限模式'), { target: { value: 'enforce' } })
    fireEvent.change(screen.getByLabelText('每日模型费用上限'), { target: { value: '8' } })
    fireEvent.click(screen.getByRole('button', { name: '保存运行策略' }))

    await waitFor(() => expect(save).toHaveBeenCalledWith(expect.objectContaining({
      enforcement_mode: 'enforce', daily_cost_limit_cny: 8,
    })))
    expect(await screen.findByText(/只延后后台生成/)).toBeInTheDocument()
  })

  it('shows sanitized call evidence, saves pricing, and reloads records when the child changes', async () => {
    const loadCalls = vi.spyOn(localApi, 'modelCalls')
      .mockResolvedValueOnce([
        modelCall(),
        modelCall({
          purpose: 'companion_dialogue', provider: 'qwen', model: 'qwen-fast',
          fallback_used: 0, verification_status: 'partially_verified', estimated_cost_cny: 0.0002,
          privacy_summary: {
            policy_version: 'cloud_text_privacy_v1', transmitted: true,
            redaction_count: 0, redaction_types: [], transmitted_recall_count: null,
            omitted_sensitive_context_count: 0, blocked_categories: [],
          },
        }),
        modelCall({
          purpose: 'tutor_privacy_block', provider: 'qwen', model: 'qwen-plus',
          status: 'failed', latency_ms: 0, prompt_tokens: null, completion_tokens: null,
          estimated_cost_cny: null, verification_status: 'rejected', safety_status: 'blocked',
          fallback_used: 0, error_code: 'SENSITIVE_TEXT_LOCAL_ONLY',
          privacy_summary: {
            policy_version: 'cloud_text_privacy_v1', transmitted: false,
            redaction_count: 0, redaction_types: [], transmitted_recall_count: null,
            omitted_sensitive_context_count: 0, blocked_categories: ['self_harm'],
          },
          created_at: '2026-08-26T08:01:00Z', completed_at: '2026-08-26T08:01:00Z',
        })
      ])
      .mockResolvedValueOnce([])
    vi.spyOn(localApi, 'modelPricing').mockResolvedValue([{
      provider: 'qwen', model: 'qwen-plus', input_price_cny_per_million: 3,
      output_price_cny_per_million: 9, currency: 'CNY', source: 'parent_configured',
      updated_at: '2026-08-26T07:00:00Z',
    }])
    vi.spyOn(localApi, 'modelRuntimePolicy').mockResolvedValue({
      policy: { enforcement_mode: 'monitor', daily_cost_limit_cny: 20, daily_token_limit: 2_000_000, failure_threshold: 4, cooldown_seconds: 120, updated_at: '2026-08-26T07:00:00Z' },
      today: { calls: 3, tokens: 165, estimated_cost_cny: 0.0008, failures: 1, fallbacks: 1, cost_limit_reached: false, token_limit_reached: false }, routes: [], purpose_quality: [],
    })
    const savePricing = vi.spyOn(localApi, 'saveModelPricing').mockResolvedValue({
      provider: 'qwen', model: 'qwen-plus', input_price_cny_per_million: 4,
      output_price_cny_per_million: 10, currency: 'CNY', source: 'parent_configured',
      updated_at: '2026-08-26T09:00:00Z',
    })
    const { rerender } = render(<ModelOperationsPanel key="child-a" childId="child-a" initialModel={configuredModel} />)

    expect(await screen.findByText('讲题第2级提示')).toBeInTheDocument()
    expect(screen.getByText('短陪伴对话')).toBeInTheDocument()
    expect(screen.getByText('deepseek / deepseek-chat · 已降级')).toBeInTheDocument()
    expect(screen.getAllByText('¥0.000600').length).toBeGreaterThan(0)
    expect(screen.getByText((_, element) => element?.tagName === 'SMALL' && Boolean(element.textContent?.includes('安全通过 · 核验不适用')))).toBeInTheDocument()
    expect(screen.getByText((_, element) => element?.tagName === 'SMALL' && Boolean(element.textContent?.includes('脱敏2项 · 同学科回忆1条')))).toBeInTheDocument()
    expect(screen.getByText('敏感文本本地阻断')).toBeInTheDocument()
    expect(screen.getByText((_, element) => element?.tagName === 'SMALL' && Boolean(element.textContent?.includes('云发送已阻断')))).toBeInTheDocument()
    expect(screen.queryByText('这道题怎么做')).not.toBeInTheDocument()

    fireEvent.change(screen.getByLabelText('输入单价'), { target: { value: '4' } })
    fireEvent.change(screen.getByLabelText('输出单价'), { target: { value: '10' } })
    fireEvent.click(screen.getByRole('button', { name: '保存单价依据' }))
    await waitFor(() => expect(savePricing).toHaveBeenCalledWith({
      provider: 'qwen', model: 'qwen-plus',
      input_price_cny_per_million: 4, output_price_cny_per_million: 10,
    }))

    rerender(<ModelOperationsPanel key="child-b" childId="child-b" initialModel={configuredModel} />)
    expect(await screen.findByText('当前孩子还没有模型调用。讲题后会在这里出现可追溯记录。')).toBeInTheDocument()
    expect(screen.queryByText('讲题第2级提示')).not.toBeInTheDocument()
    expect(loadCalls).toHaveBeenLastCalledWith('child-b')
  })
})
