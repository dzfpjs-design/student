import { useEffect, useState } from 'react'
import { ChevronLeft, ChevronRight, RefreshCw } from 'lucide-react'
import './ParentDocumentTrialPanel.css'

type Question = { candidate_id: string; question_text: string; answer_text: string | null; correctness: number | null; authority_type: string | null; mistake_id: string | null }
type Frame = { artifact_id: string; captured_at: string; reason: string; image_available: boolean; questions: Question[]; questions_has_more?: boolean }
export type DocumentTrialReview = { session_id: string; summary: { frame_count: number; correct_count: number; wrong_count: number; unscored_count: number; capture_failure_count: number }; findings: string[]; frames: Frame[]; has_more: boolean; questions: Question[] }
type Session = { session_id: string; started_at: string; status: string }
type Props = { childId: string; request: <T>(path: string, init?: RequestInit) => Promise<T>; readImage: (childId: string, artifactId: string, signal: AbortSignal) => Promise<Blob> }

export function ParentDocumentTrialPanel(props: Props): React.JSX.Element {
  return <ChildDocumentTrialPanel key={props.childId} {...props} />
}

function ChildDocumentTrialPanel({ childId, request, readImage }: Props): React.JSX.Element {
  const [sessions, setSessions] = useState<Session[]>([])
  const [sessionId, setSessionId] = useState('')
  const [offset, setOffset] = useState(0)
  const [refresh, setRefresh] = useState(0)
  const [review, setReview] = useState<DocumentTrialReview | null>(null)
  const [selected, setSelected] = useState<Frame | null>(null)
  const [image, setImage] = useState<string | null>(null)
  const [imageFailed, setImageFailed] = useState(false)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  useEffect(() => {
    let current = true
    setLoading(true); setError('')
    void request<{ items: Session[] }>(`/v1/parent/children/${childId}/document-trial/sessions`).then(result => {
      if (!current) return
      setSessions(result.items)
      setSessionId(value => result.items.some(item => item.session_id === value) ? value : result.items[0]?.session_id ?? '')
    }).catch(() => { if (current) setError('试用复盘暂时无法读取，请刷新重试。') })
      .finally(() => { if (current) setLoading(false) })
    return () => { current = false }
  }, [childId, request, refresh])
  useEffect(() => {
    let current = true
    setReview(null); setSelected(null)
    if (!sessionId) return
    setLoading(true)
    void request<DocumentTrialReview>(`/v1/parent/children/${childId}/document-trial/sessions/${sessionId}?offset=${offset}`).then(result => {
      if (!current) return
      setReview(result); setSelected(result.frames[0] ?? null)
    }).catch(() => { if (current) setError('该次学习的图片索引未能读取，请刷新重试。') })
      .finally(() => { if (current) setLoading(false) })
    return () => { current = false }
  }, [childId, request, sessionId, offset, refresh])
  useEffect(() => {
    const abort = new AbortController()
    let url: string | null = null
    setImage(null); setImageFailed(false)
    if (selected?.image_available) void readImage(childId, selected.artifact_id, abort.signal).then(blob => {
      if (abort.signal.aborted) return
      url = URL.createObjectURL(blob); setImage(url)
    }).catch(() => { if (!abort.signal.aborted) { setImageFailed(true); setError('这张图片无法读取，请核查是否已删除或损坏。') } })
    return () => { abort.abort(); if (url) URL.revokeObjectURL(url) }
  }, [childId, selected, readImage])
  const reason = (value: string): string => ({ periodic: '定时采样', page_changed: '翻页变化', ocr_capture: '识别原图' })[value] ?? '页面记录'
  return <section className="parent-panel document-trial-panel" aria-label="高拍仪试用复盘">
    <div className="panel-heading"><h2>高拍仪试用复盘</h2><button className="icon-button" title="刷新复盘" disabled={loading} onClick={() => { setError(''); setRefresh(value => value + 1) }}><RefreshCw size={18} /></button></div>
    <label>学习记录<select value={sessionId} onChange={event => { setSessionId(event.target.value); setOffset(0); setError('') }}>{sessions.map(session => <option key={session.session_id} value={session.session_id}>{new Date(session.started_at).toLocaleString()} · {session.status === 'active' ? '学习中' : '已结束'}</option>)}</select></label>
    {error && <p role="alert">{error}</p>}
    {loading && <p role="status">正在读取复盘记录。</p>}
    {!loading && !sessions.length && !error && <p>尚无高拍仪试用留图。</p>}
    {review && <>
      <p>留图 {review.summary.frame_count} 张 · 正确 {review.summary.correct_count} 题 · 错误 {review.summary.wrong_count} 题 · 未评分 {review.summary.unscored_count} 题 · 保存异常 {review.summary.capture_failure_count} 次</p>
      {review.findings.map(finding => <p key={finding}>{finding}</p>)}
      <div className="document-trial-replay">
        <div className="document-trial-frame-list" aria-label="采样时间线">{review.frames.map(frame => <button key={frame.artifact_id} className={selected?.artifact_id === frame.artifact_id ? 'is-active' : ''} onClick={() => { setSelected(frame); setError('') }}>{new Date(frame.captured_at).toLocaleTimeString()} · {reason(frame.reason)}{!frame.image_available && ' · 图片不可用'}</button>)}</div>
        <div className="document-trial-frame-detail">{image ? <img src={image} alt="该次高拍仪诊断采样原图" /> : selected ? <p>{imageFailed ? '图片读取失败，请刷新重试。' : selected.image_available ? '正在读取图片。' : '原图已删除或缺失，证据不完整。'}</p> : <p>没有可回放的图像。</p>}
          {selected && <TrialFrameQuestions key={selected.artifact_id} childId={childId} frame={selected} request={request} />}
        </div>
      </div>
      <div className="document-trial-pagination"><button className="icon-button" title="上一组图片" disabled={offset === 0 || loading} onClick={() => setOffset(value => Math.max(0, value - 100))}><ChevronLeft size={18} /></button><span>{review.frames.length ? offset + 1 : 0}–{offset + review.frames.length} / {review.summary.frame_count}</span><button className="icon-button" title="下一组图片" disabled={!review.has_more || loading} onClick={() => setOffset(value => value + 100)}><ChevronRight size={18} /></button></div>
    </>}
  </section>
}

