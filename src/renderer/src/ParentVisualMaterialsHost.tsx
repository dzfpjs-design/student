import { useEffect, useMemo, useRef, useState } from 'react'
import { ArrowLeft, ArrowRight, RefreshCw, Save } from 'lucide-react'
import { ParentVisualMaterialReview } from './ParentVisualMaterialReview'
import { createVisualMaterialApi, visualReviewDetail, type VisualListing, type VisualListItem } from './visualMaterialApi'

type Api = ReturnType<typeof createVisualMaterialApi>
type Detail = Awaited<ReturnType<Api['get']>>
type StoreIntent = { child_id: string; ref: VisualListItem; client_action_id: string }
const stateKey = (child: string) => `hsc-visual-storage:${child}`
const refKey = (ref: VisualListItem) => JSON.stringify([ref.artifact_id, ref.revision_id, ref.region_id, ref.region_version])
const mayRetain = (detail: Detail) => detail.source_current && ['not_stored', 'revoked', 'quarantined'].includes(detail.storage_status)
function restore(child: string): { pending: StoreIntent | null; corrupt: boolean } {
  try {
    const text = localStorage.getItem(stateKey(child))
    if (!text) return { pending: null, corrupt: false }
    const value = JSON.parse(text) as StoreIntent
    if (value.child_id !== child || !value.ref || !value.client_action_id || typeof value.client_action_id !== 'string'
      || !/^[a-f0-9]{64}$/.test(value.ref.region_version) || typeof value.ref.artifact_id !== 'string'
      || typeof value.ref.revision_id !== 'string' || typeof value.ref.region_id !== 'string') throw new Error('invalid stored action')
    return { pending: value, corrupt: false }
  } catch { return { pending: null, corrupt: true } }
}
export function ParentVisualMaterialsHost({ childId, api: provided }: { childId: string; api?: Api }) {
  const api = useMemo(() => provided ?? createVisualMaterialApi(), [provided])
  const [generation, setGeneration] = useState(0)
  useEffect(() => {
    const cleared = (event: Event) => { if ((event as CustomEvent<string>).detail === childId) { api.clearImages(); setGeneration(n => n + 1) } }
    window.addEventListener('hsc-visual-review-cleared', cleared)
    return () => window.removeEventListener('hsc-visual-review-cleared', cleared)
  }, [childId, api])
  return <Host key={`${childId}:${generation}`} childId={childId} api={api} />
}
function Host({ childId, api }: { childId: string; api: Api }) {
  const [initial] = useState(() => restore(childId))
  const [pending, setPending] = useState(initial.pending)
  const [storageFailed, setStorageFailed] = useState(initial.corrupt)
  const [page, setPage] = useState(0), [reload, setReload] = useState(0)
  const [listing, setListing] = useState<VisualListing | null>(null)
  const [selected, setSelected] = useState<VisualListItem | null>(initial.pending?.ref ?? null)
  const [detail, setDetail] = useState<Detail | null>(null)
  const [loading, setLoading] = useState(true), [busy, setBusy] = useState(false), [error, setError] = useState('')
  const [consent, setConsent] = useState('')
  const active = useRef(true), working = useRef(false), sequence = useRef(0)
  const selectedIdentity = useRef(''); selectedIdentity.current = selected ? refKey(selected) : ''
  const operation = useRef<AbortController | null>(null)
  useEffect(() => { active.current = true; return () => { active.current = false; operation.current?.abort(); api.clearImages() } }, [api])
  useEffect(() => {
    const abort = new AbortController()
    setLoading(true); setError(''); setListing(null)
    void api.list(childId, page, abort.signal).then(result => {
      if (abort.signal.aborted) return
      setListing(result)
      setSelected(current => current ?? result.items[0] ?? null)
    }).catch(() => { if (!abort.signal.aborted) setError('图形原件列表暂时无法读取。原审核记录保留在本机。') })
      .finally(() => { if (!abort.signal.aborted) setLoading(false) })
    return () => abort.abort()
  }, [childId, page, reload, api])
  useEffect(() => {
    const abort = new AbortController(), generation = ++sequence.current
    setDetail(null); setConsent('')
    if (selected) void api.get(childId, selected, abort.signal).then(value => {
      if (!abort.signal.aborted && sequence.current === generation) setDetail(value)
    }).catch(() => { if (!abort.signal.aborted && sequence.current === generation) setError('这幅原图的来源或版本无法核对，未采用其他图形。') })
    return () => abort.abort()
  }, [childId, selected, reload, api])
  async function refreshSelected() {
    if (!selected) throw new Error('no selection')
    const at = refKey(selected), generation = ++sequence.current, abort = new AbortController()
    const next = await api.get(childId, selected, abort.signal)
    if (!active.current || selectedIdentity.current !== at || sequence.current !== generation) throw new Error('selection changed')
    setDetail(next)
  }
  async function store(intent?: StoreIntent) {
    if (working.current || storageFailed) return
    let command = intent
    if (!command) {
      if (pending || !selected || !detail || consent !== refKey(selected) || !mayRetain(detail)) return
      command = { child_id: childId, ref: { ...selected, facts_version: detail.facts_version }, client_action_id: crypto.randomUUID() }
      try { localStorage.setItem(stateKey(childId), JSON.stringify(command)) }
      catch { setStorageFailed(true); setError('无法保存本机保留操作，尚未发送请求。'); return }
      setPending(command)
    }
    working.current = true; setBusy(true); setError('')
    const abort = new AbortController(); operation.current = abort
    const timeout = setTimeout(() => abort.abort(), 30_000)
    try {
      const next = await api.store(childId, command.ref, command.client_action_id, abort.signal)
      if (!active.current || abort.signal.aborted) return
      if (selectedIdentity.current === refKey(command.ref)) setDetail(next)
      try { localStorage.removeItem(stateKey(childId)) } catch { setStorageFailed(true); throw new Error('local storage') }
      setPending(null); setConsent('')
    } catch { if (active.current) setError('原图保留操作尚未核对；原操作已保存，可以核对后继续。') }
    finally { clearTimeout(timeout); working.current = false; if (active.current) setBusy(false) }
  }
  function changePage(next: number) { if (!busy) { setSelected(null); setPage(next) } }
  return <section id="parent-work-visual-materials" className="parent-panel visual-materials-host" aria-label="学习图形原件" style={{ minWidth: 0, overflowWrap: 'anywhere' }}>
    <header style={{ display: 'flex', justifyContent: 'space-between', gap: 12 }}><h2 style={{ fontSize: 18 }}>学习图形原件</h2>
      <button className="visual-icon" type="button" aria-label="刷新图形原件" title="刷新图形原件" disabled={busy || loading} onClick={() => setReload(n => n + 1)}><RefreshCw size={18} /></button></header>
    {error && <p role="alert">{error}</p>}
    {storageFailed && <p role="alert">本机操作恢复记录异常，新的保留请求已暂停，旧记录未删除。</p>}
    {loading && <p role="status">正在读取图形原件…</p>}
    {listing && !listing.total && <p>还没有待核对的学习图形。</p>}
    {!!listing?.items.length && <nav aria-label="选择原图" style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>{listing.items.map(item => <button className="visual-source-choice" key={refKey(item)} type="button"
      disabled={busy} aria-pressed={selectedIdentity.current === refKey(item)} onClick={() => { setSelected(item); setError('') }}>
      {item.title}<span style={{ display: 'block', fontSize: 12 }}>{new Date(item.captured_at).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai' })}</span></button>)}</nav>}
    {listing && <div style={{ display: 'flex', gap: 12, alignItems: 'center', marginTop: 12 }}>
      <button className="visual-icon" type="button" title="上一页原图" aria-label="上一页原图" disabled={busy || page === 0} onClick={() => changePage(Math.max(0, page - 20))}><ArrowLeft size={18} /></button>
      <span>{listing.total ? `${page + 1}–${Math.min(page + listing.items.length, listing.total)} / ${listing.total}` : '0 / 0'}</span>
      <button className="visual-icon" type="button" title="下一页原图" aria-label="下一页原图" disabled={busy || page + listing.items.length >= listing.total} onClick={() => changePage(page + 20)}><ArrowRight size={18} /></button>
    </div>}
    {pending && <div><p>有一次原图保留操作等待核对，刷新或重启不会重新创建它。</p><button type="button" disabled={busy || storageFailed}
      onClick={() => void store(pending)}><RefreshCw size={16} />核对原保留操作</button></div>}
    {detail && selected && <>
      {mayRetain(detail) && <div style={{ margin: '12px 0' }}>
        {detail.storage_status !== 'not_stored' && <p>原授权已撤回或被备份恢复隔离。重新授权后仍须重新核对图形内容。</p>}
        <label><input type="checkbox" checked={consent === refKey(selected)} disabled={busy || storageFailed || !!pending}
        onChange={e => setConsent(e.target.checked ? refKey(selected) : '')} />只在本机保留这幅裁图，供课后核对；不保留完整拍摄画面，也不上传。</label>
        <p><button type="button" disabled={busy || storageFailed || !!pending || consent !== refKey(selected)} onClick={() => void store()}><Save size={16} />{detail.storage_status === 'not_stored' ? '保留这幅原图' : '重新授权本机使用'}</button></p></div>}
      {['missing', 'corrupt'].includes(detail.storage_status) && <p>这幅原图文件缺失或损坏，需要重新拍摄。旧审核记录保留，不能据此判断答案。</p>}
      <ParentVisualMaterialReview detail={visualReviewDetail(detail)}
        readImage={(d, signal) => api.image(childId, d, signal)} recordDisplayed={(d, signal) => api.displayed(d, signal)}
        submit={(command, signal) => api.submit(childId, command, signal)} refresh={refreshSelected} />
    </>}
  </section>
}
