import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { webcrypto } from 'node:crypto'
import { LearningRubricReviewPanel } from './LearningRubricReviewPanel'
import { ApiError } from './api'

const local = { local_only: true, cloud_transmission_allowed: false }
const child = { child_id: 'child-a', nickname: '孩子甲' }
const labels: Record<string, string> = { task: '任务完成', coherence: '连贯', language: '语言', audio_text_alignment: '原音与回答核对', intelligibility: '可理解性', pronunciation: '发音', prosody: '重音与语调', fluency: '口语连贯性' }
const audioNames = Object.keys(labels).slice(3)
async function fixture(missing = false) {
  const bytes = new ArrayBuffer(32044)
  const sha = Array.from(new Uint8Array(await webcrypto.subtle.digest('SHA-256', bytes))).map(v => v.toString(16).padStart(2, '0')).join('')
  const dimensions = Object.keys(labels).map(dimension_id => ({ dimension_id, criterion: labels[dimension_id], max_score: 3, pass_score: 2, score_anchors: { '2': '满足该维度要求' }, score: null, response_quotes: [], rationale: 'not_assessed' }))
  const binding = { ...local, child_id: child.child_id, assessment_id: 'assessment-a', session_id: 'session-a', media_id: 'media-a', binding_version: 'original-v1', audio: { sha256: sha, frames: 16000, sample_rate: 16000 }, dimensions_required: audioNames }
  let assessment: any = { ...local, assessment_id: 'assessment-a', child_id: child.child_id, fingerprint: 'frozen-text-v1', question: 'Describe reading.', response: 'I read books.', subject: '英语', profile: 'english_open', source_kind: 'local_open_review', source_status: 'current', score_status: 'candidate', review_revision: 0, review: null, dimensions: dimensions.slice(0, 3), criteria: ['Answer clearly'], responded_at: '2026-09-07T10:00:00Z', material: { status: 'bound' } }
  const history: any[] = []
  const detail = () => ({ ...local, assessment, history, audio_review: { ...local, required: true, profile: 'pet_speaking_audio_v1', dimensions, bindings: missing ? [] : [binding], blockers: missing ? ['missing'] : [] }, qualification: { ...local, assessment_id: 'assessment-a', eligible_for_mastery_gate: history.length > 0, automatic_certification: false, receipt: history.length ? { assessment_id: 'assessment-a', review_revision: assessment.review_revision } : null } })
  const results = new Map<string, unknown>()
  const request = vi.fn(async (path: string, init?: RequestInit): Promise<unknown> => {
    if (!init?.method) return path.includes('?') ? { ...local, child_id: child.child_id, items: [assessment], total: 1, offset: 0 } : detail()
    const body = JSON.parse(String(init.body))
    if (results.has(body.client_action_id)) return results.get(body.client_action_id)
    const event = { ...local, ...body, assessment_id: 'assessment-a', review_revision: assessment.review_revision + 1, created_at: '2026-09-07T10:01:00Z', score_status: 'reviewed_human', reviewer_role: 'parent', passed: true }
    history.push(event); assessment = { ...assessment, review: event, score_status: 'reviewed_human', review_revision: event.review_revision }
    const result = { ...detail(), event }; results.set(body.client_action_id, result); return result
  })
  const requestAudio = vi.fn(async () => bytes)
  return { request, requestAudio, detail, binding }
}
afterEach(() => { cleanup(); localStorage.clear(); vi.restoreAllMocks(); vi.unstubAllGlobals() })

it('blocks missing original audio and missing host audio adapter instead of using text', async () => {
  const f = await fixture(true)
  render(<LearningRubricReviewPanel child={child} request={f.request} />)
  expect(await screen.findByText('缺少当前题目与回答的有效原音，口语审核已阻塞。')).toBeInTheDocument()
  expect(screen.getByText('本地原音读取尚未接通，不能仅凭文字认证口语。')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: '保存评分审核' })).toBeDisabled()
  expect(screen.getByLabelText('发音评分')).toHaveValue('')
})

