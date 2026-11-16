import { useEffect, useState } from 'react'
import { TextbookLanguageCandidateReview } from './TextbookLanguageCandidateReview'

type Relation = 'covers' | 'explains' | 'exemplifies' | 'assesses'
type QueueItem = {
  resource_id: string; node_id: string; relation_type: Relation; max_confidence: number; evidence_segment_count: number
  knowledge_point_title: string; canonical_key: string; grade_scope?: string; track?: string; resource_title: string
  resource_source_ref: string; mapping_sources: string[]; mapping_source_count: number; license_boundary: string
}
type ReviewDecision = 'approved' | 'rejected' | 'needs_split'
type RecentDecision = Pick<QueueItem, 'resource_id' | 'node_id' | 'relation_type' | 'knowledge_point_title' | 'resource_title'> & { review_key: string, reviewer: string, decision: ReviewDecision | 'mixed_legacy', decision_values: ReviewDecision[], decided_at: string, affected_segment_count: number }
type Evidence = { sequence: number, source: string, preview: string }
type QueueSummary = { pending_resource_review_items: number, pending_resources: number, pending_evidence_segments: number, review_items_per_resource_p95: number }
type Props = { api: (path: string, init?: RequestInit) => Promise<unknown>; reviewer: string; childId?: string; showMaterialCapture?: boolean }

const itemKey = (item: Pick<QueueItem, 'resource_id' | 'node_id' | 'relation_type'>) => `${item.resource_id}:${item.node_id}:${item.relation_type}`
const commandKey = (reviewer: string, item: Pick<QueueItem, 'resource_id' | 'node_id' | 'relation_type'>, decision: string) =>
  `home-study.kg.resource-review.${encodeURIComponent(reviewer)}.${encodeURIComponent(itemKey(item))}.${decision}`
function stableActionId(reviewer: string, item: Pick<QueueItem, 'resource_id' | 'node_id' | 'relation_type'>, decision: string): string {
  const key = commandKey(reviewer, item, decision); const stored = window.localStorage.getItem(key)
  if (stored) return stored
  const id = `kg-resource-review-${crypto.randomUUID()}`; window.localStorage.setItem(key, id); return id
}
function clearStableActionId(reviewer: string, item: Pick<QueueItem, 'resource_id' | 'node_id' | 'relation_type'>, decision: string) { window.localStorage.removeItem(commandKey(reviewer, item, decision)) }
const decisionLabel = (decision: ReviewDecision) => decision === 'approved' ? '已接受' : decision === 'rejected' ? '已拒绝' : '需拆分'

