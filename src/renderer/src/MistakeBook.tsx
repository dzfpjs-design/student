import {useQuestionViewportFocus} from './useQuestionViewportFocus'
import {privacyStopPending} from './deviceCommandReceipt'
import {useEffect,useLayoutEffect,useRef,useState} from 'react'
import {ArrowLeft,BookOpenCheck,CheckCircle2,Clock3,Mic,RefreshCw,RotateCcw,ScanLine} from 'lucide-react'
import {localApi} from './api'
import type {ChildProfile,MistakeItem,MistakeNextActivity,MistakeStatus} from './types'
import {CloudMistakeLearning,CloudMistakeIntake} from './CloudMistakeLearning'
import {mistakeLearningApi,readMistakeLearningIntake,clearMistakeLearningIntake} from './mistakeLearningApi'
import {MISTAKE_LEARNING_REFRESH_EVENT,MISTAKE_LEARNING_RENDERED_EVENT,type MistakeLearningLibrary,type MistakeLearningPanel,type MistakeLearningRefresh,type MistakeLearningState,type MistakeLearningSurface} from './mistakeLearningTypes'
import {bookCauses,bookSubjects,emptyBookFilters,filteredMistakes,MistakeBookFilters} from './MistakeBookBrowse'
import {MistakeBookTopics,type MistakeTopicSpeech,type MistakeTopicVoiceRequest} from './MistakeBookTopics'
import {reviewLabel,type OriginalReviewRequest} from './MistakeOriginalReview'
import './cloudMistakeLearning.css'
import {MistakeBookPrint} from './MistakeBookPrint'
export type MistakeWorkspaceReceipt = { workspace_opened: true; workspace: 'mistake_book' | 'learning'; child_id: string; session_id: string; content_status: 'ready' | 'empty' }
type BookTab = 'today' | 'all' | 'topics'
type MistakeBookProps = {
  child: ChildProfile
  onClose: () => void
  embedded?: boolean
  active?: boolean
  sessionId?: string
  onReady?: (status: 'ready' | 'empty') => void
  onUnavailable?: (message: string) => void
  onLearningFocus?: (surface: MistakeLearningSurface | null, ownerId: string) => void
  onIntakeRequest?: (kind: 'voice' | 'camera') => void
  onOriginalReviewRequest?: OriginalReviewRequest
  topicVoiceRequest?: MistakeTopicVoiceRequest | null
  onTopicVoiceHandled?: (id: string) => void
  onTopicSpeech?: MistakeTopicSpeech
  onCoachSpeech?: (text: string, scope: AbortSignal) => void
  documentMonitoring?: { status: 'observing' | 'paused' | 'unavailable'; pendingCount?: number }
}

export function MistakeBook(props: MistakeBookProps): React.JSX.Element {
  return <MistakeBookForChild key={`${props.child.child_id}:${props.sessionId ?? ''}`} {...props}/>
}

