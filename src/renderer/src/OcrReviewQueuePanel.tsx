import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { Check, CheckCheck, RefreshCw, RotateCcw, SkipForward } from 'lucide-react'
import { ApiError, localApi } from './api'
import type { OcrReviewItem } from './types'
import { useParentWorkFocus, type ParentWorkFocusProps } from './parentWorkFocus'
import { loadOcrReviewRecovery, saveOcrReviewRecovery, ocrRecoveryClearedEvent, type OcrRetryCommand as RetryCommand, type OcrReviewRecovery } from './ocrReviewRecovery'

const labels = { text: '低置信文字', handwriting: '手写内容', formula: '数学表达式', formula_question: '公式题干', marking: '批改标记' }

export function OcrReviewQueuePanel({ childId, ...focus }: { childId: string } & ParentWorkFocusProps): React.JSX.Element {
  const [generation, setGeneration] = useState(0)
  useEffect(() => {
    const cleared = (event: Event): void => {
      const owner = (event as CustomEvent<string | undefined>).detail
      if (owner === undefined || owner === childId) setGeneration(value => value + 1)
    }
    window.addEventListener(ocrRecoveryClearedEvent, cleared)
    return () => window.removeEventListener(ocrRecoveryClearedEvent, cleared)
  }, [childId])
  return <OcrReviewQueueContents key={`${childId}:${generation}`} childId={childId} {...focus} />
}

