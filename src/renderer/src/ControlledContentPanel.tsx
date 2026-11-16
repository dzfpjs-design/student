import { useEffect, useState } from 'react'
import { Check, Play, RefreshCw, Search, ShieldCheck, Trash2 } from 'lucide-react'
import { ApiError, localApi } from './api'
import type { ControlledContentCandidate, ControlledContentRule } from './types'

export function ControlledContentPanel({ childId }: { childId: string }): React.JSX.Element {
  const [rule, setRule] = useState<ControlledContentRule | null>(null)
  const [items, setItems] = useState<ControlledContentCandidate[]>([])
  const [query, setQuery] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [message, setMessage] = useState<string | null>(null)

  const load = async (search = query): Promise<void> => {
    setBusy(true); setError(null)
    try {
      const [nextRule, candidates] = await Promise.all([
        localApi.controlledContentRule(childId),
        localApi.controlledContentCandidates(childId, search),
      ])
      setRule(nextRule); setItems(candidates)
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : '受控内容目录暂时无法读取。')
    } finally { setBusy(false) }
  }

  useEffect(() => { void load('') }, [childId])

  const saveRule = async (next: ControlledContentRule): Promise<void> => {
    setBusy(true); setError(null); setMessage(null)
    try {
      const saved = await localApi.updateControlledContentRule(childId, {
        enabled: next.enabled,
        max_daily_minutes: next.max_daily_minutes,
        max_item_minutes: next.max_item_minutes,
      })
      setRule(saved)
      setMessage(saved.enabled ? '许可目录已启用，孩子只能用语音播放已批准内容。' : '受控内容已关闭。')
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : '受控内容规则没有保存。')
    } finally { setBusy(false) }
  }

  const approve = async (item: ControlledContentCandidate): Promise<void> => {
    if (!rule) return
    setBusy(true); setError(null); setMessage(null)
    try {
      const saved = await localApi.approveControlledContent(
        childId, item.row_id, Math.min(rule.max_item_minutes, 15), '家长在本机许可目录中批准'
      )
      setItems((current) => current.map((candidate) => candidate.row_id === saved.row_id ? saved : candidate))
      setMessage(`“${saved.title}”已加入许可目录。`)
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : '这项内容没有加入许可目录。')
    } finally { setBusy(false) }
  }

  const revoke = async (item: ControlledContentCandidate): Promise<void> => {
    setBusy(true); setError(null); setMessage(null)
    try {
      await localApi.revokeControlledContent(childId, item.row_id)
      setItems((current) => current.map((candidate) => candidate.row_id === item.row_id ? { ...candidate, approval_status: 'revoked' } : candidate))
      setMessage(`“${item.title}”已移出许可目录。`)
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : '这项许可没有撤销。')
    } finally { setBusy(false) }
  }

  return (
    <section className="controlled-content-panel">
      <div className="period-heading">
        <div><span>本地受控内容</span><h2>视频许可与时间边界</h2><p>只列出当前年级已下载到本机的课程视频。孩子无需点击，智能体按语音请求播放。</p></div>
        <ShieldCheck size={24} />
      </div>
      {rule && <div className="controlled-content-rule">
        <label><input type="checkbox" checked={rule.enabled} disabled={busy} onChange={(event) => void saveRule({ ...rule, enabled: event.target.checked })} />允许语音播放许可内容</label>
        <label>每日上限<input aria-label="受控内容每日分钟" type="number" min="0" max="120" value={rule.max_daily_minutes} disabled={busy} onChange={(event) => setRule({ ...rule, max_daily_minutes: Number(event.target.value) })} onBlur={() => void saveRule(rule)} /></label>
        <label>单项上限<input aria-label="受控内容单项分钟" type="number" min="1" max="60" value={rule.max_item_minutes} disabled={busy} onChange={(event) => setRule({ ...rule, max_item_minutes: Number(event.target.value) })} onBlur={() => void saveRule(rule)} /></label>
      </div>}
      <div className="controlled-content-search">
        <label><Search size={16} /><input aria-label="搜索本地课程视频" value={query} placeholder="知识点、单元或课程名" onChange={(event) => setQuery(event.target.value)} onKeyDown={(event) => { if (event.key === 'Enter') void load(query) }} /></label>
        <button className="secondary-button" disabled={busy} onClick={() => void load(query)}><RefreshCw size={16} />搜索</button>
      </div>
      {error && <div className="tutor-error" role="alert">{error}</div>}
      {message && <p className="operation-message"><Check size={15} />{message}</p>}
      {!busy && items.length === 0 ? <p className="muted-copy">没有找到当前年级可用的本地视频。先确认课程知识库已导入并且视频文件仍在原目录。</p> : (
        <div className="controlled-content-list">{items.map((item) => <article key={item.row_id}>
          <Play size={18} />
          <div><strong>{item.title}</strong><span>{item.subject_title} · {item.unit_path}</span><small>{item.video_duration ? `${Math.ceil(item.video_duration / 60)}分钟` : '时长未知'} · {item.approval_status === 'approved' ? `已许可，单次最多${item.max_minutes}分钟` : '未许可'}</small></div>
          {item.approval_status === 'approved'
            ? <button className="icon-button" title="撤销许可" disabled={busy} onClick={() => void revoke(item)}><Trash2 size={17} /></button>
            : <button className="secondary-button" disabled={busy} onClick={() => void approve(item)}><ShieldCheck size={16} />许可</button>}
        </article>)}</div>
      )}
    </section>
  )
}