function MistakeBookForChild({ child, onClose, embedded = false, active = true, sessionId, onReady, onUnavailable, onLearningFocus, onIntakeRequest, onOriginalReviewRequest, documentMonitoring, topicVoiceRequest, onTopicVoiceHandled, onTopicSpeech, onCoachSpeech }: MistakeBookProps): React.JSX.Element {
  const [library, setLibrary] = useState<MistakeLearningLibrary | null>(null)
  const [tab, setTab] = useState<BookTab>('all')
  const scopedTopicVoice = topicVoiceRequest?.childId === child.child_id && topicVoiceRequest.sessionId === sessionId ? topicVoiceRequest : null
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [finishedTodayId, setFinishedTodayId] = useState<string | null>(null)
  const [filters, setFilters] = useState(emptyBookFilters)
  const [showIntake, setShowIntake] = useState(false)
  const [showPrint, setShowPrint] = useState(false)
  const [loading, setLoading] = useState(true)
  const [graphMatching, setGraphMatching] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [refreshToken, setRefreshToken] = useState(0)
  const [libraryRenderRequest, setLibraryRenderRequest] = useState(0)
  const [viewRequest, setViewRequest] = useState<{ id: string; panel: MistakeLearningPanel; token: number } | null>(null)
  const [autoAnalysis, setAutoAnalysis] = useState<{ mistakeId: string; actionId: string } | null>(() => {
    try {
      const saved = readMistakeLearningIntake(child.child_id)
      return saved?.status === 'saved' && saved.mistake_id ? { mistakeId: saved.mistake_id, actionId: saved.analysis_action_id } : null
    } catch { return null }
  })
  const selected = useRef<string | null>(null)
  const surface = useRef<HTMLElement | null>(null)
  const foregroundLoading = useRef(true)
  const announced = useRef(-1), bookOwner = useRef(crypto.randomUUID()), mounted = useRef(false), generation = useRef(0)

  const load = async (preferredId?: string, backgroundSignal?: AbortSignal): Promise<MistakeLearningLibrary | null> => {
    if (backgroundSignal && (foregroundLoading.current || backgroundSignal.aborted || sessionId && privacyStopPending(sessionId))) return null
    const mine = ++generation.current
    if (!backgroundSignal) { foregroundLoading.current = true;setLoading(true) }
    try {
      const value = await mistakeLearningApi.library(child.child_id, backgroundSignal)
      if (backgroundSignal?.aborted || !mounted.current || mine !== generation.current) return null
      setLibrary(value);setError(null)
      const wanted = preferredId ?? selected.current ?? autoAnalysis?.mistakeId
      setSelectedId(wanted && value.items.some(item => item.mistake_id === wanted) ? wanted : null)
      return value
    } catch {
      if (!backgroundSignal && mounted.current && mine === generation.current) setError('错题本暂时没有读到，已经保存的学习内容仍在。请重试读取。')
      return null
    } finally {
      if (!backgroundSignal && mounted.current && mine === generation.current) { foregroundLoading.current = false;setLoading(false) }
    }
  }
  useLayoutEffect(() => { mounted.current = true;return () => { mounted.current = false;generation.current++ } }, [])
  useEffect(() => { void load() }, [child.child_id])
  useEffect(() => { if (!active) setShowPrint(false) }, [active])
  useEffect(() => {
    const refresh = (event: Event): void => {
      const detail = (event as CustomEvent<MistakeLearningRefresh>).detail
      if (!detail || detail.child_id !== child.child_id || detail.session_id && detail.session_id !== sessionId) return
      if (detail.view === 'today') { setTab('today');setFinishedTodayId(null);setFilters(emptyBookFilters);setLibraryRenderRequest(value => value + 1) }
      if (detail.mistake_id) {
        setSelectedId(detail.mistake_id)
        if (selected.current !== detail.mistake_id) { setTab('all');setFilters(emptyBookFilters) }
        const panel = detail.panel ?? (detail.review ? 'redo' : null)
        if (panel) setViewRequest(previous => ({ id: detail.mistake_id!, panel, token: (previous?.token ?? 0) + 1 }))
      }
      if (detail.capture) setShowIntake(true)
      setRefreshToken(value => value + 1);void load(detail.mistake_id)
    }
    const clear = (event: Event): void => {
      if ((event as CustomEvent<{ childId: string }>).detail?.childId !== child.child_id) return
      generation.current++;foregroundLoading.current = false;setLibrary(null);setSelectedId(null);setAutoAnalysis(null);setShowIntake(true);setShowPrint(false);setLoading(false)
    }
    window.addEventListener(MISTAKE_LEARNING_REFRESH_EVENT, refresh)
    window.addEventListener('hsc-cloud-mistake-cleared', clear)
    return () => {
      window.removeEventListener(MISTAKE_LEARNING_REFRESH_EVENT, refresh)
      window.removeEventListener('hsc-cloud-mistake-cleared', clear)
    }
  }, [child.child_id, sessionId])
  useLayoutEffect(() => {
    if (!active) { announced.current = -1;return }
    if (loading || error || !library || announced.current === libraryRenderRequest) return
    const heading = surface.current?.querySelector('h1')
    heading?.scrollIntoView?.({ block: 'start', behavior: 'auto' })
    const frame = requestAnimationFrame(() => {
      const box = heading?.getBoundingClientRect()
      if (!box || box.width <= 0 || box.height <= 0 || box.top < 0 || box.bottom > window.innerHeight) {
        onUnavailable?.('错题本已读取，但标题尚未进入可见区域，尚未确认打开。');return
      }
      announced.current = libraryRenderRequest
      onReady?.(library.items.length ? 'ready' : 'empty')
      window.dispatchEvent(new CustomEvent(MISTAKE_LEARNING_RENDERED_EVENT, { detail: { kind: 'library', owner_id: bookOwner.current, child_id: child.child_id, session_id: sessionId, visible: true, view_rendered: true, view: tab === 'today' ? 'today' : 'all' } }))
    })
    return () => cancelAnimationFrame(frame)
  }, [active, loading, error, libraryRenderRequest, library, onReady, onUnavailable, tab, child.child_id, sessionId])
  useEffect(() => { if (active && error) onUnavailable?.(error) }, [active, error, onUnavailable])
  useEffect(() => {
    if (!active || !scopedTopicVoice) return
    if (tab !== 'topics') { setTab('topics');setFinishedTodayId(null);setFilters(emptyBookFilters) }
  }, [active, scopedTopicVoice, tab])
  const graphPendingCount = library?.graph_pending_count ?? 0
  const hasPendingGraph = graphPendingCount > 0
  useEffect(() => {
    if (!active || tab !== 'topics' || loading || error || !hasPendingGraph) { setGraphMatching(false);return }
    const controller = new AbortController()
    let previous = graphPendingCount
    let timer: ReturnType<typeof setTimeout>
    const step = async (): Promise<void> => {
      const next = (await load(undefined, controller.signal))?.graph_pending_count
      if (controller.signal.aborted) return
      if (next !== undefined && next > 0 && next < previous) {
        previous = next
        timer = setTimeout(() => void step(), 250)
      } else setGraphMatching(false)
    }
    setGraphMatching(true)
    timer = setTimeout(() => void step(), 250)
    return () => { clearTimeout(timer);controller.abort() }
    // This sweep owns the decreasing count. Changes that leave work pending
    // update the display without starting a second sweep.
  }, [active, tab, loading, error, hasPendingGraph, child.child_id, sessionId])

  const all = library?.items ?? []
  const due = all.filter(item => item.review_due && item.today.status !== 'done')
  const finishedToday = finishedTodayId ? all.find(item => item.mistake_id === finishedTodayId && !due.includes(item)) : undefined
  const todayItems = finishedToday ? [...due, finishedToday] : due
  const subjects = bookSubjects(child.grade, all)
  const causes = [...new Set(all.flatMap(bookCauses))]
  const items = filteredMistakes(tab === 'today' ? todayItems : all, filters, subjects).sort((a, b) => (b.source.created_at ?? '').localeCompare(a.source.created_at ?? ''))
  const current = items.find(item => item.mistake_id === selectedId) ?? items[0]
  selected.current = current?.mistake_id ?? null
  const changed = (state: MistakeLearningState): void => {
    if (tab === 'today' && state.mistake_id === selected.current && state.original_review?.status === 'retest_pending' && due.some(item => item.mistake_id === state.mistake_id)) {
      setFinishedTodayId(state.mistake_id);setSelectedId(state.mistake_id)
    }
    setLibrary(value => value ? { ...value, items: value.items.map(item => item.mistake_id === state.mistake_id ? state : item) } : value)
    if (state.analysis && autoAnalysis?.mistakeId === state.mistake_id) {
      try { clearMistakeLearningIntake(child.child_id) } catch { /* Server learning remains available. */ }
      setAutoAnalysis(null)
    }
  }
  const switchTab = (next: BookTab): void => {
    setTab(next);setFinishedTodayId(null);setFilters(emptyBookFilters)
    if (next === 'today') setSelectedId(due[0]?.mistake_id ?? null)
  }
  const openRelated = (id: string): void => { setTab('all');setFilters(emptyBookFilters);setSelectedId(id) }
  const refreshLibrary = (): void => { setRefreshToken(value => value + 1);void load() }
  const Surface = embedded ? 'section' : 'main'
  if (showPrint && active) return <MistakeBookPrint key={child.child_id} child={child} initialFilters={filters} onClose={() => setShowPrint(false)}/>
  return <Surface ref={surface} className="mistake-book-shell cloud-mistake-book">
    <header className="mistake-book-header"><button className="icon-button" aria-label={embedded ? '返回学习' : '返回首页'} onClick={onClose}><ArrowLeft size={20}/></button><div><span>{child.nickname} · {child.grade}</span><h1>智能错题本</h1></div><button className="secondary-button" disabled={loading} onClick={refreshLibrary}><RefreshCw size={17}/>刷新</button></header>
    <div className="cloud-book-intro"><div><h2>先做一遍，再把卡住的地方弄明白</h2><p>看原题、说思路或放上草稿，接着听讲解、练变式。</p></div><span>{due.length ? `今天有 ${due.length} 道错题需要复习` : '今天可以按自己的节奏复习'}</span></div>
    <nav className="mistake-tabs cloud-book-tabs" aria-label="错题本分类">
      <button type="button" aria-current={tab === 'all' ? 'page' : undefined} className={tab === 'all' ? 'is-active' : ''} onClick={() => switchTab('all')}>错题库 <b>{all.length}</b></button>
      <button type="button" aria-current={tab === 'today' ? 'page' : undefined} className={tab === 'today' ? 'is-active' : ''} onClick={() => switchTab('today')}>今日复习 <b>{due.length}</b></button>
      <button type="button" aria-current={tab === 'topics' ? 'page' : undefined} className={tab === 'topics' ? 'is-active' : ''} onClick={() => switchTab('topics')}>高频题型</button>
    </nav>
    <div className="cloud-book-add">
      <div>{documentMonitoring && <p className="cloud-book-device" role="status"><ScanLine size={16}/><strong>高拍仪</strong>{({ observing: '持续观察中', paused: '观察已暂停', unavailable: '暂未就绪' })[documentMonitoring.status]}</p>}</div>
      <div className="cloud-book-intake-actions">
        <button type="button" className="secondary-button" disabled={!active} onClick={() => setShowPrint(true)}>打印错题本</button>
        {onIntakeRequest && <><button type="button" className="secondary-button" disabled={!active} onClick={() => onIntakeRequest('camera')}><ScanLine size={16}/>{documentMonitoring?.pendingCount ? `核对 ${documentMonitoring.pendingCount} 条候选` : '高拍仪录入'}</button><button type="button" className="secondary-button" disabled={!active} onClick={() => onIntakeRequest('voice')}><Mic size={16}/>语音录入</button></>}
        <button type="button" className="secondary-button" aria-expanded={showIntake} onClick={() => setShowIntake(value => !value)}>{showIntake ? '收起手动录入' : '手动录入'}</button>
      </div>
    </div>
    {showIntake && <CloudMistakeIntake key={child.child_id} childId={child.child_id} sessionId={sessionId} active={active} onSaved={(mistakeId, analysisActionId) => { setAutoAnalysis({ mistakeId, actionId: analysisActionId });openRelated(mistakeId);setShowIntake(false);void load(mistakeId) }}/>}
    {error && <p role="alert" className="cloud-learning-notice">{error}<button className="secondary-button" disabled={loading} onClick={() => void load()}>重试读取</button></p>}
    {loading && !library && <p className="cloud-book-loading" role="status">正在读取错题和今日复习…</p>}
    {library && <>
      <MistakeBookFilters key={tab} items={tab === 'today' ? todayItems : all} subjects={subjects} causes={causes} value={filters} onChange={setFilters} onOpen={tab === 'topics' ? openRelated : setSelectedId}/>
      {active && tab === 'topics' && hasPendingGraph && <p role="status" className="cloud-book-loading">{graphMatching
        ? `正在本机匹配知识点，剩余 ${graphPendingCount} 道。`
        : `还有 ${graphPendingCount} 道待匹配，可以刷新继续。`}</p>}
      {tab === 'topics' ? <MistakeBookTopics childId={child.child_id} sessionId={sessionId} active={active} refreshToken={refreshToken}
        items={items} onOpen={openRelated} onRefresh={refreshLibrary} voiceRequest={loading || error ? null : scopedTopicVoice}
        onVoiceHandled={onTopicVoiceHandled} onSpeak={onTopicSpeech}/> : !all.length || !items.length ? <section className="cloud-book-empty"><BookOpenCheck size={36}/><h2>{all.length ? tab === 'today' && !due.length ? '今天没有到期的错题' : '没有找到符合条件的错题' : '从一道卡住的题开始'}</h2><p>{all.length ? '可以换个搜索条件，或打开错题库。' : '把批改过的题放到高拍仪下，也可以直接告诉小知，或手动输入题目。'}</p>{all.length > 0 && tab === 'today' && <button type="button" className="secondary-button" onClick={() => switchTab('all')}>查看错题库</button>}</section>
        : <div className="cloud-book-grid">
          <aside className="cloud-case-list" aria-label={tab === 'today' ? '今日复习题目' : '错题列表'}>
            <header><strong>{tab === 'today' ? '到期错题' : '错题列表'}</strong><small role="status">{items.length} 道</small></header>
            {items.map(item => <button type="button" aria-pressed={current?.mistake_id === item.mistake_id} className={current?.mistake_id === item.mistake_id ? 'is-active' : ''} key={item.mistake_id} onClick={() => setSelectedId(item.mistake_id)}>
              <span>{item.source.subject}{item.source.created_at ? ` · ${dateLabel(item.source.created_at)}` : ''}</span>
              <strong>{item.source.title || '一道待弄懂的题'}</strong><p>{item.source.question}</p>
              <small>{[reviewLabel(item), ...bookCauses(item).slice(0, 1)].filter(Boolean).join(' · ')}</small>
            </button>)}
          </aside>
          <div className="cloud-book-detail">{current && <CloudMistakeLearning key={`${child.child_id}:${current.mistake_id}:${sessionId ?? ''}`}
            childId={child.child_id} mistakeId={current.mistake_id} sessionId={sessionId} active={active} refreshToken={refreshToken} onChanged={changed} onLearningFocus={onLearningFocus}
            onCoachSpeech={onCoachSpeech}
            autoAnalyzeActionId={autoAnalysis?.mistakeId === current.mistake_id ? autoAnalysis.actionId : undefined}
            viewRequest={viewRequest?.id === current.mistake_id ? viewRequest : undefined} bookView onOriginalReviewRequest={onOriginalReviewRequest}/>}
          </div>
        </div>}
    </>}
  </Surface>
}

