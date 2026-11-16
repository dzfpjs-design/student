import { useEffect, useRef, useState } from 'react'
import { Check, RefreshCw, Trash2 } from 'lucide-react'
import { ApiError } from './api'
import './SpecializedReviewQueue.css'
import type { SpecializedSupplyCandidate } from './types'
import { assertReviewAvailable, checkOriginalReview, emptyReview, postOriginalReview, readReviewContext,
  readSpecializedReviews, saveSpecializedReviews, specializedReviewCleared, specializedReviewKey, typedModelReview,
  sameReviewReference, type ReviewBody, type ReviewContext, type ReviewDraft, type StoredReviews } from './specializedReviewRecovery'

type QueueProps = {
  childId: string; candidates: SpecializedSupplyCandidate[]; onSaved: () => void
}
export function SpecializedReviewQueue(props: QueueProps): React.JSX.Element {
  return <ChildReviewQueue key={props.childId} {...props} />
}
function ChildReviewQueue({ childId, candidates, onSaved }: QueueProps): React.JSX.Element {
  const [store, setStore] = useState<StoredReviews | null>(null)
  const latest = useRef<StoredReviews | null>(null)
  const [error, setError] = useState('')
  const [halted, setHalted] = useState(false)
  const [notice, setNotice] = useState('')
  const [confirmed, setConfirmed] = useState<string[]>([])
  const controller = useRef(new AbortController())
  useEffect(() => {
    controller.current = new AbortController()
    const read = (): void => {
      try { const value = readSpecializedReviews(childId); latest.current = value; setStore(value); setError('') }
      catch { setError('本机审核记录暂不可读，未发送或清空。请检查本机存储。'); latest.current = null; setStore(null) }
    }
    read()
    const stop = (): void => { controller.current.abort(); setHalted(true) }
    const clear = (event: Event): void => {
      if ((event as CustomEvent).detail !== childId && (event as CustomEvent).detail !== undefined) return
      stop(); latest.current = null; setStore(null); setNotice('本机审核记录已按隐私操作清理，请重新打开审核。')
    }
    const changed = (event: StorageEvent): void => {
      if (event.key !== null && event.key !== specializedReviewKey(childId)) return
      stop(); read()
    }
    window.addEventListener('hsc-privacy-pause', stop)
    window.addEventListener(specializedReviewCleared, clear)
    window.addEventListener('storage', changed)
    return () => {
      controller.current.abort()
      window.removeEventListener('hsc-privacy-pause', stop)
      window.removeEventListener(specializedReviewCleared, clear)
      window.removeEventListener('storage', changed)
    }
  }, [childId])
  const persist = (id: string, draft: ReviewDraft | null): void => {
    assertReviewAvailable(controller.current.signal)
    if (!latest.current || halted) throw new Error('本机审核已暂停。')
    try {
      const entries = latest.current.value.entries.filter(d => d.candidateId !== id)
      if (draft) entries.push(draft)
      const value = saveSpecializedReviews(childId, latest.current, entries)
      latest.current = value; setStore(value)
    } catch (cause) { setError('本机审核记录没有安全保存，未继续发送。原记录仍需核对。'); throw cause }
  }
  const complete = (id: string): void => {
    persist(id, null)
    setConfirmed(ids => [...ids, id])
    setNotice('原审核已保存。发布记录不代表当前来源仍有效，也不代表孩子已经掌握。')
    onSaved()
  }
  const owned = candidates.filter(c => c.child_id === childId && c.review_workflow !== 'curriculum_review')
  const ids = [...new Set([...owned.map(c => c.candidate_id), ...(store?.value.entries.map(d => d.candidateId) ?? [])])].filter(id => !confirmed.includes(id))
  return <div className="variant-review-list specialized-review-queue">
    {notice && <p role="status">{notice}</p>}
    {error && <p role="alert">{error}</p>}
    {halted && <p role="alert">审核已暂停，未重发原请求。请重新打开审核。</p>}
    {ids.map((id, index) => <ReviewItem key={`${childId}:${id}`} childId={childId}
      candidate={owned.find(c => c.candidate_id === id)} draft={store?.value.entries.find(d => d.candidateId === id)}
      index={index} disabled={!store || !!error || halted} parentSignal={controller.current.signal}
      save={d => persist(id, d)} complete={() => complete(id)} />)}
  </div>
}

