import { useCallback, useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { ApiError } from './api'
import { privacyStopPending } from './deviceCommandReceipt'
import { readMistakeTopic, type MistakeTopicLesson, type MistakeTopicScope } from './mistakeTopicApi'
import type { MistakeLearningState } from './mistakeLearningTypes'

type Topic = { key: string; label: string; subject: string; graphNodeKey?: string; items: MistakeLearningState[] }
export type MistakeTopicVoiceRequest = { id: string; childId: string; sessionId: string; explain: boolean }
export type MistakeTopicSpeech = (text: string, scope: AbortSignal) => void
type TopicProps = {
  childId: string
  sessionId?: string
  active: boolean
  refreshToken: number
  items: MistakeLearningState[]
  onOpen: (id: string) => void
  onRefresh: () => void
  voiceRequest?: MistakeTopicVoiceRequest | null
  onVoiceHandled?: (id: string) => void
  onSpeak?: MistakeTopicSpeech
}

function topicSources(topic: Topic, examples: MistakeLearningState[]): MistakeTopicScope['sources'] {
  return examples.map(({ mistake_id, source_revision, graph_context }) => ({
    mistake_id, source_revision, ...(topic.graphNodeKey && graph_context ? { graph_version: graph_context.graph_version } : {}),
  }))
}

function topicScopeKey(topic: Topic, examples: MistakeLearningState[]): string {
  return JSON.stringify([topic.key, topic.label, topicSources(topic, examples), examples.map(item => item.graph_context ?? null)])
}

function groupTopics(items: MistakeLearningState[]): Topic[] {
  const groups = new Map<string, Topic>()
  for (const item of items) {
    const graph = item.graph_context
    const graphSubject = typeof graph?.subject === 'string' ? graph.subject.trim() : ''
    const originalSubject = item.source.subject.trim()
    const subject = graph?.status === 'matched' ? graphSubject || originalSubject : originalSubject || graphSubject
    if (!subject) continue
    const subjectKey = subject.normalize('NFKC').toLowerCase()
    if (graph?.status === 'matched') {
      const primary = graph.primary
      if (typeof primary?.key !== 'string' || !primary.key || typeof primary.title !== 'string' || !primary.title.trim()
        || typeof graph.graph_version !== 'string' || !graph.graph_version) continue
      const key = JSON.stringify([subjectKey, 'graph', primary.key])
      const group = groups.get(key)
      if (group) group.items.push(item)
      else groups.set(key, { key, label: primary.title.trim(), subject, graphNodeKey: primary.key, items: [item] })
      continue
    }
    const points = new Set<string>()
    for (const point of item.analysis?.knowledge_points ?? []) {
      const label = point.label.trim()
      const normalized = label.normalize('NFKC').toLowerCase()
      if (!label || points.has(normalized)) continue
      points.add(normalized)
      const key = JSON.stringify([subjectKey, 'label', normalized])
      const group = groups.get(key)
      if (group) group.items.push(item)
      else groups.set(key, { key, label, subject, items: [item] })
    }
  }
  return [...groups.values()].sort((a, b) => b.items.length - a.items.length)
}

export function MistakeBookTopics({ childId, sessionId, active, refreshToken, items, onOpen, onRefresh, voiceRequest, onVoiceHandled, onSpeak }: TopicProps): React.JSX.Element {
  const [openTopic, setOpenTopic] = useState<{ key: string; voice?: { id: string; sources: string } } | null>(null)
  const handled = useRef<string | null>(null)
  const speechScope = useRef<AbortController | null>(null)
  useLayoutEffect(() => {
    if (!active) { setOpenTopic(topic => topic?.voice ? { key: topic.key } : topic); return }
    const scope = new AbortController()
    speechScope.current = scope
    return () => { scope.abort();speechScope.current = null }
  }, [active])
  const ranked = useMemo(() => groupTopics(items), [items])
  useEffect(() => {
    const speech = speechScope.current?.signal
    if (!active || !speech || !voiceRequest || handled.current === voiceRequest.id) return
    handled.current = voiceRequest.id
    onVoiceHandled?.(voiceRequest.id)
    if (!voiceRequest.explain) {
      onSpeak?.('高频题型已打开。', speech)
      return
    }
    const eligible = ranked.filter(topic => topic.items.length >= 2)
    const selected = eligible.find(topic => topic.key === openTopic?.key) ?? eligible[0]
    if (selected) setOpenTopic({ key: selected.key, voice: { id: voiceRequest.id, sources: topicScopeKey(selected, selected.items.slice(0, 6)) } })
    else onSpeak?.('这个范围还没有两道能归入同一知识点的错题。可以扩大筛选范围，或继续添加、分析错题。', speech)
  }, [active, voiceRequest, onVoiceHandled, onSpeak, ranked, openTopic?.key])
  if (!ranked.length) return <section className="cloud-book-empty"><h2>还没有可归类的错题</h2><p>添加错题后，本机会尝试匹配知识点，也可以通过错因分析补充标签。</p></section>

  return <section className="cloud-book-topic-list" aria-label="高频题型排行">
    <p>按当前筛选范围汇总。匹配知识图谱的题按同一知识点归组，其他题按分析标签单独归组。每组至少有两道题时，可以打开云端专题。</p>
    {ranked.map((topic, index) => {
      const isOpen = openTopic?.key === topic.key && topic.items.length >= 2
      const examples = topic.items.slice(0, 6)
      const sources = topicSources(topic, examples)
      const exampleKey = topicScopeKey(topic, examples)
      const voiceId = isOpen && openTopic.voice?.sources === exampleKey ? openTopic.voice.id : undefined
      return <article key={topic.key}>
        <header><span>{String(index + 1).padStart(2, '0')}</span><h2>{topic.label}</h2><small>{topic.subject} · {topic.items.length} 道错题 · {topic.graphNodeKey ? '知识图谱' : '按分析标签'}</small></header>
        <div>
          {topic.items.length >= 2 && <button type="button" className="primary-button" aria-expanded={isOpen} disabled={!active}
            onClick={() => setOpenTopic(isOpen ? null : { key: topic.key })}>{isOpen ? '收起专题' : '打开云端专题'}</button>}
          {!isOpen && topic.items.slice(0, 4).map(item => <button type="button" className="secondary-button" key={item.mistake_id}
            onClick={() => onOpen(item.mistake_id)}>{item.source.title || '查看代表错题'}</button>)}
        </div>
        {isOpen && <MistakeTopicPanel key={exampleKey} childId={childId} sessionId={sessionId} active={active}
          refreshToken={refreshToken} topic={topic} examples={examples} sources={sources} onOpen={onOpen} onRefresh={onRefresh} voiceId={voiceId} onSpeak={onSpeak}/>}
      </article>
    })}
  </section>
}

type TopicState =
  | { status: 'checking' | 'generating' | 'missing' | 'stopped' }
  | { status: 'error'; message: string; retry: 'refresh' | 'review' | 'read' }
  | { status: 'ready'; lesson: MistakeTopicLesson }

function topicError(error: unknown): { message: string; retry: 'refresh' | 'review' | 'read' } {
  const code = error instanceof ApiError ? error.code ?? '' : ''
  if (code === 'MISTAKE_TOPIC_GRAPH_STALE') {
    return { message: '知识图谱已更新。打开相关错题，重新分析后再读专题。', retry: 'review' }
  }
  if (/SOURCE_CHANGED|SOURCE_MISMATCH|REQUEST_INVALID/.test(code)) {
    return { message: '专题里的错题已有变化，请刷新错题本。', retry: 'refresh' }
  }
  if (/CONSENT|PRIVACY|PERMISSION/.test(code)) {
    return { message: '云端学习暂未开启，请让家长检查设置。', retry: 'read' }
  }
  if (code === 'MISTAKE_TOPIC_SENSITIVE_SOURCE') {
    return { message: '这组题里有需要留在本机的信息。可以调整筛选范围，另选一组错题。', retry: 'read' }
  }
  return { message: '暂时没有读到这份专题。可以重试读取，已有进度会保留。', retry: 'read' }
}

type PanelProps = Omit<TopicProps, 'items' | 'voiceRequest' | 'onVoiceHandled'> & {
  topic: Topic
  examples: MistakeLearningState[]
  sources: MistakeTopicScope['sources']
  voiceId?: string
}

function MistakeTopicPanel({ childId, sessionId, active, refreshToken, topic, examples, sources, onOpen, onRefresh, voiceId, onSpeak }: PanelProps): React.JSX.Element {
  const heading = useId()
  // The panel key changes whenever its topic or any selected source revision changes.
  const [scope] = useState<MistakeTopicScope>(() => ({ session_id: sessionId, subject: topic.subject, knowledge_point: topic.label,
    ...(topic.graphNodeKey ? { knowledge_node_key: topic.graphNodeKey } : {}), sources }))
  const [view, setView] = useState<TopicState>({ status: 'checking' })
  const [paused, setPaused] = useState(() => Boolean(sessionId && privacyStopPending(sessionId)))
  const inFlight = useRef<AbortController | null>(null)
  const playback = useRef<AbortController | null>(null)
  const deliveredVoice = useRef<string | undefined>(undefined)
  const speakRef = useRef(onSpeak)
  speakRef.current = onSpeak
  const cancel = useCallback(() => { inFlight.current?.abort();inFlight.current = null;playback.current?.abort() }, [])

  const speak = useCallback((text: string): void => {
    playback.current?.abort()
    const controller = new AbortController()
    playback.current = controller
    speakRef.current?.(text, controller.signal)
  }, [])

  const playLesson = useCallback((lesson: MistakeTopicLesson): void => {
    speak(`${topic.label}专题。${lesson.summary} ${lesson.patterns[0]?.explanation ?? ''} 你可以先想一想。${lesson.socratic_question}`)
  }, [speak, topic.label])

  const load = useCallback(async (reuseOnly: boolean, requestedVoice?: string): Promise<void> => {
    if (!active || paused || sessionId && privacyStopPending(sessionId) || inFlight.current) return
    const controller = new AbortController()
    inFlight.current = controller
    setView({ status: reuseOnly ? 'checking' : 'generating' })
    try {
      const receipt = await readMistakeTopic(childId, scope, reuseOnly, controller.signal)
      if (controller.signal.aborted) return
      setView(receipt.status === 'ready' ? { status: 'ready', lesson: receipt.explanation } : { status: 'missing' })
      if (requestedVoice && receipt.status === 'ready') { deliveredVoice.current = requestedVoice;playLesson(receipt.explanation) }
    } catch (error) {
      if (!controller.signal.aborted) {
        const notice = topicError(error)
        setView({ status: 'error', ...notice })
        if (requestedVoice) { deliveredVoice.current = requestedVoice;speak(notice.message) }
      }
    } finally {
      if (inFlight.current === controller) inFlight.current = null
    }
  }, [active, paused, sessionId, childId, scope, speak, playLesson])

  useEffect(() => {
    if (active && !paused) {
      const requestedVoice = voiceId && voiceId !== deliveredVoice.current ? voiceId : undefined
      void load(!requestedVoice, requestedVoice)
    }
    return cancel
  }, [active, paused, refreshToken, voiceId, load, cancel])
  useEffect(() => {
    const pause = (): void => { cancel();setPaused(true);setView({ status: 'stopped' }) }
    window.addEventListener('hsc-privacy-pause', pause)
    return () => window.removeEventListener('hsc-privacy-pause', pause)
  }, [cancel])

  const labels = new Map(examples.map((item, index) => [item.mistake_id, item.source.title.trim() || `错题 ${index + 1}`]))
  return <section className="cloud-topic-panel" aria-labelledby={heading}>
    <h3 id={heading}>{topic.label}专题讲解</h3>
    <p>选用当前范围内最近的 {examples.length} 道错题。</p>
    {!active || paused ? <p role="status">专题已暂停。</p> : <>
      {view.status === 'checking' && <p role="status">正在读取专题…</p>}
      {view.status === 'generating' && <><p role="status">小知正在比较这些错题，整理共性错因和练习步骤…</p><button type="button" className="secondary-button" onClick={() => { cancel();setView({ status: 'stopped' }) }}>停止等待</button></>}
      {view.status === 'missing' && <button type="button" className="primary-button" onClick={() => void load(false)}>生成云端专题</button>}
      {view.status === 'stopped' && <><p role="status">已停止等待。可以稍后读取这份专题。</p><button type="button" className="secondary-button" onClick={() => void load(true)}>读取专题</button></>}
      {view.status === 'error' && <><p role="alert" className="cloud-learning-notice">{view.message}</p>
        {view.retry === 'review' ? <div className="cloud-topic-sources" aria-label="重新分析相关错题">{examples.map(item => <button type="button" className="secondary-button"
          key={item.mistake_id} onClick={() => onOpen(item.mistake_id)}>{labels.get(item.mistake_id)}</button>)}</div> : <button type="button" className="secondary-button"
          onClick={view.retry === 'refresh' ? onRefresh : () => void load(true)}>{view.retry === 'refresh' ? '刷新错题本' : '重试读取专题'}</button>}</>}
      {view.status === 'ready' && <>
        <p className="cloud-topic-summary">{view.lesson.summary}</p>
        {onSpeak && <button type="button" className="secondary-button" onClick={() => playLesson(view.lesson)}>听专题讲解</button>}
        <h4>这些题有什么共同点</h4>
        <ul className="cloud-topic-patterns">{view.lesson.patterns.map((pattern, index) => <li key={index}>
          <strong>{pattern.label}</strong><p>{pattern.explanation}</p>
          <div className="cloud-topic-sources" aria-label="这条讲解关联的原题">{pattern.mistake_ids.map(id => <button type="button" className="secondary-button"
            key={id} onClick={() => onOpen(id)}>{labels.get(id)}</button>)}</div>
        </li>)}</ul>
        <section className="cloud-topic-practice">
          <h4>先说说你的思路</h4><p>{view.lesson.socratic_question}</p>
          <details><summary>想好后，对照练习步骤</summary><ol>{view.lesson.learning_steps.map((step, index) => <li key={index}>{step}</li>)}</ol></details>
        </section>
      </>}
    </>}
  </section>
}