function dateLabel(date: string): string {
  const value = new Date(date)
  return Number.isFinite(value.getTime()) ? value.toLocaleDateString('zh-CN', { month: 'numeric', day: 'numeric' }) : ''
}

export function MistakeAction({ item, next, childId, busy, run, focusEnabled = true, sessionId }: {
  focusEnabled?: boolean
  sessionId?: string
  item: MistakeItem
  next: MistakeNextActivity | null
  childId: string
  busy: boolean
  run: (action: () => Promise<MistakeItem>, success: string) => Promise<void>
}): React.JSX.Element {
  const [errorType, setErrorType] = useState('concept_gap')
  const [explanation, setExplanation] = useState('')
  const [stepType, setStepType] = useState('concept_explanation')
  const [notes, setNotes] = useState('')
  const [answer, setAnswer] = useState('')
  const questionRef = useQuestionViewportFocus<HTMLParagraphElement>(
    focusEnabled && item.child_id === childId && next?.kind === 'attempt' && next.item
      ? `${childId}:${item.mistake_id}:${next.stage}:${next.item.diagnostic_item_id}` : null,
    () => !sessionId || !privacyStopPending(sessionId))

  useEffect(() => { setExplanation(''); setNotes(''); setAnswer('') }, [item.mistake_id, item.status, next?.stage])

  useLayoutEffect(() => {
    if(!focusEnabled)return
    const handle = (event: Event): void => {
      const detail = (event as CustomEvent<{
        action: 'review' | 'diagnose' | 'remediate' | 'answer'
        decision?: 'confirm' | 'exclude'
        errorType?: string
        explanation?: string
        outcome?: 'completed' | 'still_stuck'
        notes?: string
        answer?: string
        resolve: (handled: boolean) => void
      }>).detail
      const execute = async (): Promise<boolean> => {
        if (detail.action === 'review' && item.status === 'awaiting_confirmation' && detail.decision === 'exclude') {
          await run(() => localApi.confirmMistake(item.mistake_id, childId, 'exclude'), '识别错误的记录已经排除。')
          return true
        }
        if (detail.action === 'diagnose' && ['diagnosing', 'relapsed'].includes(item.status) && detail.explanation?.trim()) {
          setErrorType(detail.errorType ?? 'concept_gap'); setExplanation(detail.explanation.trim())
          await run(() => localApi.diagnoseMistake(item.mistake_id, childId, detail.errorType ?? 'concept_gap', detail.explanation!.trim(), 0.8), '错因先作为假设保存，接下来用新题验证。')
          return true
        }
        if (detail.action === 'remediate' && item.status === 'remediating' && detail.outcome) {
          const spokenNotes = detail.notes?.trim() || (detail.outcome === 'still_stuck' ? '孩子语音确认仍未理解' : '孩子语音确认能说清关键一步')
          setNotes(spokenNotes)
          await run(() => localApi.addMistakeRemediation(item.mistake_id, childId, stepType, detail.outcome!, spokenNotes), detail.outcome === 'completed' ? '补救已保存，下一步用新题独立验证。' : '已记录还没弄懂，不会继续追加同级题。')
          return true
        }
        if (detail.action === 'answer' && next?.kind === 'attempt' && next.item && next.stage && detail.answer?.trim()) {
          setAnswer(detail.answer.trim())
          await run(() => localApi.submitMistakeAttempt(item.mistake_id, childId, next.stage!, next.item!.diagnostic_item_id, detail.answer!.trim()), '回答已保存，正在更新强化进度。')
          return true
        }
        return false
      }
      void execute().then(detail.resolve).catch(() => detail.resolve(false))
    }
    window.addEventListener('hsc-mistake-voice-action', handle)
    return () => window.removeEventListener('hsc-mistake-voice-action', handle)
  }, [item.mistake_id, item.status, next?.kind, next?.stage, next?.item?.diagnostic_item_id, stepType, busy,focusEnabled])

  if (item.status === 'awaiting_confirmation') {
    return <div className="mistake-actions"><h3>正在核对评分依据</h3><p>系统会核对老师批改、对应版本的标准答案，或智能体出题时保存的评分规则。孩子不需要判断对错；只有题干、实际作答或题目归属识别错了才排除。</p><div><button className="secondary-button" disabled={busy} onClick={() => void run(() => localApi.confirmMistake(item.mistake_id, childId, 'exclude'), '识别错误的记录已经排除。')}>识别有误，排除记录</button></div></div>
  }
  if (item.status === 'diagnosing' || item.status === 'relapsed') {
    return <div className="mistake-actions"><h3>这次主要卡在哪里？</h3><label>错因候选<select value={errorType} onChange={(event) => setErrorType(event.target.value)}><option value="concept_gap">概念还没弄清</option><option value="prerequisite_gap">前置知识不稳</option><option value="reading_gap">没有读懂条件或问题</option><option value="method_gap">方法选择不对</option><option value="execution_error">计算、符号或单位执行错误</option><option value="retrieval_failure">当时没有想起来</option><option value="transfer_gap">换一种题目表达就不会</option><option value="incidental">偶然失误，仍需快速检查</option><option value="unknown">还需要更多证据</option></select></label><label>你怎么理解这次错误<textarea value={explanation} maxLength={1000} onChange={(event) => setExplanation(event.target.value)} placeholder="例如：我把b²算成了b×2，也没有检查判别式的符号。" /></label><button className="primary-button" disabled={busy || explanation.trim().length < 2} onClick={() => void run(() => localApi.diagnoseMistake(item.mistake_id, childId, errorType, explanation.trim(), 0.8), '错因先作为假设保存，接下来用新题验证。')}>保存诊断并开始补救</button></div>
  }
  if (item.status === 'remediating') {
    return <div className="mistake-actions"><h3>先补最关键的一步</h3><label>补救方式<select value={stepType} onChange={(event) => setStepType(event.target.value)}><option value="concept_explanation">重新理解概念</option><option value="prerequisite_review">回到前置知识</option><option value="problem_restatement">重新说清题意</option><option value="method_comparison">比较不同方法</option><option value="execution_check">检查计算、符号和单位</option><option value="active_recall">主动回忆关键内容</option><option value="representation_change">换一种图示或表达</option></select></label><label>刚才做了什么<textarea value={notes} maxLength={1000} onChange={(event) => setNotes(event.target.value)} placeholder="写下你现在能说清的关键一步。" /></label><div><button className="primary-button" disabled={busy || notes.trim().length < 2} onClick={() => void run(() => localApi.addMistakeRemediation(item.mistake_id, childId, stepType, 'completed', notes), '补救已保存，下一步用新题独立验证。')}>我能说清了，做一道新题</button><button className="secondary-button" disabled={busy} onClick={() => void run(() => localApi.addMistakeRemediation(item.mistake_id, childId, stepType, 'still_stuck', notes), '已记录还没弄懂，不会继续追加同级题。')}>还是不明白</button></div></div>
  }
  if (!next) return <div className="mistake-actions"><span className="loader" /><p>正在准备下一步...</p></div>
  if (next.kind === 'scheduled') return <div className="mistake-actions is-scheduled"><Clock3 size={22} /><h3>先让记忆离开即时线索</h3><p>下一次{stageLabel(next.stage)}安排在{next.due_at ? new Date(next.due_at).toLocaleString() : '稍后'}。现在不重复原题。</p></div>
  if (next.kind === 'blocked') return <div className="mistake-actions"><h3>当前没有合格变式题</h3><p>{next.reason}</p></div>
  if (item.status === 'mastered' || next.kind === 'mastered') return <div className="mistake-actions is-mastered"><CheckCircle2 size={25} /><h3>已经通过本轮掌握门槛</h3><p>以后还会低频抽查；如果再次出现同类错误，会保留这次进步并重新诊断。</p></div>
  if (next.kind === 'attempt' && next.item && next.stage) {
    return <div className="mistake-actions"><h3>{stageLabel(next.stage)}</h3><p ref={questionRef} className="mistake-question" data-question-viewport="mistake">{next.item.prompt}</p><label>你的回答<textarea value={answer} maxLength={4000} onChange={(event) => setAnswer(event.target.value)} /></label><button className="primary-button" disabled={busy || answer.trim().length < 1} onClick={() => void run(() => localApi.submitMistakeAttempt(item.mistake_id, childId, next.stage!, next.item!.diagnostic_item_id, answer.trim()), '回答已保存，正在更新强化进度。')}>提交回答</button></div>
  }
  return <div className="mistake-actions"><RotateCcw size={22} /><p>正在根据最新证据重新安排下一步。</p></div>
}

function stageLabel(value: string | null): string {
  return ({ repair_check: '关键一步检查', near_transfer: '同结构新题', representation_transfer: '换一种表达', discrimination: '易混题型辨析', far_transfer: '新场景迁移', delayed_48h: '48小时复测', delayed_7d: '7天复测' } as Record<string, string>)[value ?? ''] ?? '下一步验证'
}
