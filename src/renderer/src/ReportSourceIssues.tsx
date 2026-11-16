import { useEffect, useState } from 'react'
import { requestParentLocal } from './api'

type SourceIssue = {
  issue_id: string; child_id: string; source_id: string; status: 'open' | 'resolved'
  report_type?: 'daily' | 'weekly' | 'monthly'; report_date?: string
  snapshot_id?: string | null; message: string; recovery_hint: string
}
type Props = { childId: string; ready: boolean; refreshVersion: number; onOpen: (id: string) => Promise<void> }

export function ReportSourceIssues(props: Props): React.JSX.Element {
  return <IssueList key={props.childId} {...props} />
}

function IssueList({ childId, ready, refreshVersion, onOpen }: Props): React.JSX.Element {
  const [items, setItems] = useState<SourceIssue[]>([])
  const [error, setError] = useState('')
  const [opening, setOpening] = useState(false)
  useEffect(() => {
    if (!ready) return
    let current = true
    void requestParentLocal<{ child_id: string; items: SourceIssue[] }>(
      `/v1/parent/children/${encodeURIComponent(childId)}/report-source-issues`, { cache: 'no-store' },
    ).then(result => {
      if (!current) return
      if (result.child_id !== childId || !Array.isArray(result.items) || result.items.some(item => item.child_id !== childId)) {
        throw new Error('报告来源归属未能核对。')
      }
      setItems(result.items.filter(item => item.status === 'open'))
      setError('')
    }).catch(() => {
      if (current) setError('历史报告来源问题尚未读取，请刷新报告后重试；不能据此认为没有问题。')
    })
    return () => { current = false }
  }, [childId, ready, refreshVersion])
  const open = async (id: string): Promise<void> => {
    setOpening(true)
    try { await onOpen(id); setError('') }
    catch { setError('原报告暂不能打开，问题记录仍保留。请刷新后核对。') }
    finally { setOpening(false) }
  }
  return <>{error && <p role="status">{error}</p>}{items.length > 0 && <section aria-label="历史报告来源问题">
    <h3>历史报告需要核对</h3>
    <ul>{items.map(item => <li key={item.issue_id} data-work-object={item.source_id}>
      <strong>{item.report_date ?? '历史记录'} · {{ daily: '日报', weekly: '周报', monthly: '月报' }[item.report_type ?? 'daily']}</strong>
      <p>{item.message}</p><p>{item.recovery_hint}</p>
      {item.snapshot_id && <button type="button" disabled={opening} onClick={() => { void open(item.snapshot_id!) }}>查看保留的原报告</button>}
    </li>)}</ul>
  </section>}</>
}