function TrialFrameQuestions({ childId, frame, request }: Pick<Props, 'childId' | 'request'> & { frame: Frame }): React.JSX.Element {
  const [offset, setOffset] = useState(0)
  const [page, setPage] = useState({ questions: frame.questions, has_more: Boolean(frame.questions_has_more) })
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(false)
  const [retry, setRetry] = useState(0)
  useEffect(() => {
    if (offset === 0) { setPage({ questions: frame.questions, has_more: Boolean(frame.questions_has_more) }); setError(false); setLoading(false); return }
    const abort = new AbortController()
    setLoading(true); setError(false)
    void request<typeof page>(`/v1/parent/children/${childId}/document-trial/frames/${frame.artifact_id}/questions?offset=${offset}`, { signal: abort.signal })
      .then(value => { if (!abort.signal.aborted) setPage(value) })
      .catch(() => { if (!abort.signal.aborted) setError(true) })
      .finally(() => { if (!abort.signal.aborted) setLoading(false) })
    return () => abort.abort()
  }, [childId, frame, offset, request, retry])
  return <div aria-label="该页作答记录">
    {loading ? <p role="status">正在读取该页作答。</p> : error ? <div><p role="alert">作答记录读取失败，原图和已保存题目未被删除。</p><button className="icon-button" title="重试作答记录" onClick={() => setRetry(value => value + 1)}><RefreshCw size={18} /></button></div> : page.questions.map(question => <article key={question.candidate_id}><p>{question.question_text}</p><p>作答：{question.answer_text ?? '尚未确认作答'}</p><strong>{question.correctness === 1 ? '正确' : question.correctness === 0 ? '错误' : '尚未合格评分'}</strong>{question.authority_type && <p>评分依据：{({ teacher_mark: '老师批改', published_answer_key: '标准答案', agent_generated_rubric: '出题时冻结的答案或量表' })[question.authority_type] ?? '待核查'}</p>}</article>)}
    {(offset > 0 || page.has_more) && <div className="document-trial-pagination"><button className="icon-button" title="上一组作答" disabled={offset === 0 || loading} onClick={() => setOffset(value => Math.max(0, value - 100))}><ChevronLeft size={18} /></button><span>{loading ? '正在读取' : error ? '读取未完成' : page.questions.length ? `${offset + 1}–${offset + page.questions.length}` : '没有更多作答'}</span><button className="icon-button" title="下一组作答" disabled={!page.has_more || loading || error} onClick={() => setOffset(value => value + 100)}><ChevronRight size={18} /></button></div>}
  </div>
}
