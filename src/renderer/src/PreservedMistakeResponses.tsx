import { useEffect, useRef, useState } from 'react'
import { RefreshCw } from 'lucide-react'
import { localApi } from './api'

type Item = Awaited<ReturnType<typeof localApi.preservedMistakeResponses>>['items'][number]

export function PreservedMistakeResponses({ childId, mistakeId }: { childId: string; mistakeId: string }): React.JSX.Element {
  return <History key={`${childId}:${mistakeId}`} childId={childId} mistakeId={mistakeId} />
}

function History({ childId, mistakeId }: { childId: string; mistakeId: string }): React.JSX.Element {
  const [open, setOpen] = useState(false)
  const [attempt, setAttempt] = useState(0)
  const [items, setItems] = useState<Item[] | null>(null)
  const [error, setError] = useState(false)
  const generation = useRef(0)
  useEffect(() => {
    if (!open) return
    const current = ++generation.current
    setItems(null)
    setError(false)
    void localApi.preservedMistakeResponses(childId, mistakeId).then(result => {
      if (generation.current !== current) return
      if (!Array.isArray(result.items) || result.items.some(item => item.child_id !== childId || item.mistake_id !== mistakeId
        || item.source_erased !== true || item.score_authority !== 'none' || item.response_status !== 'historical_only'
        || typeof item.response_text !== 'string')) throw new Error('HISTORY_SCOPE_CHANGED')
      setItems(result.items)
    }).catch(() => { if (generation.current === current) setError(true) })
    return () => { generation.current++ }
  }, [open, attempt, childId, mistakeId])
  return <details onToggle={event => setOpen(event.currentTarget.open)}>
    <summary>来源删除后的历史作答</summary>
    {open && <div style={{ minWidth: 0, overflowWrap: 'anywhere' }}>
      {error ? <p role="alert">历史作答未能读取，未展示过期或其他孩子的记录。
        <button type="button" className="secondary-button" onClick={() => setAttempt(value => value + 1)}><RefreshCw size={16} />重试</button></p>
        : items === null ? <p role="status">正在读取历史作答…</p>
          : items.length === 0 ? <p>没有此类历史作答。</p>
            : <><p>来源资料已删除。以下保留孩子当时的回答，不作为掌握程度的依据。</p>
              {items.map(item => <section key={item.response_id}>
                <small>原作答时间 · {new Date(item.responded_at).toLocaleString('zh-CN')}</small>
                <p style={{ whiteSpace: 'pre-wrap' }}>{item.response_text}</p>
              </section>)}</>}
    </div>}
  </details>
}