function ReviewItem({ childId, candidate, draft, index, disabled, parentSignal, save, complete }: {
  childId: string; candidate?: SpecializedSupplyCandidate; draft?: ReviewDraft; index: number; disabled: boolean
  parentSignal: AbortSignal; save: (draft: ReviewDraft) => void; complete: () => void
}): React.JSX.Element {
  const current = draft ?? emptyReview(candidate!)
  const [context, setContext] = useState<ReviewContext | null>(null)
  const [message, setMessage] = useState('')
  const [busy, setBusy] = useState(false)
  const [retry, setRetry] = useState(false)
  const active = useRef(true)
  const inFlight = useRef(false)
  const localController = useRef(new AbortController())
  useEffect(() => {
    active.current = true
    localController.current = new AbortController()
    const abort = (): void => localController.current.abort()
    if (parentSignal.aborted) abort()
    parentSignal.addEventListener('abort', abort)
    return () => { active.current = false; abort(); parentSignal.removeEventListener('abort', abort) }
  }, [parentSignal])
  const live = (): void => { if (!active.current) throw new Error('审核已关闭。'); assertReviewAvailable(localController.current.signal) }
  const run = async (operation: () => Promise<void>): Promise<void> => {
    if (inFlight.current || disabled) return
    inFlight.current = true; setBusy(true); setMessage('')
    try { live(); await operation() }
    catch (cause) { if (active.current && !localController.current.signal.aborted) setMessage(cause instanceof Error ? cause.message : '审核暂未完成，原记录已保留。') }
    finally { inFlight.current = false; if (active.current) setBusy(false) }
  }
  const update = (patch: Partial<ReviewDraft>): void => {
    try { live(); save({ ...current, ...patch }); setMessage('草稿已保存在本机。') }
    catch { setMessage('草稿未保存，未发送审核。请检查本机存储。') }
  }
  const readContext = (): Promise<void> => run(async () => {
    const value = await readReviewContext(childId, current.candidateId, localController.current.signal)
    live()
    if (!candidate || value.candidate_prompt !== candidate.prompt || value.candidate_explanation !== candidate.explanation
      || !sameReviewReference(value.candidate_expected_answer, candidate.expected_answer)) {
      setContext(null); throw new Error('列表中的题目或参考答案已变化，请刷新列表后重新核对，原草稿未自动迁移。')
    }
    setContext(value)
    if (!current.binding && !current.pending) save({ ...current, binding: value.binding_version })
    if (current.binding && current.binding !== value.binding_version) setMessage('原题或候选版本已变化，旧草稿未自动批准或迁移。')
  })
  const oldVersion = !!context && current.binding !== context.binding_version
  const model = candidate?.review_workflow === 'model' || candidate?.validation.ruleset === 'specialized_closed_exact_v1'
  const legacy = !!candidate && (model ? !typedModelReview(candidate) : candidate.validation.ruleset === 'specialized_supply_local_gate_v1')
  const canApprove = !!candidate && candidate.validation.passed && !legacy && !!context && !oldVersion
    && context.status === 'needs_review' && context.candidate_prompt === candidate.prompt
    && context.candidate_explanation === candidate.explanation && sameReviewReference(context.candidate_expected_answer, candidate.expected_answer)
    && !!current.note.trim()
    && (!current.stage || (context.allowed_stages.includes(current.stage)
      && current.sourceQuote.trim().length >= 4 && context.source_prompt.includes(current.sourceQuote)
      && current.candidateQuote.trim().length >= 4 && context.candidate_prompt.includes(current.candidateQuote)
      && current.sameSkill.trim().length >= 12 && current.changedContext.trim().length >= 12))
  const send = async (body: ReviewBody): Promise<void> => {
    try {
      await postOriginalReview(childId, current.candidateId, body, localController.current.signal)
      live(); complete()
    } catch (cause) {
      live()
      // A received validation/auth rejection is not a lost success receipt.
      if (cause instanceof ApiError && cause.status && [400, 401, 403, 404, 422].includes(cause.status)) {
        save({ ...current, pending: null }); setRetry(false)
        throw new Error(`审核被拒绝，未记为通过；原备注已保留。${cause.message}`)
      }
      throw new Error('审核回执未确认，原请求已保存在本机。请核对原审核，不要重新批准。')
    }
  }
  const submit = (decision: 'approve' | 'reject'): Promise<void> => run(async () => {
    if (current.pending || (decision === 'approve' && !canApprove)) return
    const body: ReviewBody = { decision, note: current.note, client_action_id: crypto.randomUUID() }
    if (decision === 'approve') {
      body.expected_binding_version = current.binding
      if (current.stage) body.stage_review = { schema: 'specialized_stage_review_v1', stage: current.stage,
        binding_version: current.binding, source_quote: current.sourceQuote, candidate_quote: current.candidateQuote,
        same_skill_rationale: current.sameSkill, changed_context_rationale: current.changedContext }
    }
    save({ ...current, pending: body }); live()
    await send(body)
  })
  const check = (): Promise<void> => run(async () => {
    if (!current.pending) return
    const result = await checkOriginalReview(childId, current.candidateId, current.pending, localController.current.signal)
    live(); setRetry(false)
    if (result === 'saved') { complete(); return }
    if (result === 'conflict') { setMessage('原审核尚不能确认，候选缺失或已有不同审核。原请求保留，未重发。'); return }
    if (current.pending.decision === 'approve') {
      const value = await readReviewContext(childId, current.candidateId, localController.current.signal)
      live(); setContext(value)
      if (value.binding_version !== current.pending.expected_binding_version) {
        setMessage('原审核尚未入库且来源版本已变，不能重发旧批准。原请求保留。'); return
      }
    }
    setRetry(true); setMessage('当前仍未审核。可再次发送已保存的原请求，编号和内容不变。')
  })
  return <article className="variant-review-item" data-work-object={current.candidateId}>
    <div className="panel-heading"><h3>专项候选题 {index + 1}</h3><span>{candidate?.subject ?? '待核对原审核'}</span></div>
    <p><strong>题目：</strong>{candidate?.prompt ?? current.title}</p>
    {candidate && <><p><strong>完整参考答案：</strong>{candidate.expected_answer?.exact_answers?.join('；') ?? candidate.expected_answer?.keywords?.join('；') ?? '答案依据暂不可读'}</p><p><strong>解析：</strong>{candidate.explanation}</p></>}
    {candidate?.expected_answer?.answer_format === 'reviewed_exact_text_v1' && <p><strong>评分规则：完整文字答案。</strong>只做全角、大小写和空白归一，不会自动把“六”换成“6”。不是关键词命中；单位和表示方式须在参考答案中明确。</p>}
    {candidate?.expected_answer?.answer_format === 'reviewed_spoken_numeric_v1' && <p><strong>评分规则：{candidate.expected_answer.numeric_kind === 'integer' ? '单个整数等值' : '单个有理数等值'}。</strong>
      {candidate.expected_answer.numeric_kind === 'integer' ? '允许阿拉伯整数及中文整数口述；不接受分数或小数表示。' : '允许整数、分数、小数及相应中文口述的数值等价。'}
      仅适用无单位、无指定表示方式的单值答案。单位、百分数、多答案、否定句和表达式不按本规则自动评分；题目有这些要求时不能批准为数值题。</p>}
    {legacy && <p role="alert">旧版关键词候选没有可用的评分合同，不能直接批准。请重新准备候选，原记录保留。</p>}
    {!candidate && !current.pending && <p>该候选已不在待审列表，旧草稿保留，未自动用于其他题目。</p>}
    <p>这里只核对题目与参考答案，不代表模型经过校准，也不代表孩子已经掌握。迁移阶段须另核对原题与变化情境。</p>
    <fieldset disabled={disabled || busy || !!current.pending}>
      <label>审核备注（批准前必填）<textarea aria-label={`专项候选题${index + 1}审核备注`} maxLength={500} value={current.note} onChange={e => update({ note: e.target.value })} /></label>
      {candidate && !legacy && <button type="button" onClick={() => void readContext()}><RefreshCw size={16} />核对当前原题</button>}
      {context && <>
        <p><strong>原题：</strong>{context.source_prompt}</p>
        {oldVersion ? <><p role="alert">草稿属于旧版本，须重新核对。原备注保留，迁移判断不会自动沿用。</p>
          <button onClick={() => update({ binding: context.binding_version, stage: '', sourceQuote: '', candidateQuote: '', sameSkill: '', changedContext: '' })}><RefreshCw size={16} />按当前原题重新核对</button></>
          : <label>发布用途<select aria-label="发布用途" value={current.stage} onChange={e => update({ stage: e.target.value as ReviewDraft['stage'] })}>
            <option value="">仅审核题目，不认定迁移阶段</option>
            {context.allowed_stages.includes('near_transfer') && <option value="near_transfer">近迁移：相同技能，不同条件</option>}
            {context.allowed_stages.includes('far_transfer') && <option value="far_transfer">远迁移：相同技能，不同情境</option>}
          </select></label>}
        {current.stage && !oldVersion && <>
          <label>原题依据原文<textarea value={current.sourceQuote} maxLength={1200} onChange={e => update({ sourceQuote: e.target.value })} /></label>
          <label>候选题依据原文<textarea value={current.candidateQuote} maxLength={1200} onChange={e => update({ candidateQuote: e.target.value })} /></label>
          <label>相同技能的核对理由<textarea value={current.sameSkill} maxLength={1200} onChange={e => update({ sameSkill: e.target.value })} /></label>
          <label>条件或情境变化的核对理由<textarea value={current.changedContext} maxLength={1200} onChange={e => update({ changedContext: e.target.value })} /></label>
        </>}
      </>}
      {candidate && <div className="review-actions"><button disabled={!canApprove} onClick={() => void submit('approve')}><Check size={16} />批准</button>
        <button onClick={() => void submit('reject')}><Trash2 size={16} />拒绝</button></div>}
    </fieldset>
    {current.pending && <div className="review-actions"><p>原审核回执待核对；未自动再次发送。</p>
      <button disabled={disabled || busy} onClick={() => void check()}><RefreshCw size={16} />核对原审核</button>
      {retry && <button disabled={disabled || busy} onClick={() => void run(async () => {
        // Recheck on every explicit retry; the server repeats source/version checks in its write transaction.
        const result = await checkOriginalReview(childId, current.candidateId, current.pending!, localController.current.signal)
        live(); setRetry(false)
        if (result === 'saved') complete()
        else if (result === 'unreviewed') await send(current.pending!)
        else setMessage('原审核状态已变化，未重发。')
      })}><RefreshCw size={16} />重试原审核</button>}
    </div>}
    {message && <p role="status">{message}</p>}
  </article>
}
