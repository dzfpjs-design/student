import { useEffect, useMemo, useState } from 'react'
import { BookPlus, CheckCircle2, Search, SkipForward, X } from 'lucide-react'
import { ApiError, localApi } from './api'
import type { MistakeCaptureCandidate, MistakeItem, MistakeKnowledgeCandidate, OcrArtifact } from './types'

type BusyState = 'prepare' | 'search' | 'save' | 'skip' | null

export function inferMistakeSubject(text: string, fallback = '数学'): string {
  const latinWords = text.match(/[A-Za-z]{2,}/g)?.length ?? 0
  const chineseCharacters = text.match(/[\u4e00-\u9fff]/g)?.length ?? 0
  if (latinWords >= 3 && latinWords >= chineseCharacters) return '英语'
  if (/(化学方程式|元素|原子|分子|质量分数|溶液|酸|碱|盐)/.test(text)) return '化学'
  if (/(速度|加速度|电流|电压|电阻|浮力|压强|功率|光线|焦距)/.test(text)) return '物理'
  if (/(阅读理解|中心思想|修辞|病句|古诗|文言文|作文)/.test(text)) return '语文'
  return fallback || '数学'
}

export function MistakeCaptureComposer({ childId, artifact, initialSubject, onCaptured, onClose }: {
  childId: string
  artifact: OcrArtifact
  initialSubject: string
  onCaptured?: (item: MistakeItem) => void
  onClose: () => void
}): React.JSX.Element {
  const [subject, setSubject] = useState(() => inferMistakeSubject(artifact.extracted_text, initialSubject))
  const [items, setItems] = useState<MistakeCaptureCandidate[]>([])
  const [selectedId, setSelectedId] = useState('')
  const [prompt, setPrompt] = useState('')
  const [answer, setAnswer] = useState('')
  const [knowledgeCandidates, setKnowledgeCandidates] = useState<MistakeKnowledgeCandidate[]>([])
  const [selectedNodeId, setSelectedNodeId] = useState('')
  const [busy, setBusy] = useState<BusyState>('prepare')
  const [error, setError] = useState<string | null>(null)

  const active = useMemo(
    () => items.find((item) => item.candidate_id === selectedId && item.status === 'pending') ?? null,
    [items, selectedId]
  )
  const pendingCount = items.filter((item) => item.status === 'pending').length
  const savedCount = items.filter((item) => item.status === 'saved').length

  const findCandidates = async (text = prompt, nextSubject = subject): Promise<void> => {
    if (text.trim().length < 2) return
    setBusy('search')
    setError(null)
    try {
      const found = await localApi.mistakeKnowledgeCandidates(childId, text.trim(), nextSubject)
      setKnowledgeCandidates(found)
      setSelectedNodeId((current) => found.some((item) => item.knowledge_node_id === current)
        ? current
        : found[0]?.knowledge_node_id ?? '')
      if (found[0]) {
        window.dispatchEvent(new CustomEvent('hsc-agent-intervention', {
          detail: { message: `我找到了一处有作答或批改痕迹的题，题目是“${text.trim().slice(0, 80)}”。它最可能关联“${found[0].title}”。请告诉我你当时写的答案；如果题目识别不对，直接说“题干改成”再说正确题目。对错由评分依据核对，不需要你判断。` }
        }))
      } else {
        window.dispatchEvent(new CustomEvent('hsc-agent-intervention', {
          detail: { message: '我找到了一处有作答或批改痕迹的题，但还不能可靠判断对应知识点。请把这道题完整读给我，我会重新匹配；这一步只核对事实，不判断对错。' }
        }))
      }
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : '知识点候选没有加载成功，请重试。')
    } finally {
      setBusy(null)
    }
  }

  const selectCandidate = (candidate: MistakeCaptureCandidate, currentSubject = subject): void => {
    if (candidate.status !== 'pending') return
    const inferredSubject = inferMistakeSubject(candidate.question_text, currentSubject)
    setSubject(inferredSubject)
    setSelectedId(candidate.candidate_id)
    setPrompt(candidate.question_text)
    setAnswer('')
    setKnowledgeCandidates([])
    setSelectedNodeId('')
    void findCandidates(candidate.question_text, inferredSubject)
  }

  useEffect(() => {
    let cancelled = false
    setBusy('prepare')
    setError(null)
    void localApi.prepareMistakeCandidates(artifact.artifact_id, childId)
      .then((prepared) => {
        if (cancelled) return
        setItems(prepared)
        const first = prepared.find((item) => item.status === 'pending')
        if (first) selectCandidate(first, inferMistakeSubject(first.question_text, initialSubject))
        else setBusy(null)
      })
      .catch((cause) => {
        if (!cancelled) {
          setError(cause instanceof ApiError ? cause.message : '题目没有整理成功，请重试。')
          setBusy(null)
        }
      })
    return () => { cancelled = true }
  }, [artifact.artifact_id, childId])

  const advance = (updated: MistakeCaptureCandidate[]): void => {
    setItems(updated)
    const next = updated.find((item) => item.status === 'pending')
    if (next) selectCandidate(next)
    else {
      setSelectedId('')
      setPrompt('')
      setAnswer('')
      setKnowledgeCandidates([])
      setSelectedNodeId('')
    }
  }

  const save = async (): Promise<boolean> => {
    if (!active) return false
    const selected = knowledgeCandidates.find((item) => item.knowledge_node_id === selectedNodeId)
    if (!selected || answer.trim().length < 1) return false
    setBusy('save')
    setError(null)
    try {
      let current = active
      if (prompt.trim() !== active.question_text) {
        current = await localApi.editMistakeCaptureCandidate(active.candidate_id, childId, prompt.trim())
      }
      const result = await localApi.captureMistakeCandidate(current.candidate_id, {
        childId,
        knowledgeNodeId: selected.knowledge_node_id,
        subject,
        title: selected.title,
        submittedAnswer: answer.trim()
      })
      onCaptured?.(result.mistake)
      advance(items.map((item) => item.candidate_id === result.candidate.candidate_id ? result.candidate : item))
      return true
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : '错题没有保存，请重试。')
      return false
    } finally {
      setBusy(null)
    }
  }

  const skip = async (): Promise<boolean> => {
    if (!active) return false
    setBusy('skip')
    setError(null)
    try {
      const skipped = await localApi.skipMistakeCaptureCandidate(active.candidate_id, childId)
      advance(items.map((item) => item.candidate_id === skipped.candidate_id ? skipped : item))
      return true
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : '这道题没有跳过成功，请重试。')
      return false
    } finally {
      setBusy(null)
    }
  }

  useEffect(() => {
    const control = (event: Event): void => {
      const detail = (event as CustomEvent<{ action?: 'set_answer' | 'correct_prompt' | 'save' | 'skip' | 'close'; value?: string; resolve?: (handled: boolean) => void }>).detail
      if (!detail?.action) return
      if (detail.action === 'set_answer') {
        if (!active || !detail.value?.trim()) { detail.resolve?.(false); return }
        setAnswer(detail.value.trim())
        detail.resolve?.(true)
        return
      }
      if (detail.action === 'correct_prompt') {
        if (!active || !detail.value?.trim()) { detail.resolve?.(false); return }
        const corrected = detail.value.trim()
        setPrompt(corrected)
        void findCandidates(corrected).finally(() => detail.resolve?.(true))
        return
      }
      if (detail.action === 'save') { void save().then((saved) => detail.resolve?.(saved)); return }
      if (detail.action === 'skip') { void skip().then((skipped) => detail.resolve?.(skipped)); return }
      if (detail.action === 'close') { onClose(); detail.resolve?.(true) }
    }
    window.addEventListener('hsc-mistake-capture-voice-action', control)
    return () => window.removeEventListener('hsc-mistake-capture-voice-action', control)
  }, [active, answer, knowledgeCandidates, onClose, prompt, selectedNodeId])

  if (busy === 'prepare' && items.length === 0) {
    return <section className="mistake-capture-composer" aria-label="作答事实整理"><span className="muted-copy">正在整理本页题目...</span></section>
  }

  if (items.length > 0 && pendingCount === 0) {
    return <section className="mistake-capture-composer is-saved" aria-label="作答事实整理"><CheckCircle2 size={24} /><div><strong>本页已整理</strong><span>{savedCount} 道作答进入待评分队列，{items.length - savedCount} 道无效识别已排除。</span></div><button className="secondary-button" onClick={onClose}>完成</button></section>
  }

  return (
    <section className="mistake-capture-composer" aria-label="作答事实整理">
      <header><div><BookPlus size={20} /><div><strong>逐题整理</strong><span>{active ? `第 ${active.question_index + 1} 题，共 ${items.length} 题` : '等待题目'}</span></div></div><button className="icon-button" title="关闭错题整理" onClick={onClose}><X size={17} /></button></header>
      {items.length > 1 && <div className="mistake-capture-queue" aria-label="本页候选题">{items.map((item) => <button key={item.candidate_id} type="button" className={`${item.candidate_id === selectedId ? 'is-active' : ''} is-${item.status}`} disabled={item.status !== 'pending' || busy !== null} onClick={() => selectCandidate(item)}><span>{item.question_index + 1}</span><small>{item.status === 'saved' ? '已保存' : item.status === 'skipped' ? '已跳过' : '待处理'}</small></button>)}</div>}
      {active && <>
        <div className="mistake-capture-fields">
          <label>学科<select value={subject} onChange={(event) => { const next = event.target.value; setSubject(next); setKnowledgeCandidates([]); setSelectedNodeId(''); void findCandidates(prompt, next) }}><option>语文</option><option>数学</option><option>英语</option><option>物理</option><option>化学</option></select></label>
          <label className="is-wide">题目文字<textarea aria-label="错题题目文字" value={prompt} maxLength={4000} onChange={(event) => setPrompt(event.target.value)} /></label>
          <label className="is-wide">当时写的答案<textarea aria-label="错题原答案" value={answer} maxLength={4000} onChange={(event) => setAnswer(event.target.value)} placeholder="填写当时实际写下或说出的答案；不需要判断对错" /></label>
        </div>
        <div className="mistake-candidate-heading"><strong>关联知识点</strong><button className="secondary-button" disabled={busy !== null || prompt.trim().length < 2} onClick={() => void findCandidates()}><Search size={15} />{busy === 'search' ? '匹配中' : '重新匹配'}</button></div>
        {knowledgeCandidates.length === 0 && busy !== 'search' ? <p className="muted-copy">没有可靠候选，请修改题目文字或学科后重新匹配。</p> : <div className="mistake-candidate-list" role="radiogroup" aria-label="选择知识点">{knowledgeCandidates.map((item) => <label key={item.knowledge_node_id}><input type="radio" name="mistake-knowledge-node" checked={selectedNodeId === item.knowledge_node_id} onChange={() => setSelectedNodeId(item.knowledge_node_id)} /><span><strong>{item.title}</strong><small>{item.description}</small></span></label>)}</div>}
        {error && <div className="inline-error">{error}</div>}
        <div className="mistake-capture-actions"><button className="secondary-button" disabled={busy !== null} onClick={() => void skip()}><SkipForward size={15} />{busy === 'skip' ? '正在排除' : '没有作答或识别有误'}</button><button className="primary-button" disabled={busy !== null || !selectedNodeId || prompt.trim().length < 2 || answer.trim().length < 1} onClick={() => void save()}>{busy === 'save' ? '正在保存' : '保存作答，等待评分'}</button></div>
      </>}
      {!active && error && <div className="inline-error">{error}</div>}
    </section>
  )
}
