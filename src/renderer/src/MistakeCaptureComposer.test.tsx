import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { localApi } from './api'
import { inferMistakeSubject, MistakeCaptureComposer } from './MistakeCaptureComposer'
import type { MistakeCaptureCandidate, MistakeItem, OcrArtifact } from './types'

const artifact: OcrArtifact = {
  artifact_id: 'artifact-1', extracted_text: '找出数列1、3、6、10的下一个数。', confidence: 0.91,
  local_path: null, status: 'completed', details: { lines: [], engine: 'local-test' }
}

const candidate: MistakeCaptureCandidate = {
  candidate_id: 'candidate-1', artifact_id: 'artifact-1', child_id: 'child-1', question_index: 0,
  question_text: artifact.extracted_text, region: null, confidence: 0.91, status: 'pending',
  mistake_id: null, created_at: '', updated_at: ''
}

describe('MistakeCaptureComposer', () => {
  afterEach(() => { cleanup(); vi.restoreAllMocks() })

  it('infers English from the photographed question instead of inheriting an unrelated math task', () => {
    expect(inferMistakeSubject('(C) 2. Would you like a banana?', '数学')).toBe('英语')
    expect(inferMistakeSubject('求一元二次方程的根', '数学')).toBe('数学')
  })

  it('stores the observed answer as pending scoring without asking the child to judge correctness', async () => {
    vi.spyOn(localApi, 'prepareMistakeCandidates').mockResolvedValue([candidate])
    vi.spyOn(localApi, 'mistakeKnowledgeCandidates').mockResolvedValue([{
      knowledge_node_id: 'node-oly-pattern', node_type: 'skill', track: 'competition_math',
      subject: '数学', grade_band: '四年级竞赛', code: 'OLY.P4.PATTERN',
      title: '数列规律与归纳', description: '观察变化，提出并验证规律。', match_score: 4
    }])
    const mistake = {
      mistake_id: 'mistake-1', child_id: 'child-1', cluster_id: 'cluster-1', diagnostic_item_id: null,
      source_evidence_id: null, subject: '数学', title: '数列规律与归纳', prompt_summary: artifact.extracted_text,
      submitted_answer: '12', source_type: 'document_camera', source_ref: artifact.artifact_id,
      confidence: 0.91, status: 'awaiting_confirmation', diagnosis_status: 'suspected', occurrence_count: 1,
      relapse_count: 0, confirmed_at: null, created_at: '', updated_at: '',
      knowledge_node: { knowledge_node_id: 'node-oly-pattern', title: '数列规律与归纳', code: 'OLY.P4.PATTERN', track: 'competition_math' },
      diagnosis: null, mastery_gate: null
    } as MistakeItem
    const captured = vi.spyOn(localApi, 'captureMistakeCandidate').mockResolvedValue({
      candidate: { ...candidate, status: 'saved', mistake_id: mistake.mistake_id }, mistake
    })

    render(<MistakeCaptureComposer childId="child-1" artifact={artifact} initialSubject="数学" onClose={vi.fn()} />)
    expect(await screen.findByText('数列规律与归纳')).toBeInTheDocument()
    expect(screen.getByLabelText('错题题目文字')).toHaveValue(artifact.extracted_text)
    expect(screen.getByRole('button', { name: '保存作答，等待评分' })).toBeDisabled()

    fireEvent.change(screen.getByLabelText('错题原答案'), { target: { value: '12' } })
    fireEvent.click(screen.getByRole('button', { name: '保存作答，等待评分' }))

    await screen.findByText('本页已整理')
    await waitFor(() => expect(captured).toHaveBeenCalledWith(
      'candidate-1',
      expect.objectContaining({ childId: 'child-1', knowledgeNodeId: 'node-oly-pattern', submittedAnswer: '12' })
    ))
  })

  it('moves through multiple persisted candidates one by one', async () => {
    const second = { ...candidate, candidate_id: 'candidate-2', question_index: 1, question_text: '2. 计算125×32。' }
    vi.spyOn(localApi, 'prepareMistakeCandidates').mockResolvedValue([candidate, second])
    vi.spyOn(localApi, 'mistakeKnowledgeCandidates').mockResolvedValue([])
    const skip = vi.spyOn(localApi, 'skipMistakeCaptureCandidate').mockResolvedValue({ ...candidate, status: 'skipped' })

    render(<MistakeCaptureComposer childId="child-1" artifact={artifact} initialSubject="数学" onClose={vi.fn()} />)
    expect(await screen.findByText('第 1 题，共 2 题')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '没有作答或识别有误' }))

    expect(await screen.findByText('第 2 题，共 2 题')).toBeInTheDocument()
    expect(skip).toHaveBeenCalledWith('candidate-1', 'child-1')
    expect(screen.getByLabelText('错题题目文字')).toHaveValue(second.question_text)
  })

  it('captures the current OCR candidate through answer and save voice actions', async () => {
    vi.spyOn(localApi, 'prepareMistakeCandidates').mockResolvedValue([candidate])
    vi.spyOn(localApi, 'mistakeKnowledgeCandidates').mockResolvedValue([{
      knowledge_node_id: 'node-oly-pattern', node_type: 'skill', track: 'competition_math',
      subject: '数学', grade_band: '四年级竞赛', code: 'OLY.P4.PATTERN',
      title: '数列规律与归纳', description: '观察变化，提出并验证规律。', match_score: 4
    }])
    const mistake = {
      mistake_id: 'mistake-voice', child_id: 'child-1', cluster_id: 'cluster-1', diagnostic_item_id: null,
      source_evidence_id: null, subject: '数学', title: '数列规律与归纳', prompt_summary: artifact.extracted_text,
      submitted_answer: '12', source_type: 'document_camera', source_ref: artifact.artifact_id,
      confidence: 0.91, status: 'awaiting_confirmation', diagnosis_status: 'suspected', occurrence_count: 1,
      relapse_count: 0, confirmed_at: null, created_at: '', updated_at: '',
      knowledge_node: { knowledge_node_id: 'node-oly-pattern', title: '数列规律与归纳', code: 'OLY.P4.PATTERN', track: 'competition_math' },
      diagnosis: null, mastery_gate: null
    } as MistakeItem
    const captured = vi.spyOn(localApi, 'captureMistakeCandidate').mockResolvedValue({
      candidate: { ...candidate, status: 'saved', mistake_id: mistake.mistake_id }, mistake
    })
    render(<MistakeCaptureComposer childId="child-1" artifact={artifact} initialSubject="数学" onClose={vi.fn()} />)
    await screen.findByText('数列规律与归纳')

    const answerHandled = vi.fn()
    await act(async () => {
      window.dispatchEvent(new CustomEvent('hsc-mistake-capture-voice-action', {
        detail: { action: 'set_answer', value: '12', resolve: answerHandled }
      }))
    })
    await waitFor(() => expect(screen.getByLabelText('错题原答案')).toHaveValue('12'))
    expect(answerHandled).toHaveBeenCalledWith(true)

    const saveHandled = vi.fn()
    await act(async () => {
      window.dispatchEvent(new CustomEvent('hsc-mistake-capture-voice-action', {
        detail: { action: 'save', resolve: saveHandled }
      }))
    })

    expect(await screen.findByText('本页已整理')).toBeInTheDocument()
    expect(saveHandled).toHaveBeenCalledWith(true)
    expect(captured).toHaveBeenCalledTimes(1)
  })
})
