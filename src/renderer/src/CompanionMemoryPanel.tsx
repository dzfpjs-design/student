import { useEffect, useRef, useState } from 'react'
import { Check, RefreshCw, Search, Trash2, X } from 'lucide-react'

export interface Memory {
  memory_id: string
  content: string
  kind: string
  source_turn_id: string | null
  confidence: number
  created_at: string
  status: string
  source_text?: string | null
}

export interface CompanionMemoryPanelProps {
  childId: string
  /** Callbacks must already be authenticated and scoped to childId by the parent. */
  load: (query: string) => Promise<{ items: Memory[]; cloud_transmission_allowed: false }>
  forget: (memoryId: string) => Promise<void>
}

const kindLabels: Record<string, string> = {
  preference: '偏好', interest: '兴趣', goal: '目标', habit: '习惯',
  fact: '生活记录', experience: '经历', relationship: '人际关系',
  commitment: '约定', learning: '学习记录', emotion: '情绪记录',
  self_report: '孩子自述', event: '经历', learning_barrier: '学习困难自述',
}
const statusLabels: Record<string, string> = {
  active: '生效中', pending: '待核实', candidate: '待核实',
  confirmed: '已确认', forgotten: '已忘记', revoked: '已撤回',
  deleted: '已删除', expired: '已到期', superseded: '已更新',
}

function createdLabel(value: string): string {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '时间暂未提供' : date.toLocaleString('zh-CN', { hour12: false })
}

export function CompanionMemoryPanel(props: CompanionMemoryPanelProps): React.JSX.Element {
  // A new child gets a new lifetime, even for A -> B -> A switches with requests in flight.
  return <ChildMemoryPanel key={props.childId} {...props} />
}