it('persists all human scores and exact original audio frames then retries a lost receipt after remount', async () => {
  vi.stubGlobal('crypto', webcrypto)
  vi.stubGlobal('OfflineAudioContext', class { async decodeAudioData() { return { numberOfChannels: 1, length: 16000, sampleRate: 16000 } } })
  vi.stubGlobal('URL', class extends URL { static createObjectURL = vi.fn(() => 'blob:original'); static revokeObjectURL = vi.fn() })
  vi.spyOn(HTMLMediaElement.prototype, 'pause').mockImplementation(() => {})
  const f = await fixture()
  const original = f.request.getMockImplementation()!
  let drop = true
  f.request.mockImplementation(async (path, init) => { const result = await original(path, init); if (init?.method && drop) { drop = false; throw new ApiError('Lost response') } return result })
  const view = render(<LearningRubricReviewPanel child={child} request={f.request} requestAudio={f.requestAudio} />)
  fireEvent.change(await screen.findByLabelText('选择原始录音'), { target: { value: 'media-a' } })
  fireEvent.click(screen.getByRole('button', { name: '读取原音' }))
  const audio = await screen.findByLabelText('本次口语原音')
  expect(audio).not.toHaveAttribute('autoplay')
  for (const [id, name] of Object.entries(labels)) {
    fireEvent.change(screen.getByLabelText(`${name}评分`), { target: { value: '2' } })
    fireEvent.change(screen.getByLabelText(`${name}评分理由`), { target: { value: 'Reviewed original evidence' } })
    if (audioNames.includes(id)) {
      fireEvent.change(screen.getByLabelText(`${name}片段起点（秒）`), { target: { value: '0' } })
      fireEvent.change(screen.getByLabelText(`${name}片段终点（秒）`), { target: { value: '1' } })
      fireEvent.change(screen.getByLabelText(`${name}实际听到的内容`), { target: { value: 'I read books.' } })
    } else fireEvent.change(screen.getByLabelText(`${name}回答摘录`), { target: { value: 'I read books.' } })
  }
  fireEvent.change(screen.getByLabelText('目标1回答摘录'), { target: { value: 'I read books.' } })
  fireEvent.change(screen.getByLabelText('独立完成情况'), { target: { value: 'yes' } })
  fireEvent.change(screen.getByLabelText('提示等级'), { target: { value: '0' } })
  fireEvent.change(screen.getByLabelText('审核说明'), { target: { value: 'Checked original audio' } })
  expect(screen.getByRole('button', { name: '保存评分审核' })).toBeDisabled()
  fireEvent.ended(audio)
  fireEvent.click(screen.getByRole('button', { name: '保存评分审核' }))
  await screen.findByRole('button', { name: '重试原审核' })
  const first = f.request.mock.calls.find(([, init]) => init?.method === 'POST')![1]!.body
  const saved = JSON.parse(String(first))
  expect(saved.review_profile).toBe('pet_speaking_audio_v1')
  expect(saved.audio_evidence.review.observations).toHaveLength(5)
  expect(saved.audio_evidence.review.observations[0]).toMatchObject({ start_frame: 0, end_frame: 16000 })
  expect(saved.dimensions).toHaveLength(8)
  view.unmount()
  render(<LearningRubricReviewPanel child={child} request={f.request} requestAudio={f.requestAudio} />)
  fireEvent.click(await screen.findByRole('button', { name: '重试原审核' }))
  await waitFor(() => expect(screen.queryByRole('button', { name: '重试原审核' })).not.toBeInTheDocument())
  expect(f.request.mock.calls.filter(([, init]) => init?.method === 'POST')[1][1]!.body).toBe(first)
  expect(f.requestAudio).toHaveBeenCalledTimes(1)
  expect(f.detail().history).toHaveLength(1)
})

it('aborts the exact recording download on unmount and never revives it later', async () => {
  const f = await fixture()
  let done!: (value: ArrayBuffer) => void
  let signal: AbortSignal | undefined
  const requestAudio = vi.fn((_path: string, init?: RequestInit) => { signal = init?.signal as AbortSignal; return new Promise<ArrayBuffer>(resolve => { done = resolve }) })
  const view = render(<LearningRubricReviewPanel child={child} request={f.request} requestAudio={requestAudio} />)
  fireEvent.change(await screen.findByLabelText('选择原始录音'), { target: { value: 'media-a' } })
  fireEvent.click(screen.getByRole('button', { name: '读取原音' }))
  view.unmount()
  expect(signal?.aborted).toBe(true)
  await act(async () => done(new ArrayBuffer(44)))
  expect(screen.queryByLabelText('本次口语原音')).not.toBeInTheDocument()
})
