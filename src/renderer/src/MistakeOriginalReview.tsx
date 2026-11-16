import { useEffect, useRef, useState } from 'react'
import { Camera, Mic, ScanLine } from 'lucide-react'
import { ApiError, request } from './api'
import type { MistakeLearningState, OriginalMistakeModality, OriginalMistakeResult } from './mistakeLearningTypes'

export type OriginalReviewRequest = (request: { record: MistakeLearningState; method: OriginalMistakeModality; signal: AbortSignal }) => Promise<MistakeLearningState>

export function reviewLabel(record: MistakeLearningState): string {
  const original = record.original_review
  if (original?.status === 'not_mastered') return '未掌握'
  if (original?.status === 'retest_pending') return '待复测'
  return record.review_due ? '待复习' : ''
}

type Props = {
  record: MistakeLearningState
  active: boolean
  unavailable?: boolean
  onRequest?: OriginalReviewRequest
  onResult: (state: MistakeLearningState) => void
}

export function OriginalReviewControls({ record, active, unavailable, onRequest, onResult }: Props): React.JSX.Element {
  const [method, setMethod] = useState<OriginalMistakeModality | null>(null)
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState<string | null>(null)
  const requestRef = useRef<AbortController | null>(null)
  const current = useRef({ active, source: record.source_revision })
  current.current = { active, source: record.source_revision }
  useEffect(() => {
    setBusy(false);setMethod(null)
    return () => { requestRef.current?.abort();requestRef.current = null }
  }, [active, record.source_revision])
  useEffect(() => {
    const pause = (): void => { requestRef.current?.abort();requestRef.current = null;setBusy(false);setMethod(null) }
    window.addEventListener('hsc-privacy-pause', pause)
    return () => window.removeEventListener('hsc-privacy-pause', pause)
  }, [])

  async function submit(modality: OriginalMistakeModality): Promise<void> {
    if (!onRequest || !active || unavailable || requestRef.current) return
    const controller = new AbortController()
    requestRef.current = controller
    setMethod(modality);setBusy(true);setNotice(null)
    try {
      const result = await onRequest({ record, method: modality, signal: controller.signal })
      if (controller.signal.aborted || !current.current.active || current.current.source !== record.source_revision) return
      if (result.mistake_id !== record.mistake_id || result.child_id !== record.child_id || result.source_revision !== record.source_revision) {
        setNotice('当前题目已有更新，请重新核对再作答。');return
      }
      if (result.review_result?.judgement === 'uncertain') {
        setNotice(result.review_result.feedback || '这次没能辨认清楚，可以再试一次。')
      } else if (!result.review_result) {
        setNotice('这次判题暂未确认，请刷新错题本后再试。')
      } else {
        setMethod(null)
      }
      onResult(result)
    } catch (error) {
      if (!controller.signal.aborted && current.current.active && current.current.source === record.source_revision) {
        setNotice(error instanceof Error ? error.message : '这次暂时没能完成判题，请再试一次。')
      }
    } finally {
      if (requestRef.current === controller) { requestRef.current = null;setBusy(false) }
    }
  }

  const review = record.original_review
  const ready = active && !unavailable && Boolean(onRequest)
  return <div className="cloud-original-controls">
    {review?.last_result && <OriginalReviewOutcome result={review.last_result} wrongCount={review.wrong_count}/>}
    <div className="cloud-learning-controls">
      <button type="button" className="primary-button" disabled={!ready || busy} onClick={() => { setMethod('paper');setNotice(null) }}><ScanLine size={17}/>在草稿纸上重做</button>
      <button type="button" className="secondary-button" disabled={!ready || busy} onClick={() => void submit('voice')}><Mic size={17}/>用语音讲解</button>
    </div>
    {!onRequest && <p className="cloud-list-note">进入当前学习后，可以使用高拍仪或小知的语音来判题。</p>}
    {method === 'paper' && <div className="cloud-original-capture"><p>写清「答案：…」，把草稿纸正放在画面中央。等画面稳定，再请小知检查。</p><button type="button" className="secondary-button" disabled={!ready || busy} onClick={() => void submit('paper')}><Camera size={16}/>拍草稿并判断</button></div>}
    {busy && <div className="cloud-original-capture"><p role="status">{method === 'paper' ? '正在识别草稿并判断…' : '请说出你的答案和思路，小知正在听…'}</p><button type="button" className="secondary-button" onClick={() => { requestRef.current?.abort();requestRef.current = null;setBusy(false);setMethod(null) }}>取消</button></div>}
    {notice && <p className="cloud-learning-notice" role="status">{notice}</p>}
  </div>
}

export function OriginalReviewOutcome({ result, wrongCount, showHint = true }: { result: OriginalMistakeResult; wrongCount: number; showHint?: boolean }): React.JSX.Element {
  const label = result.judgement === 'correct' ? '独立答对 · 待复测' : result.judgement === 'incorrect' ? '这次还没做对 · 未掌握' : '请再试一次'
  return <div className="cloud-original-outcome" role="status"><strong>{label}{wrongCount > 0 ? ` · 累计答错 ${wrongCount} 次` : ''}</strong><p>{result.feedback}</p>{showHint && result.next_hint && <p>{result.next_hint}</p>}</div>
}

type Crop = { image_base64: string; media_type: 'image/jpeg'; width: number; height: number }
export function MistakeQuestionCrop({ record, active }: { record: MistakeLearningState; active: boolean }): React.JSX.Element {
  const [crop, setCrop] = useState<Crop | null>(null)
  const [status, setStatus] = useState<'loading' | 'ready' | 'empty' | 'failed'>('loading')
  const [retry, setRetry] = useState(0)
  const { child_id: childId, mistake_id: mistakeId, source_revision: revision, source: { has_question_crop: available } } = record
  useEffect(() => {
    setCrop(null)
    if (!active || !available) { setStatus('empty');return }
    const controller = new AbortController()
    setStatus('loading')
    const path = `/v1/children/${encodeURIComponent(childId)}/mistakes/${encodeURIComponent(mistakeId)}/question-crop`
    void request<Crop>(path, { signal: controller.signal, cache: 'no-store' }, false, true).then(value => {
      if (controller.signal.aborted) return
      if (value.media_type !== 'image/jpeg' || !Number.isSafeInteger(value.width) || !Number.isSafeInteger(value.height) || value.width < 1 || value.height < 1 || typeof value.image_base64 !== 'string' || value.image_base64.length > 2_800_000 || !/^\/9j\/[A-Za-z0-9+/]+={0,2}$/.test(value.image_base64)) throw Error('QUESTION_CROP_INVALID')
      setCrop(value);setStatus('ready')
    }).catch(error => {
      if (!controller.signal.aborted) setStatus(error instanceof ApiError && error.status === 404 ? 'empty' : 'failed')
    })
    return () => controller.abort()
  }, [active, available, childId, mistakeId, revision, retry])
  return <section className="cloud-question-crop"><h3>原题裁图</h3>
    {active && crop && status === 'ready' ? <details><summary>展开原图（可能有批改或旧作答）</summary><img src={`data:image/jpeg;base64,${crop.image_base64}`} width={crop.width} height={crop.height} alt="高拍仪保存的原题区域" onError={() => setStatus('failed')}/></details>
      : !available || status === 'empty' ? <p>这道题没有保存原题图片。</p>
      : status === 'failed' ? <p role="alert">暂时没有读到原题裁图。 <button type="button" className="secondary-button" onClick={() => setRetry(value => value + 1)}>重试</button></p>
      : <p role="status">正在读取原题裁图…</p>}
  </section>
}
