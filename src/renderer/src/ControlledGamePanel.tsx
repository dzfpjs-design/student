import { useEffect, useState } from 'react'
import { Check, Gamepad2, Plus, ShieldCheck, Trash2 } from 'lucide-react'
import { ApiError, localApi } from './api'
import type { ControlledGameApproval, ControlledGameRule } from './types'

export function ControlledGamePanel({ childId }: { childId: string }): React.JSX.Element {
  const [rule, setRule] = useState<ControlledGameRule | null>(null)
  const [items, setItems] = useState<ControlledGameApproval[]>([])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [message, setMessage] = useState<string | null>(null)

  const load = async (): Promise<void> => {
    setBusy(true); setError(null)
    try {
      const [nextRule, approvals] = await Promise.all([
        localApi.controlledGameRule(childId), localApi.controlledGameApprovals(childId),
      ])
      setRule(nextRule); setItems(approvals)
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : '受控游戏许可暂时无法读取。')
    } finally { setBusy(false) }
  }

  useEffect(() => { void load() }, [childId])

  const saveRule = async (next: ControlledGameRule): Promise<void> => {
    setBusy(true); setError(null); setMessage(null)
    try {
      const saved = await localApi.updateControlledGameRule(childId, {
        enabled: next.enabled,
        max_daily_minutes: next.max_daily_minutes,
        max_item_minutes: next.max_item_minutes,
      })
      setRule(saved)
      setMessage(saved.enabled ? '受控游戏已启用，孩子只能用语音启动许可程序。' : '受控游戏已关闭。')
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : '游戏时间规则没有保存。')
    } finally { setBusy(false) }
  }

  const add = async (): Promise<void> => {
    if (!rule || !window.desktop?.chooseControlledGameExecutable) {
      setError('桌面程序选择功能不可用。')
      return
    }
    setBusy(true); setError(null); setMessage(null)
    try {
      const selected = await window.desktop.chooseControlledGameExecutable()
      if (!selected) return
      const filename = selected.path.split(/[\\/]/).pop() ?? '本地游戏'
      const title = filename.replace(/\.exe$/i, '')
      const saved = await localApi.approveControlledGame(childId, {
        title,
        executable_path: selected.path,
        max_minutes: Math.min(rule.max_item_minutes, 15),
        note: '家长在本机选择并许可',
      })
      setItems((current) => [saved, ...current.filter((item) => item.approval_id !== saved.approval_id)])
      setMessage(`“${saved.title}”已加入许可目录。`)
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : cause instanceof Error ? cause.message : '游戏没有加入许可目录。')
    } finally { setBusy(false) }
  }

  const revoke = async (item: ControlledGameApproval): Promise<void> => {
    setBusy(true); setError(null); setMessage(null)
    try {
      await localApi.revokeControlledGame(childId, item.approval_id)
      setItems((current) => current.map((candidate) => candidate.approval_id === item.approval_id
        ? { ...candidate, status: 'revoked' }
        : candidate))
      setMessage(`“${item.title}”已移出许可目录。`)
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : '这项游戏许可没有撤销。')
    } finally { setBusy(false) }
  }

  return (
    <section className="controlled-content-panel">
      <div className="period-heading">
        <div><span>本地受控游戏</span><h2>程序许可与强制时间边界</h2><p>只有家长选择的 Windows 游戏才能被语音启动；到时后应用关闭整个游戏进程并恢复学习任务。</p></div>
        <Gamepad2 size={24} />
      </div>
      {rule && <div className="controlled-content-rule">
        <label><input type="checkbox" checked={rule.enabled} disabled={busy} onChange={(event) => void saveRule({ ...rule, enabled: event.target.checked })} />允许语音启动许可游戏</label>
        <label>每日上限<input aria-label="受控游戏每日分钟" type="number" min="0" max="120" value={rule.max_daily_minutes} disabled={busy} onChange={(event) => setRule({ ...rule, max_daily_minutes: Number(event.target.value) })} onBlur={() => void saveRule(rule)} /></label>
        <label>单次上限<input aria-label="受控游戏单次分钟" type="number" min="1" max="60" value={rule.max_item_minutes} disabled={busy} onChange={(event) => setRule({ ...rule, max_item_minutes: Number(event.target.value) })} onBlur={() => void saveRule(rule)} /></label>
        <button className="secondary-button" disabled={busy} onClick={() => void add()}><Plus size={16} />许可本机游戏</button>
      </div>}
      {error && <div className="tutor-error" role="alert">{error}</div>}
      {message && <p className="operation-message"><Check size={15} />{message}</p>}
      {!busy && items.length === 0 ? <p className="muted-copy">还没有许可任何游戏。孩子说“打开游戏”时，智能体不会自行安装或启动其他程序。</p> : (
        <div className="controlled-content-list">{items.map((item) => <article key={item.approval_id}>
          <Gamepad2 size={18} />
          <div><strong>{item.title}</strong><span>{item.status === 'approved' ? `已许可，单次最多${item.max_minutes}分钟` : '许可已撤销'}</span><small title={item.executable_path}>{item.executable_path}</small></div>
          {item.status === 'approved'
            ? <button className="icon-button" title="撤销游戏许可" disabled={busy} onClick={() => void revoke(item)}><Trash2 size={17} /></button>
            : <ShieldCheck size={17} aria-label="已撤销" />}
        </article>)}</div>
      )}
    </section>
  )
}