function ChildMemoryPanel({ childId, load, forget }: CompanionMemoryPanelProps): React.JSX.Element {
  const [items, setItems] = useState<Memory[]>([])
  const [query, setQuery] = useState('')
  const [submittedQuery, setSubmittedQuery] = useState('')
  const [loading, setLoading] = useState(Boolean(childId))
  const [loadError, setLoadError] = useState(false)
  const [confirmId, setConfirmId] = useState<string | null>(null)
  const [forgettingId, setForgettingId] = useState<string | null>(null)
  const [forgetErrorId, setForgetErrorId] = useState<string | null>(null)
  const [message, setMessage] = useState<string | null>(null)
  const loadRef = useRef(load)
  const requestId = useRef(0)
  const mounted = useRef(false)
  const forgetting = useRef(false)

  // Inline callbacks from ParentCenter must not trigger a new read on every render.
  useEffect(() => { loadRef.current = load }, [load])

  const read = async (search: string): Promise<void> => {
    if (!childId || forgetting.current) return
    const id = ++requestId.current
    const normalizedQuery = search.trim()
    setSubmittedQuery(normalizedQuery)
    setLoading(true)
    setLoadError(false)
    setMessage(null)
    setConfirmId(null)
    setForgetErrorId(null)
    try {
      const response = await loadRef.current(normalizedQuery)
      if (!mounted.current || id !== requestId.current) return
      setItems(response.items)
    } catch {
      if (!mounted.current || id !== requestId.current) return
      setLoadError(true)
    } finally {
      if (mounted.current && id === requestId.current) setLoading(false)
    }
  }

  useEffect(() => {
    mounted.current = true
    void read('')
    return () => { mounted.current = false; requestId.current += 1 }
  }, [])

  const forgetMemory = async (memoryId: string): Promise<void> => {
    if (forgetting.current || loading) return
    forgetting.current = true
    setForgettingId(memoryId)
    setForgetErrorId(null)
    setMessage(null)
    try {
      await forget(memoryId)
      if (!mounted.current) return
      setItems((current) => current.filter((item) => item.memory_id !== memoryId))
      setConfirmId(null)
      setMessage('已忘记这条记忆。')
    } catch {
      if (mounted.current) setForgetErrorId(memoryId)
    } finally {
      forgetting.current = false
      if (mounted.current) setForgettingId(null)
    }
  }

  return (
    <section className="parent-panel" aria-label="孩子长期记忆" style={{ minWidth: 0, overflowWrap: 'anywhere' }}>
      <div className="panel-heading">
        <div><span>当前孩子</span><h2>长期记忆</h2></div>
        <button type="button" className="secondary-button" title="刷新记忆" aria-label="刷新记忆"
          disabled={!childId || loading || forgettingId !== null} onClick={() => void read(submittedQuery)}>
          <RefreshCw size={16} aria-hidden="true" />刷新
        </button>
      </div>
      {!childId ? <p className="muted-copy">请先选择孩子。</p> : <>
        <p className="muted-copy">记忆不允许传往云端。</p>
        <form role="search" aria-label="搜索当前孩子的记忆" className="controlled-content-search"
          style={{ flexWrap: 'wrap' }} onSubmit={(event) => { event.preventDefault(); void read(query) }}>
          <label style={{ minWidth: 0 }}>
            <Search size={16} aria-hidden="true" />
            <input type="search" aria-label="搜索记忆" placeholder="搜索记忆" value={query}
              style={{ minWidth: 0 }} disabled={forgettingId !== null} onChange={(event) => setQuery(event.target.value)} />
          </label>
          <button type="submit" className="secondary-button" disabled={forgettingId !== null}>
            <Search size={16} aria-hidden="true" />搜索
          </button>
          {(query || submittedQuery) && <button type="button" className="icon-button" title="清空搜索" aria-label="清空搜索"
            disabled={forgettingId !== null} onClick={() => { setQuery(''); void read('') }}><X size={16} aria-hidden="true" /></button>}
        </form>
        {loading && <p role="status" className="muted-copy">正在读取记忆…</p>}
        {loadError && <div className="tutor-error" role="alert">
          <p>记忆暂时读取失败，请重试。</p>
          <button type="button" className="secondary-button" onClick={() => void read(submittedQuery)}>
            <RefreshCw size={16} aria-hidden="true" />重试读取
          </button>
        </div>}
        {message && <p role="status" className="operation-message"><Check size={15} aria-hidden="true" />{message}</p>}
        {!loading && !loadError && <>
          {items.length === 0 ? <p className="muted-copy">{submittedQuery ? '没有找到相关记忆。' : '这个孩子还没有长期记忆。'}</p> :
            <div className="goal-cycle-list">{items.map((item) => <article key={item.memory_id} style={{ minWidth: 0 }}>
              <p style={{ whiteSpace: 'pre-wrap' }}>{item.content}</p>
              <p className="muted-copy">{kindLabels[item.kind] ?? '其他记忆'} · {statusLabels[item.status] ?? '状态待确认'}</p>
              <details>
                <summary>来源详情</summary>
                <dl>
                  <dt>来源</dt><dd>{item.source_turn_id ? '陪伴对话' : '来源暂未提供'}</dd>
                  <dt>记录时间</dt><dd>{createdLabel(item.created_at)}</dd>
                  <dt>置信度</dt><dd>{Number.isFinite(item.confidence) && item.confidence >= 0 && item.confidence <= 1
                    ? `${Math.round(item.confidence * 100)}%（不代表已核实）` : '置信度暂未提供'}</dd>
                  <dt>对话原文</dt><dd style={{ whiteSpace: 'pre-wrap' }}>{item.source_text?.trim() || '原始对话暂未提供。'}</dd>
                </dl>
              </details>
              {forgetErrorId === item.memory_id && <p role="alert" className="tutor-error">没有忘记成功，原记忆仍保留。请重试。</p>}
              {confirmId === item.memory_id ? <div>
                <p>确认忘记这条记忆？</p>
                <div className="review-actions" style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
                  <button type="button" className="secondary-button" disabled={forgettingId !== null} onClick={() => void forgetMemory(item.memory_id)}>
                    <Trash2 size={16} aria-hidden="true" />{forgettingId === item.memory_id ? '正在忘记' : '确认忘记'}
                  </button>
                  <button type="button" className="secondary-button" disabled={forgettingId !== null}
                    onClick={() => { setConfirmId(null); setForgetErrorId(null) }}>取消</button>
                </div>
              </div> : !['forgotten', 'deleted', 'revoked'].includes(item.status) &&
                <button type="button" className="secondary-button" disabled={forgettingId !== null}
                  onClick={() => { setConfirmId(item.memory_id); setForgetErrorId(null); setMessage(null) }}>
                  <Trash2 size={16} aria-hidden="true" />忘记这条记忆
                </button>}
            </article>)}</div>}
        </>}
      </>}
    </section>
  )
}