/** Parent-only review UI. Internal identifiers remain in component state and are never rendered. */
export function KnowledgeGraphReviewQueue({ api, reviewer, childId, showMaterialCapture = true }: Props) {
  const [items, setItems] = useState<QueueItem[]>([]); const [recent, setRecent] = useState<RecentDecision[]>([])
  const [evidence, setEvidence] = useState<Record<string, Evidence[]>>({}); const [summary, setSummary] = useState<QueueSummary | null>(null); const [busy, setBusy] = useState(false); const [message, setMessage] = useState('')
  const load = async () => {
    setBusy(true)
    try {
      const [queue, history] = await Promise.all([
        api('/v1/parent/knowledge-graph/resource-review-queue') as Promise<{ items: QueueItem[], summary?: QueueSummary }>,
        api(`/v1/parent/knowledge-graph/resource-review-history?reviewer=${encodeURIComponent(reviewer)}`) as Promise<{ items: RecentDecision[] }>,
      ])
      setItems(queue.items); setSummary(queue.summary || null); setRecent(history.items); setMessage(queue.items.length ? '' : '当前没有待审核的讲解映射。')
    } catch { setMessage('审核队列暂时无法加载，可稍后重试。') } finally { setBusy(false) }
  }
  useEffect(() => { void load() }, [])
  const decide = async (item: QueueItem, decision: ReviewDecision) => {
    setBusy(true)
    try {
      const client_action_id = stableActionId(reviewer, item, decision)
      await api('/v1/parent/knowledge-graph/resource-mapping-reviews', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ resource_id: item.resource_id, node_id: item.node_id, relation_type: item.relation_type, decision, reviewer, client_action_id }) })
      clearStableActionId(reviewer, item, decision)
      setMessage(decision === 'approved' ? '已批准：该讲解可用于学生学习路径。' : decision === 'rejected' ? '已拒绝：该资源的这些证据片段不会再被学生召回。' : '已标记需拆分，等待拆分后重新提交。')
      await load()
    } catch { setMessage('保存未确认；会保留本次命令，重试不会重复写入。') } finally { setBusy(false) }
  }
  const undo = async (item: RecentDecision) => {
    setBusy(true)
    try {
      const client_action_id = stableActionId(reviewer, item, 'undo')
      await api('/v1/parent/knowledge-graph/resource-mapping-reviews/undo', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ resource_id: item.resource_id, node_id: item.node_id, relation_type: item.relation_type, reviewer, client_action_id, note: '家长撤销资源级决定' }) })
      clearStableActionId(reviewer, item, 'undo'); setMessage('已撤销。该资源的这些证据片段恢复为待审核，学生仍不会看到它。'); await load()
    } catch { setMessage('撤销未确认；会保留本次命令，重试不会重复写入。') } finally { setBusy(false) }
  }
  const showEvidence = async (item: QueueItem) => {
    const key = itemKey(item)
    if (evidence[key]) { setEvidence(current => { const next = { ...current }; delete next[key]; return next }); return }
    setBusy(true)
    try {
      const params = new URLSearchParams({ resource_id: item.resource_id, node_id: item.node_id, relation_type: item.relation_type })
      const response = await api(`/v1/parent/knowledge-graph/resource-review-evidence?${params.toString()}`) as { items: Evidence[] }
      setEvidence(current => ({ ...current, [key]: response.items }))
    } catch { setMessage('本机证据暂时无法加载，可稍后重试。') } finally { setBusy(false) }
  }
  return <section aria-label="知识图谱审核队列" className="knowledge-graph-review-queue">
    <header><h2>讲解映射质量校准</h2><p>候选只在这里显示。学生端仅使用家长或产品已批准、且带来源与许可边界的本机讲解。</p>{summary && <p>待审核 {summary.pending_resource_review_items} 项资源级映射，覆盖 {summary.pending_resources} 份资源、{summary.pending_evidence_segments} 个证据片段。本页最多显示 50 项；审核后会继续加载。</p>}<button disabled={busy} onClick={() => void load()}>刷新队列</button></header>
    {message && <p role="status">{message}</p>}
    {items.map(item => <article key={itemKey(item)}><h3>{item.knowledge_point_title}</h3><p>{item.grade_scope || '范围待补充'} · {item.track || '路径待补充'} · 最高置信度 {Math.round(item.max_confidence * 100)}%</p><p><strong>{item.resource_title}</strong> · {item.evidence_segment_count} 个证据片段</p><p>候选来源（{item.mapping_source_count} 类）：{item.mapping_sources.join('、')}；素材来源：{item.resource_source_ref}；使用边界：{item.license_boundary}</p><button disabled={busy} onClick={() => void showEvidence(item)}>{evidence[itemKey(item)] ? '收起本机证据' : '查看本机证据'}</button>{evidence[itemKey(item)] && <ol aria-label="本机证据片段">{evidence[itemKey(item)].map(row => <li key={`${row.sequence}-${row.source}`}><small>片段 {row.sequence} · {row.source}</small><p>{row.preview}</p></li>)}</ol>}<div><button disabled={busy} onClick={() => void decide(item, 'approved')}>接受</button><button disabled={busy} onClick={() => void decide(item, 'rejected')}>拒绝</button><button disabled={busy} onClick={() => void decide(item, 'needs_split')}>需拆分</button></div></article>)}
    {recent.length > 0 && <section aria-label="最近资源级审核"><h3>最近审核</h3>{recent.map(item => <article key={item.review_key}><p><strong>{item.knowledge_point_title}</strong> · {item.resource_title}</p><p>{item.decision === 'approved' ? '已接受' : item.decision === 'rejected' ? '已拒绝' : item.decision === 'needs_split' ? '已标记需拆分' : `混合旧决定（${item.decision_values.map(decisionLabel).join('、')}）`} · 影响 {item.affected_segment_count} 个证据片段</p>{item.decision === 'mixed_legacy' && <p>撤销会移除该资源全部旧片段决定，并恢复为待审核。</p>}<button disabled={busy} onClick={() => void undo(item)}>{item.decision === 'mixed_legacy' ? '撤销全部旧决定' : '撤销此决定'}</button></article>)}</section>}
    {showMaterialCapture && <TextbookLanguageCandidateReview api={api} childId={childId} />}
  </section>
}
