import { cleanup, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { LearningRubricReviewPanel } from './LearningRubricReviewPanel'

const child = { child_id: 'child-a', nickname: '孩子甲', grade: '四年级' }
const local = { local_only: true, cloud_transmission_allowed: false }
const ids = ['audio_text_alignment', 'intelligibility', 'pronunciation', 'prosody', 'fluency']
function fixture() {
  const assessment = { ...local, assessment_id: 'assessment-a', child_id: child.child_id, fingerprint: 'original-a',
    question: 'Describe this picture.', response: 'There is a tree.', subject: '英语', profile: 'english_response',
    source_kind: 'model_candidate', source_status: 'current', score_status: 'candidate', review_revision: 0, review: null,
    criteria: [], material: { status: 'unbound' }, responded_at: '2026-10-25T10:00:00Z',
    dimensions: [{ dimension_id: 'content', criterion: '说明图片内容', max_score: 3, pass_score: 2,
      score_anchors: { '0': '未作答', '1': '部分', '2': '符合', '3': '充分' }, score: null, response_quotes: [], rationale: 'not_assessed' }] }
  const audio: any = { ...local, status: 'calibrated_audio_dimensions', score_qualified: true,
    score_scope: 'original_audio_dimensions_only', mastery_claim_allowed: false, certification_claim_allowed: false,
    assessment_id: assessment.assessment_id, score_authority: 'typed_cpu_original_audio_projection_v1', covered_dimensions: [...ids],
    provenance: { child_id: child.child_id, selector: { assessment_id: assessment.assessment_id } },
    dimensions: Object.fromEntries(ids.map(id => [id, { score: 2, start_frame: 0, end_frame: 16000, abstain_reason: null }])) }
  const qualification = { ...local, assessment_id: assessment.assessment_id, score_qualified: false,
    eligible_for_mastery_gate: false, automatic_certification: false, receipt: null, original_audio_dimensions: audio }
  const request = vi.fn(async (path: string, init?: RequestInit) => {
    if (init?.method) throw Error('This read-only display must not submit or create authority')
    return path.includes('?') ? { ...local, child_id: child.child_id, items: [{ ...assessment, qualification }], total: 1, offset: 0 }
      : { ...local, assessment, qualification, history: [] }
  })
  return { assessment, audio, qualification, request }
}
describe('parent original-audio dimension qualification', () => {
  afterEach(() => { cleanup(); localStorage.clear(); vi.restoreAllMocks() })
  it('shows only the calibrated audio dimensions while the whole answer remains unqualified', async () => {
    const f = fixture()
    render(<LearningRubricReviewPanel child={child} request={f.request} />)
    const section = await screen.findByRole('region', { name: '原音维度评分' })
    expect(within(section).getAllByRole('listitem')).toHaveLength(5)
    for (const row of within(section).getAllByRole('listitem')) expect(row).toHaveTextContent('2 / 3分')
    expect(screen.getByText('这份评分当前不能用于独立掌握门槛。')).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: '待人工审核' })).toBeInTheDocument()
    expect(screen.queryByText('本次模型评分已通过校准核验。')).not.toBeInTheDocument()
    expect(section).toHaveTextContent('英语内容、整题得分、独立掌握和考试认证仍需各自证据')
    expect(f.request.mock.calls.every(([, init]) => !init?.method)).toBe(true)
  })
  it('does not display uncovered dimension scores', async () => {
    const f = fixture(); f.audio.covered_dimensions = ['fluency']
    render(<LearningRubricReviewPanel child={child} request={f.request} />)
    const section = await screen.findByRole('region', { name: '原音维度评分' })
    expect(within(section).getAllByRole('listitem')).toHaveLength(1)
    expect(section).toHaveTextContent('口语连贯性：2 / 3分')
  })
  it('re-reads revocation on reopen and never retains previous calibrated scores', async () => {
    const f = fixture(); const view = render(<LearningRubricReviewPanel child={child} request={f.request} />)
    await screen.findByText('本次原音的以下维度已通过校准核验。'); view.unmount()
    f.qualification.original_audio_dimensions = { status: 'unavailable', score_qualified: false,
      score_scope: 'original_audio_dimensions_only', mastery_claim_allowed: false, certification_claim_allowed: false } as any
    render(<LearningRubricReviewPanel child={child} request={f.request} />)
    await screen.findByText('本次原音尚无可用的校准评分。')
    expect(screen.queryByText('本次原音的以下维度已通过校准核验。')).not.toBeInTheDocument()
    expect(screen.getByRole('heading', { name: '待人工审核' })).toBeInTheDocument()
  })
  it.each([
    ['another child', (f: ReturnType<typeof fixture>) => { f.audio.provenance.child_id = 'child-b' }],
    ['another answer', (f: ReturnType<typeof fixture>) => { f.audio.assessment_id = 'assessment-b' }],
    ['another original', (f: ReturnType<typeof fixture>) => { f.audio.provenance.selector.assessment_id = 'assessment-b' }],
    ['stale source', (f: ReturnType<typeof fixture>) => { f.assessment.source_status = 'withdrawn' }],
    ['content scope escalation', (f: ReturnType<typeof fixture>) => { f.audio.covered_dimensions = ['content'] }],
    ['repeated dimensions', (f: ReturnType<typeof fixture>) => { f.audio.covered_dimensions = ['fluency', 'fluency'] }],
    ['score outside rubric', (f: ReturnType<typeof fixture>) => { f.audio.dimensions.fluency.score = 4 }],
    ['missing original frames', (f: ReturnType<typeof fixture>) => { f.audio.dimensions.fluency.end_frame = null }],
    ['mastery escalation', (f: ReturnType<typeof fixture>) => { f.audio.mastery_claim_allowed = true }],
    ['certification escalation', (f: ReturnType<typeof fixture>) => { f.audio.certification_claim_allowed = true }],
    ['nonlocal data', (f: ReturnType<typeof fixture>) => { f.audio.local_only = false }],
    ['reference pretending qualified', (f: ReturnType<typeof fixture>) => { f.audio.status = 'reference' }],
  ])('refuses %s before showing calibrated evidence or allowing review', async (_, corrupt) => {
    const f = fixture(); corrupt(f)
    render(<LearningRubricReviewPanel child={child} request={f.request} />)
    expect(await screen.findByRole('alert')).toHaveTextContent('原回答或审核版本已变化。请核对最新记录，旧决定不会自动用于新版本。')
    expect(screen.queryByRole('region', { name: '原音维度评分' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '保存评分审核' })).not.toBeInTheDocument()
  })
})
