import { useEffect, useMemo, useRef, useState } from 'react'
import { RefreshCw, Save, Undo2 } from 'lucide-react'
import { ApiError } from './api'
import { createVisualRetentionApi, validRetentionCommand, type RetentionCommand, type RetentionView } from './visualRetentionApi'
import './visualMaterialReview.css'

type Api = ReturnType<typeof createVisualRetentionApi>
type Pending = { version: 1; child_id: string; command: RetentionCommand }
const key = (child: string) => `hsc-visual-retention:${child}`
const labels = { disabled: '尚未开启', enabled: '已开启', expired: '授权已到期', revoked: '已撤回', quarantined: '恢复后待重新授权' }
function recover(child: string): { pending: Pending | null; corrupt: boolean } {
  try {
    const raw = localStorage.getItem(key(child))
    if (!raw) return { pending: null, corrupt: false }
    const p = JSON.parse(raw) as Pending
    if (p.version !== 1 || p.child_id !== child || !validRetentionCommand(p.command)) throw Error('invalid journal')
    return { pending: p, corrupt: false }
  } catch { return { pending: null, corrupt: true } }
}
export function ParentVisualRetention({ childId, api: provided }: { childId: string; api?: Api }) {
  const api = useMemo(() => provided ?? createVisualRetentionApi(), [provided])
  const [epoch, setEpoch] = useState(0)
  useEffect(() => {
    const cleared = (e: Event) => { if ((e as CustomEvent<string>).detail === childId) setEpoch(n => n + 1) }
    window.addEventListener('hsc-visual-review-cleared', cleared)
    return () => window.removeEventListener('hsc-visual-review-cleared', cleared)
  }, [childId])
  return <Settings key={`${childId}:${epoch}`} childId={childId} api={api} />
}
function Settings({ childId, api }: { childId: string; api: Api }) {
  const [initial] = useState(() => recover(childId))
  const [pending, setPending] = useState(initial.pending), [corrupt, setCorrupt] = useState(initial.corrupt)
  const [view, setView] = useState<RetentionView | null>(null), [error, setError] = useState(''), [notice, setNotice] = useState('')
  const [busy, setBusy] = useState(false), [loading, setLoading] = useState(true), [reload, setReload] = useState(0)
  const [days, setDays] = useState(7), [authorizationDays, setAuthorizationDays] = useState(30), [megabytes, setMegabytes] = useState(128)
  const [consent, setConsent] = useState(false)
  const active = useRef(true), running = useRef(false), request = useRef<AbortController | null>(null)
  useEffect(() => { active.current = true; return () => { active.current = false; request.current?.abort() } }, [])
  function accept(next: RetentionView) {
    setView(next); setConsent(false)
    setDays(next.policy.retention_days ?? 7); setMegabytes((next.policy.max_bytes ?? 134217728) / 1048576)
  }
  useEffect(() => {
    const abort = new AbortController(), timer = setTimeout(() => abort.abort(), 15_000)
    let relevant = true
    setLoading(true); setError('')
    void api.get(childId, abort.signal).then(next => { if (!abort.signal.aborted) accept(next) })
      .catch(() => { if (relevant) setError('保留设置暂时无法读取，没有变更原授权。') })
      .finally(() => { clearTimeout(timer); if (relevant) setLoading(false) })
    return () => { relevant = false; clearTimeout(timer); abort.abort() }
  }, [childId, api, reload])
  async function send(p: Pending) {
    if (running.current || corrupt) return
    running.current = true; setBusy(true); setError(''); setNotice('')
    const abort = new AbortController(); request.current = abort
    const timer = setTimeout(() => abort.abort(), 30_000)
    try {
      const result = await api.submit(childId, p.command, abort.signal)
      if (!active.current || abort.signal.aborted) return
      accept(result)
      try { localStorage.removeItem(key(childId)) } catch { setCorrupt(true); throw Error('journal cleanup') }
      setPending(null)
      setNotice(result.replayed ? '原操作已核对，以下为当前生效状态。' : '学习裁图保留设置已保存。')
    } catch (error) {
      if (!active.current) return
      // The server checks prior receipts before reporting this conflict, so only
      // this explicit refusal proves the original request was not committed.
      if (error instanceof ApiError && error.code === 'VISUAL_RETENTION_REVISION_CONFLICT' && !abort.signal.aborted) {
        try {
          const current = await api.get(childId, abort.signal)
          if (!active.current || abort.signal.aborted) return
          accept(current)
          localStorage.removeItem(key(childId)); setPending(null)
          setNotice('设置已被其他操作更新，本次旧版本请求未执行。请核对当前设置后重新选择。')
          return
        } catch { /* Preserve the original journal until refusal and current state are both known. */ }
      }
      if (active.current) setError('尚未核对保存结果，原操作已保留；恢复连接后可核对，不会重复授权。')
    }
    finally { clearTimeout(timer); running.current = false; if (active.current) setBusy(false) }
  }
  function submit(action: RetentionCommand['action']) {
    if (!view || loading || busy || pending || corrupt || action !== 'revoke' && !consent) return
    const common = { expected_revision: view.policy.revision, client_action_id: crypto.randomUUID() }
    const command: RetentionCommand = action === 'revoke' ? { ...common, action }
      : { ...common, action, session_id: view.policy.scope?.session_id ?? null, retention_days: days,
        authorization_days: authorizationDays, max_bytes: megabytes * 1048576 }
    if (!validRetentionCommand(command)) { setError('请核对保留天数、授权天数和容量。'); return }
    const p: Pending = { version: 1, child_id: childId, command }
    try { localStorage.setItem(key(childId), JSON.stringify(p)) }
    catch { setCorrupt(true); setError('本机无法保存操作恢复记录，本次设置尚未发送。'); return }
    setPending(p); void send(p)
  }
  const disabled = loading || busy || !!pending || corrupt
  return <section className="visual-retention" aria-label="学习裁图自动保留" style={{ borderBottom: '1px solid #cfd9d3', padding: '12px 0', minWidth: 0 }}>
    <header style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 12 }}>
      <h3 style={{ fontSize: 16 }}>学习裁图自动保留</h3>
      <button className="visual-icon" type="button" title="刷新保留设置" aria-label="刷新保留设置" disabled={busy || loading} onClick={() => setReload(n => n + 1)}><RefreshCw size={18} /></button>
    </header>
    {loading && <p role="status">正在读取保留设置…</p>}
    {error && <p role="alert">{error}</p>}{notice && <p role="status">{notice}</p>}
    {corrupt && <p role="alert">本机操作恢复记录无法核对，新的授权已暂停，旧记录未删除。</p>}
    {pending && <div><p>有一次保留设置操作等待核对。</p><button type="button" disabled={busy || corrupt} onClick={() => void send(pending)}><RefreshCw size={16} />核对原设置操作</button></div>}
    {view && <>
      <p>{labels[view.policy.status]} · 已占用 {(view.policy.used_bytes / 1048576).toFixed(1)} MB</p>
      {view.policy.scope?.session_id && <p>仅限原学习会话；不会扩展到其他会话。</p>}
      {view.policy.authorization_expires_at && <p>授权截止：{new Date(view.policy.authorization_expires_at).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai' })}</p>}
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 12 }}>
        <label>每幅裁图保留天数<input type="number" min={1} max={30} value={days} disabled={disabled} onChange={e => setDays(Number(e.target.value))} /></label>
        <label>此次授权天数<input type="number" min={1} max={365} value={authorizationDays} disabled={disabled} onChange={e => setAuthorizationDays(Number(e.target.value))} /></label>
        <label>本机容量上限（MB）<input type="number" min={1} max={256} step={1} value={megabytes} disabled={disabled} onChange={e => setMegabytes(Number(e.target.value))} /></label>
      </div>
      <p><label><input type="checkbox" checked={consent} disabled={disabled} onChange={e => setConsent(e.target.checked)} />允许为这名孩子自动保留安全裁图，仅用于本机学习核对；不保留完整画面、不上传、不自动确认解题结论。</label></p>
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 12 }}>
        <button type="button" disabled={disabled || !consent} onClick={() => submit(view.policy.status === 'enabled' ? 'update' : 'enable')}><Save size={16} />{view.policy.status === 'enabled' ? '更新保留设置' : '开启自动保留'}</button>
        {view.policy.status !== 'disabled' && view.policy.status !== 'revoked' && <button type="button" disabled={disabled} onClick={() => submit('revoke')}><Undo2 size={16} />撤回自动保留</button>}
      </div>
    </>}
  </section>
}
