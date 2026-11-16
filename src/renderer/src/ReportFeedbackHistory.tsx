import { useEffect, useRef, useState } from 'react'
import { RefreshCw, Undo2 } from 'lucide-react'
import { localApi } from './api'
import type { ReportFeedbackRevision } from './types'

type Props = { childId: string; snapshotId: string; onChanged: () => Promise<void> }

export function ReportFeedbackHistory({ childId, snapshotId, onChanged }: Props): React.JSX.Element {
  const [opened, setOpened] = useState(false)
  const [items, setItems] = useState<ReportFeedbackRevision[] | null>(null)
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState('')
  const epoch = useRef(0)
  useEffect(() => {
    epoch.current += 1
    setItems(null); setOpened(false); setBusy(false); setMessage('')
    return () => { epoch.current += 1 }
  }, [childId, snapshotId])

  async function refresh(): Promise<void> {
    const generation = ++epoch.current
    setBusy(true); setMessage('')
    try {
      const rows = await localApi.reportFeedbackHistory(childId, snapshotId)
      if (generation === epoch.current) setItems(rows)
    } catch {
      if (generation === epoch.current) setMessage('反馈记录暂不可用，请重试。')
    } finally {
      if (generation === epoch.current) setBusy(false)
    }
  }

  async function retract(revision: number): Promise<void> {
    const generation = ++epoch.current
    setBusy(true); setMessage('')
    try {
      await localApi.retractReportFeedback(childId, snapshotId, revision)
      if (generation !== epoch.current) return
      const rows = await localApi.reportFeedbackHistory(childId, snapshotId)
      if (generation !== epoch.current) return
      setItems(rows); setMessage('这次评价已撤回，历史记录保留。')
      await onChanged()
    } catch {
      if (generation === epoch.current) setMessage('尚未确认撤回结果。请刷新核对，或重试撤回；不会重复保存。')
    } finally {
      if (generation === epoch.current) setBusy(false)
    }
  }

  const latest = items?.reduce<ReportFeedbackRevision | null>((last, row) => !last || row.revision > last.revision ? row : last, null)
  return <details open={opened} onToggle={event => {
    const value = event.currentTarget.open
    setOpened(value)
    if (value && items === null && !busy) void refresh()
  }}>
    <summary>已保存的评价与撤回</summary>
    {opened && <div aria-busy={busy}>
      {message && <p role="status">{message}</p>}
      {busy && <p role="status">正在核对反馈记录…</p>}
      {!busy && items?.length === 0 && <p>还没有已保存的评价。</p>}
      <ul>{items?.map(row => <li key={row.event_id}>
        <time dateTime={row.created_at}>{new Date(row.created_at).toLocaleString('zh-CN')}</time>
        {row.operation === 'retract' ? <p>评价已撤回：{row.payload.reason}</p> : <>
          <p>{row.revision === latest?.revision ? '当前评价' : '历史评价'}：帮助程度{row.payload.helpfulness ?? '未评价'}，沟通压力{row.payload.stress_rating ?? '未评价'}</p>
          {row.payload.notes && <p>{row.payload.notes}</p>}
        </>}
      </li>)}</ul>
      <button type="button" title="刷新反馈记录" aria-label="刷新反馈记录" disabled={busy} onClick={() => void refresh()}><RefreshCw size={16} /></button>
      {latest?.operation === 'record' && <button type="button" disabled={busy} onClick={() => void retract(latest.revision)}><Undo2 size={16} aria-hidden="true" />撤回这次评价</button>}
    </div>}
  </details>
}