function OcrReviewQueueContents({ childId, focusRequest, onFocusResolved }: { childId: string } & ParentWorkFocusProps): React.JSX.Element {
  const root = useRef<HTMLElement>(null)
  const [initial] = useState(() => {
    try { return { recovery: loadOcrReviewRecovery(childId), invalid: false } }
    catch { return { recovery: { childId, drafts: {}, pending: null } as OcrReviewRecovery, invalid: true } }
  })
  const recovery = useRef(initial.recovery)
  const active = useRef(true)
  const loadEpoch = useRef(0)
  const [storageError, setStorageError] = useState(initial.invalid)
  const [queueReady, setQueueReady] = useState(false)
  const [focusReadNonce, setFocusReadNonce] = useState('')
  const [items, setItems] = useState<OcrReviewItem[]>([])
  const [selectedId, setSelectedId] = useState('')
  const [text, setText] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [recent, setRecent] = useState<OcrReviewItem[]>([])
  const [batchSelection, setBatchSelection] = useState<string[]>([])
  const [retryCommand, setRetryCommand] = useState<RetryCommand | 'load' | null>(initial.recovery.pending)
  const writeRecovery = (next: OcrReviewRecovery): boolean => {
    if (initial.invalid) return false
    try {
      const stored = { ...next, artifactIds: { ...next.artifactIds, ...Object.fromEntries([...items, ...recent].map(item => [item.review_id, item.artifact_id])) } }
      saveOcrReviewRecovery(stored); recovery.current = stored; setStorageError(false); return true
    }
    catch { setStorageError(true); return false }
  }

  const selected = useMemo(() => items.find((item) => item.review_id === selectedId) ?? items[0] ?? null, [items, selectedId])
  useEffect(() => {
    if (focusRequest && queueReady && items.some(item => item.review_id === focusRequest.objectId)) setSelectedId(focusRequest.objectId)
  }, [focusRequest, queueReady, items])
  useParentWorkFocus(focusRequest, !busy && queueReady && (!focusRequest || focusReadNonce === focusRequest.nonce) && (!focusRequest || selected?.review_id === focusRequest.objectId || !items.some(item => item.review_id === focusRequest.objectId)), root, onFocusResolved)
  const load = async (): Promise<void> => {
    const epoch = ++loadEpoch.current
    setBusy(true); setError(null)
    const [pendingResult, historyResult] = await Promise.allSettled([localApi.ocrReviewQueue(childId), localApi.ocrReviewHistory(childId)])
    if (!active.current || loadEpoch.current !== epoch) return
    const pendingValid = pendingResult.status === 'fulfilled' && pendingResult.value.every(item => item.child_id === childId)
    const historyValid = historyResult.status === 'fulfilled' && historyResult.value.every(item => item.child_id === childId)
    setQueueReady(pendingValid)
    if (pendingResult.status === 'fulfilled' && pendingValid) {
      setFocusReadNonce(focusRequest?.nonce ?? '')
      const next = pendingResult.value
      setItems(next)
      setBatchSelection((current) => current.filter((id) => next.some((item) => item.review_id === id)))
      setSelectedId((current) => next.some((item) => item.review_id === current) ? current : next[0]?.review_id ?? '')
    }
    if (historyResult.status === 'fulfilled' && historyValid) setRecent(historyResult.value)
    if (!pendingValid || !historyValid) {
      if (!pendingValid && focusRequest) onFocusResolved?.(focusRequest.nonce, '目标校对项读取失败，未打开其他题目。')
      const failure = pendingResult.status === 'rejected' ? pendingResult.reason
        : historyResult.status === 'rejected' ? historyResult.reason : null
      setError(failure instanceof ApiError ? failure.message : '部分本地校对记录没有加载成功；已保留可用记录。')
      if (!recovery.current.pending) setRetryCommand('load')
    } else if (!recovery.current.pending) {
      setRetryCommand(null)
    }
    setBusy(false)
  }
  useEffect(() => { active.current = true; void load(); return () => { active.current = false; loadEpoch.current += 1 } }, [childId, focusRequest?.nonce])
  useLayoutEffect(() => {
    const draft = selected && recovery.current.drafts[selected.review_id]
    setText(draft && draft.proposed === selected?.proposed_text ? draft.text : selected?.proposed_text ?? '')
  }, [selected?.review_id, selected?.proposed_text])

  const process = async (kind: 'confirm' | 'skip'): Promise<void> => {
    if (!selected) return
    if (kind === 'confirm' && !text.trim()) { setError('修正文字不能为空；如该区域不是文本，请选择跳过。'); return }
    await execute(kind === 'confirm' ? { kind, reviewId: selected.review_id, text: text.trim(), actionId: crypto.randomUUID() } : { kind, reviewId: selected.review_id, actionId: crypto.randomUUID() })
  }
  const execute = async (command: RetryCommand): Promise<void> => {
    if (busy || initial.invalid) return
    const replaying = Boolean(recovery.current.pending)
    if (recovery.current.pending && JSON.stringify(recovery.current.pending) !== JSON.stringify(command)) {
      setError('请先确认上一次操作的结果，再处理其他校对项。'); return
    }
    if (!writeRecovery({ ...recovery.current, pending: command })) return
    setBusy(true); setError(null)
    try {
      if (command.kind === 'batch') {
        const result = await localApi.batchOcrReview(
          childId, command.reviewIds, command.decision, command.actionId
        )
        if (result.items.length !== command.reviewIds.length || result.items.some(item => item.child_id !== childId || !command.reviewIds.includes(item.review_id))) throw new Error('OCR batch receipt mismatch')
        if (!active.current) return
        const processedIds = new Set(result.items.map((item) => item.review_id))
        setRecent((current) => [...result.items, ...current.filter((item) => !processedIds.has(item.review_id))].slice(0, 10))
        setItems((current) => current.filter((item) => !processedIds.has(item.review_id)))
        setBatchSelection([])
        setSelectedId((current) => processedIds.has(current) ? '' : current)
      } else if (command.kind === 'undo') {
        const restored = await localApi.undoOcrReview(command.reviewId, childId, command.actionId)
        if (restored.child_id !== childId || restored.review_id !== command.reviewId) throw new Error('OCR undo receipt mismatch')
        if (!active.current) return
        setItems((current) => [restored, ...current.filter((item) => item.review_id !== restored.review_id)])
        setSelectedId(restored.review_id)
        setRecent((current) => current.filter((item) => item.review_id !== restored.review_id))
      } else {
        const saved = command.kind === 'confirm'
          ? await localApi.confirmOcrReview(command.reviewId, childId, command.text, command.actionId)
          : await localApi.skipOcrReview(command.reviewId, childId, command.actionId)
        if (saved.child_id !== childId || saved.review_id !== command.reviewId) throw new Error('OCR receipt mismatch')
        if (!active.current) return
        setRecent((current) => [saved, ...current.filter((item) => item.review_id !== saved.review_id)].slice(0, 10))
        setItems((current) => current.filter((item) => item.review_id !== saved.review_id))
        setSelectedId((current) => current === saved.review_id ? '' : current)
      }
      if (!active.current) return
      const drafts = { ...recovery.current.drafts }
      for (const id of command.kind === 'batch' ? command.reviewIds : [command.reviewId]) delete drafts[id]
      if (!writeRecovery({ ...recovery.current, drafts, pending: null })) {
        setError('服务端已保存，但本机恢复记录尚未清理；请用原请求核对结果。'); setRetryCommand(command); return
      }
      setRetryCommand(null)
      // A replay confirms an old operation, not the current status after another review or undo.
      if (replaying) await load()
    } catch (cause) { if (active.current) { setError(cause instanceof ApiError ? cause.message : '操作结果暂未确认，请用原请求重试。'); setRetryCommand(command) } }
    finally { if (active.current) setBusy(false) }
  }
  const undo = async (processed: OcrReviewItem): Promise<void> => {
    await execute({ kind: 'undo', reviewId: processed.review_id, actionId: crypto.randomUUID() })
  }
  const processBatch = async (decision: 'confirm_proposed' | 'skip'): Promise<void> => {
    if (batchSelection.length === 0) return
    await execute({ kind: 'batch', reviewIds: batchSelection, decision, actionId: crypto.randomUUID() })
  }

  return <section ref={root} className="parent-section ocr-review-panel" aria-label="OCR 人工校对">
    <header><div><h2>OCR 人工校对</h2><p>仅核对低置信文字，以及有明确证据的手写、公式或批改标记；修正仅保存在本机，并会成为后续本地评测真值。</p></div><button className="secondary-button" disabled={busy} onClick={() => void load()}><RefreshCw size={15} />刷新</button></header>
    <p className="muted-copy">原始页面未保留，刷新或重启后不能查看图片；请依据候选文字和区域位置复核，必要时重新拍摄。</p>
    {storageError && <p role="alert">本机校对恢复记录未能安全保存或读取。请保留当前页面；在恢复记录可用前不会提交新的校对。{!initial.invalid && <button disabled={busy} onClick={() => {
      writeRecovery({ ...recovery.current, drafts: selected ? { ...recovery.current.drafts, [selected.review_id]: { proposed: selected.proposed_text, text } } : recovery.current.drafts })
    }}>重试保存恢复记录</button>}</p>}
    {retryCommand && retryCommand !== 'load' && !error && <p role="status">上次校对结果待确认，尚未自动重发。<button disabled={busy || initial.invalid} onClick={() => void execute(retryCommand)}>重试刚才操作</button></p>}
    {error && <div className="tutor-error">{error}{retryCommand && <button className="secondary-button" disabled={busy} onClick={() => { if (retryCommand === 'load') void load(); else void execute(retryCommand) }}>{retryCommand === 'load' ? '重试加载' : '重试刚才操作'}</button>}</div>}
    {items.length === 0 ? <p className="muted-copy">{queueReady ? '没有待处理的 OCR 校对项。' : busy ? '正在读取 OCR 校对项。' : '待处理的 OCR 校对项暂时无法确认。'}</p> : <div className="ocr-review-layout">
      <div className="ocr-review-list" role="list" aria-label="待处理校对项">
        <div className="ocr-batch-controls">
          <label><input type="checkbox" aria-label="选择全部 OCR 校对项" checked={batchSelection.length === items.length} onChange={(event) => setBatchSelection(event.target.checked ? items.map((item) => item.review_id) : [])} />选择全部</label>
          <button className="secondary-button" disabled={busy || batchSelection.length === 0} onClick={() => void processBatch('skip')}><SkipForward size={15} />跳过选中</button>
          <button className="secondary-button" disabled={busy || batchSelection.length === 0 || items.some(item => item.review_kind === 'formula_question' && batchSelection.includes(item.review_id))} onClick={() => void processBatch('confirm_proposed')}><CheckCheck size={15} />确认选中候选</button>
        </div>
        {items.map((item, index) => <div key={item.review_id} role="listitem" className="ocr-review-row"><input type="checkbox" aria-label={`选择第${index + 1}条 OCR 校对项`} checked={batchSelection.includes(item.review_id)} onChange={(event) => setBatchSelection((current) => event.target.checked ? [...current, item.review_id] : current.filter((id) => id !== item.review_id))} disabled={busy} /><button type="button" className={item.review_id === selected?.review_id ? 'is-active' : ''} onClick={() => setSelectedId(item.review_id)} disabled={busy}><strong>{index + 1}. {labels[item.review_kind]}</strong><span>置信度 {Math.round(item.confidence * 100)}%</span></button></div>)}
      </div>
      {selected && <div data-work-object={selected.review_id} className="ocr-review-editor"><strong>{labels[selected.review_kind]}</strong><span className="muted-copy">区域 {regionLabel(selected.region)} · 识别置信度 {Math.round(selected.confidence * 100)}%</span><label>候选与修正文字<textarea aria-label="OCR 校对文字" value={text} maxLength={4000} disabled={busy || Boolean(recovery.current.pending) || initial.invalid} onChange={(event) => {
        const value = event.target.value; setText(value)
        writeRecovery({ ...recovery.current, drafts: { ...recovery.current.drafts, [selected.review_id]: { proposed: selected.proposed_text, text: value } } })
      }} /></label><div className="ocr-review-actions"><button className="secondary-button" disabled={busy || Boolean(recovery.current.pending) || initial.invalid} onClick={() => void process('skip')}><SkipForward size={15} />跳过</button><button className="primary-button" disabled={busy || !text.trim() || Boolean(recovery.current.pending) || initial.invalid || storageError} onClick={() => void process('confirm')}><Check size={15} />确认修正</button></div></div>}
    </div>}
    {recent.length > 0 && <div className="ocr-review-history" aria-label="最近处理的校对项"><strong>最近处理（刷新或重启后仍可撤销）</strong>{recent.map((item) => <div key={item.review_id}><span>{labels[item.review_kind]} · {item.status === 'confirmed' ? '已确认' : '已跳过'}</span><button className="secondary-button" disabled={busy} onClick={() => void undo(item)}><RotateCcw size={15} />撤销</button></div>)}</div>}
  </section>
}

function regionLabel(region: number[][] | null): string {
  if (!region?.length) return '未提供区域位置'
  const x = Math.min(...region.map((point) => point[0] ?? 0)); const y = Math.min(...region.map((point) => point[1] ?? 0))
  return `页内约 ${Math.round(x)}, ${Math.round(y)}`
}
