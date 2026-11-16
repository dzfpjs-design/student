import { useEffect, useRef, useState } from 'react'
import { ApiError, requestParentLocal } from './api'
import { hasPrivacyIntent } from './privacyOperationRecovery'

type Source = { source_id: string; title: string; version_label: string; official_url: string; pdf_sha256: string; page_count: number; status: string }
type Node = { knowledge_node_id: string; title: string; description: string; source_ref: string; knowledge_version: string }
type Context = { child_id: string; grade: string; stage_scope: string; sources: Source[]; nodes: Node[] }
type Summary = { candidate_id: string; title: string; state: string; version: number; page_number: number }
type Page = { pdf_page: number; image_sha256: string; image_data_url?: string }
type Packet = Summary & { child_id: string; eligible: boolean; blockers: string[]; source: Source;
  scope: { node: Node | null }; pages: Page[]; payload: Record<string, string | number | null>;
  events: { action_id: string; kind: string; version: number; note: string | null; occurred_at: string }[] }
type Fields = { source_id: string; knowledge_node_id: string; page_number: string; stage_page_number: string;
  page_text: string; stage_page_text: string; quote: string; title: string; observable_outcome: string }
type Command = { action_id: string; kind: 'draft' | 'approve' | 'reject' | 'withdraw'; cid?: string; body: Record<string, unknown> }
type Recovery = { fields: Fields; focusedId: string; note: string; pending: Command | null }
type Api = (path: string, init?: RequestInit) => Promise<unknown>
const defaultApi: Api = requestParentLocal
const empty = (): Recovery => ({ fields: { source_id: '', knowledge_node_id: '', page_number: '', stage_page_number: '',
  page_text: '', stage_page_text: '', quote: '', title: '', observable_outcome: '' }, focusedId: '', note: '', pending: null })
const keyFor = (child: string) => `hsc-manual-standard:${encodeURIComponent(child)}`
const labels: Record<string, string> = { draft: '待核对', approved: '已审核', rejected: '已退回', withdrawn: '已撤销', approve: '确认审核', reject: '退回', withdraw: '撤销审核' }
const errors: Record<string, string> = {
  MANUAL_CURRENT_SCHOOL_NODE_REQUIRED: '尚未绑定这个孩子当前年级的学校知识点，可以保存草稿，不能审核通过。',
  MANUAL_CURRENT_SCHOOL_SCOPE_REQUIRED: '这份逐条登记流程适用于三至四年级数学，不能用于其他学段。',
  MANUAL_CURRENT_SCHOOL_SCOPE_CHANGED: '当前孩子或学校知识点已变化，请建立新草稿重新核对。',
  MANUAL_PDF_RENDERER_UNAVAILABLE: '本机课标原页读取组件暂不可用，未保存或授予新的审核。',
  MANUAL_PDF_METADATA_UNAVAILABLE: '本机暂时无法核对原 PDF 页数，不能继续审核。',
  MANUAL_SOURCE_VERSION_CHANGED: '课标来源或版本已变化，请建立新草稿重新核对。',
  MANUAL_PDF_CHANGED: '原 PDF 与登记时不一致，不能继续审核。',
  MANUAL_GENERATED_PAGE_CHANGED: '已保存原页图片的校验不一致，不能继续审核。',
  MANUAL_VERSION_CONFLICT: '记录版本已变化。请刷新核对，原请求不会改成新的审核决定。',
  MANUAL_ACTION_CONFLICT: '原请求与记录不一致，请刷新核对，未重复提交。',
  PARENT_SESSION_REQUIRED: '家长授权已失效，请重新解锁家长中心后核对已保存结果。'
}
function messageFor(error: unknown): string {
  if (error instanceof ApiError && error.code && errors[error.code]) return errors[error.code]
  return '保存结果未确认。请刷新核对已保存记录，或重试原请求；不会自动重复审核。'
}
function readRecovery(child: string): Recovery {
  const raw = localStorage.getItem(keyFor(child)); if (!raw) return empty()
  const value = JSON.parse(raw)
  const fields = empty().fields
  if (!value || !value.fields || typeof value.focusedId !== 'string' || typeof value.note !== 'string'
    || Object.keys(fields).some(k => typeof value.fields[k] !== 'string')
    || (value.pending && (typeof value.pending.action_id !== 'string' || !['draft', 'approve', 'reject', 'withdraw'].includes(value.pending.kind)
      || !value.pending.body || typeof value.pending.body !== 'object'))) throw new Error('invalid manual recovery')
  return value
}

/** Parent work is local and durable. Stored original page bytes are never replaced on refresh. */
export function ManualStandardScopePanel(props: { child: { child_id: string; grade: string }; api?: Api }) {
  const [generation, setGeneration] = useState(0)
  useEffect(() => {
    const reset = (e: Event) => { if ((e as CustomEvent).detail == null || (e as CustomEvent).detail === props.child.child_id) setGeneration(v => v + 1) }
    window.addEventListener('hsc-manual-standard-cleared', reset)
    return () => window.removeEventListener('hsc-manual-standard-cleared', reset)
  }, [props.child.child_id])
  if (!['三年级', '四年级'].includes(props.child.grade)) return null
  return <ManualPanel key={`${props.child.child_id}:${generation}`} {...props} onStorageRetry={() => setGeneration(v => v + 1)} />
}

function ManualPanel({ child, api = defaultApi, onStorageRetry }: { child: { child_id: string; grade: string }; api?: Api; onStorageRetry: () => void }) {
  const [initial] = useState(() => { try { return { value: readRecovery(child.child_id), failed: false } } catch { return { value: empty(), failed: true } } })
  const [saved, setSaved] = useState(initial.value); const current = useRef(saved)
  const [context, setContext] = useState<Context | null>(null); const [items, setItems] = useState<Summary[]>([])
  const [packet, setPacket] = useState<Packet | null>(null); const [busy, setBusy] = useState(false)
  const [storageFailed, setStorageFailed] = useState(initial.failed); const [error, setError] = useState(initial.failed ? '本机草稿存储无法读回，未发送新操作。恢复存储后重试。' : '')
  const [notice, setNotice] = useState(''); const [confirmed, setConfirmed] = useState(false)
  const [paused, setPaused] = useState(() => hasPrivacyIntent())
  const [zoom, setZoom] = useState<number | null>(null)
  const active = useRef(true); const sequence = useRef(0)
  const base = `/v1/parent/children/${encodeURIComponent(child.child_id)}/manual-standard-requirements`
  const persist = (patch: Partial<Recovery>): boolean => {
    const next = { ...current.current, ...patch }
    try { localStorage.setItem(keyFor(child.child_id), JSON.stringify(next)) }
    catch { setStorageFailed(true); setError('本机无法保存草稿或原请求，尚未发送新操作。恢复存储后重试。'); return false }
    current.current = next; setSaved(next); setStorageFailed(false); return true
  }
  const get = async <T,>(path: string): Promise<T> => await api(path) as T
  const load = async (focused?: string) => {
    const mine = ++sequence.current; setBusy(true); setError('')
    try {
      const [ctx, list] = await Promise.all([get<Context>(`${base}/context`), get<Summary[]>(base)])
      if (!active.current || sequence.current !== mine) return
      if (ctx.child_id !== child.child_id || ctx.grade !== child.grade) throw new Error('child changed')
      setContext(ctx); setItems(list)
      const pending = current.current.pending
      if (pending) {
        try {
          const found = await get<Packet>(`${base}/actions/${encodeURIComponent(pending.action_id)}`)
          if (!active.current || sequence.current !== mine) return
          if (found.child_id !== child.child_id || (pending.cid && found.candidate_id !== pending.cid)
            || !found.events.some(e => e.action_id === pending.action_id && e.kind === pending.kind)) throw new Error('action mismatch')
          if (persist({ focusedId: found.candidate_id, pending: null })) { setPacket(found); setNotice('已从本机记录找回保存结果。'); setConfirmed(false) }
          return
        } catch (cause) { if (!(cause instanceof ApiError && cause.status === 404)) throw cause }
      }
      const id = focused ?? current.current.focusedId ?? ''
      const selected = list.find(i => i.candidate_id === id) ?? list[0]
      const next = selected ? await get<Packet>(`${base}/${encodeURIComponent(selected.candidate_id)}?images=true`) : null
      if (!active.current || sequence.current !== mine) return
      if (next && next.child_id !== child.child_id) throw new Error('child mismatch')
      if (next && next.candidate_id !== current.current.focusedId) persist({ focusedId: next.candidate_id, note: '' })
      setPacket(next); setConfirmed(false)
    } catch (cause) { if (active.current && mine === sequence.current) { setPacket(null); setError(messageFor(cause)) } }
    finally { if (active.current && mine === sequence.current) setBusy(false) }
  }
  useEffect(() => {
    active.current = true
    const pause = () => { setPaused(true); sequence.current++; setPacket(null); setConfirmed(false); setBusy(false) }
    window.addEventListener('hsc-privacy-pause', pause)
    if (!hasPrivacyIntent()) void load()
    return () => { active.current = false; sequence.current++; window.removeEventListener('hsc-privacy-pause', pause) }
  }, [child.child_id, api])
  const submit = async (command: Command) => {
    if (hasPrivacyIntent() || paused) { setError('数据恢复或删除仍在核对中，请完成后重新打开本页。'); return }
    if (!persist({ pending: command })) return
    const mutationSequence = ++sequence.current
    setBusy(true); setError(''); setNotice('')
    try {
      const path = command.kind === 'draft' ? base : `${base}/${encodeURIComponent(command.cid!)}/${command.kind === 'withdraw' ? 'withdraw' : 'review'}`
      const result = await api(path, { method: 'POST', body: JSON.stringify(command.body) }) as Packet
      if (!active.current || sequence.current !== mutationSequence || hasPrivacyIntent()) return
      if (result.child_id !== child.child_id || !result.events.some(e => e.action_id === command.action_id && e.kind === command.kind)) throw new Error('receipt mismatch')
      if (!persist({ focusedId: result.candidate_id, pending: null, note: '' })) return
      setPacket(result); setConfirmed(false); setNotice(command.kind === 'draft' ? '草稿已保存。请核对原页后再审核。' : command.kind === 'withdraw' ? '审核已撤销，关联题目不再获得这条课标授权。历史学习证据保留。' : '决定已保存。')
      await load(result.candidate_id)
    } catch (cause) { if (active.current && sequence.current === mutationSequence) setError(messageFor(cause)) }
    finally { if (active.current && sequence.current === mutationSequence) setBusy(false) }
  }
  const fields = saved.fields; const source = context?.sources.find(s => s.source_id === fields.source_id)
  const locked = busy || !!saved.pending || storageFailed || paused
  const field = (name: keyof Fields, value: string) => persist({ fields: { ...current.current.fields, [name]: value } })
  const canSave = !!source && Number.isSafeInteger(Number(fields.page_number)) && Number(fields.page_number) > 0
    && Number(fields.page_number) <= source.page_count && Number.isSafeInteger(Number(fields.stage_page_number)) && Number(fields.stage_page_number) > 0 && Number(fields.stage_page_number) <= source.page_count
    && fields.page_number !== fields.stage_page_number && fields.page_text.length >= 8 && fields.stage_page_text.includes('第二学段（3～4年级）')
    && fields.quote.length >= 8 && fields.page_text.includes(fields.quote) && fields.title.trim().length >= 2 && fields.observable_outcome.trim().length >= 8
  const saveDraft = () => {
    if (!source || !canSave) return
    const id = `manual-${crypto.randomUUID()}`
    void submit({ kind: 'draft', action_id: id, body: { ...fields, page_number: Number(fields.page_number), stage_page_number: Number(fields.stage_page_number),
      knowledge_node_id: fields.knowledge_node_id || null, source_pdf_sha256: source.pdf_sha256, source_version: source.version_label,
      stage_scope: context!.stage_scope, action_id: id, expected_version: 0 } })
  }
  const decide = (kind: Command['kind']) => {
    if (!packet || !saved.note.trim()) return
    const id = `manual-${crypto.randomUUID()}`
    void submit({ kind, cid: packet.candidate_id, action_id: id, body: { action_id: id, expected_version: packet.version,
      note: saved.note.trim(), decision: kind === 'reject' ? 'reject' : 'approve' } })
  }
  const imagesReady = !!packet && packet.pages.length === 2 && packet.pages.every(p => !!p.image_data_url?.startsWith('data:image/png;base64,'))
  return <section aria-label="人工逐条课标登记" className="manual-standard-scope">
    <style>{`.manual-standard-scope{margin:18px 0;padding:18px;border:1px solid #c9d9d1;border-radius:12px;background:#fff}.manual-standard-scope label{display:flex;flex-direction:column;gap:5px;margin:12px 0}.manual-standard-scope textarea{min-height:88px;width:100%;resize:vertical}.manual-standard-scope input,.manual-standard-scope select{max-width:100%;padding:8px}.manual-standard-scope button{margin:6px 8px 6px 0;padding:8px 12px;border:1px solid #a1b8aa;border-radius:6px;background:#f3f8f5;color:#254a34;cursor:pointer}.manual-standard-scope button:disabled{opacity:.6;cursor:default}.manual-proof-layout{display:grid;grid-template-columns:minmax(0,1fr) 310px;gap:20px}.manual-proof-layout aside{position:sticky;top:16px;align-self:start}.manual-page-zoom{max-width:94vw;width:1100px;height:90vh;overflow:auto;border:1px solid #a1b8aa;border-radius:10px}.manual-page-zoom::backdrop{background:#152a21b0}.manual-page-zoom img{width:100%;height:auto}.manual-page-zoom header{position:sticky;top:0;background:#fff;padding:10px}.manual-standard-scope figure{margin:0}.manual-standard-scope input[type=checkbox]{margin-right:6px}.manual-standard-pages{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}.manual-standard-pages img{display:block;max-width:100%;height:auto}.manual-standard-scope blockquote{white-space:pre-wrap;margin:8px 0;padding:10px;border-left:3px solid #59806b;background:#f4f8f5}.manual-standard-scope .manual-warning{padding:10px;background:#fff5e4}@media(max-width:900px){.manual-proof-layout{grid-template-columns:1fr}.manual-proof-layout aside{position:static}}@media(max-width:540px){.manual-standard-pages{grid-template-columns:1fr}}`}</style>
    <h3>逐条核对数学课标</h3><p>为这个孩子登记一条官方要求。先保存草稿，再对照服务生成的原页审核；只授权这条要求与所选知识点，整份 PDF 仍保留真实页数和未审核状态。</p>
    <button disabled={busy || paused} onClick={() => void load()}>刷新已保存记录</button>
    {paused && <p role="alert">数据恢复或删除仍在核对中，请完成后重新打开本页。</p>}
    {storageFailed && <button disabled={busy || paused} onClick={onStorageRetry}>重新读取本机草稿</button>}
    {busy && <p role="status">正在核对或保存本机资料，请稍候。</p>}
    {notice && <p role="status">{notice}</p>}{error && <p role="alert">{error}</p>}
    {saved.pending && <div className="manual-warning"><p>有一笔保存结果待核对的原请求。刷新会先查本机记录，重试沿用原请求。</p><button disabled={busy || storageFailed || paused} onClick={() => void submit(saved.pending!)}>重试原请求</button></div>}
    {context && <details open={!packet}><summary>登记新草稿</summary>
      {!context.sources.length && <p>请先导入官方数学 PDF 来源，不能用自行填写的图片路径代替原 PDF。</p>}
      <fieldset disabled={locked}><label>官方课标来源<select value={fields.source_id} onChange={e => field('source_id', e.target.value)}><option value="">请选择已登记来源</option>{context.sources.map(s => <option value={s.source_id} key={s.source_id}>{s.title} · {s.version_label} · 完整 {s.page_count} 页</option>)}</select></label>
        <label>当前学校知识点<select value={fields.knowledge_node_id} onChange={e => field('knowledge_node_id', e.target.value)}><option value="">暂未绑定，只保存草稿</option>{context.nodes.map(n => <option value={n.knowledge_node_id} key={n.knowledge_node_id}>{n.title}</option>)}</select></label>
        {fields.knowledge_node_id && <p>{context.nodes.find(n => n.knowledge_node_id === fields.knowledge_node_id)?.description}</p>}
        <label>要求所在 PDF 页码<input type="number" min="1" value={fields.page_number} onChange={e => field('page_number', e.target.value)} /></label>
        <label>学段说明所在 PDF 页码<input type="number" min="1" value={fields.stage_page_number} onChange={e => field('stage_page_number', e.target.value)} /></label>
        <p>填写阅读器中的 PDF 页码。印刷页码可能不同；原页中必须能核对“第二学段（3～4年级）”。</p>
        <label>要求所在页的人工摘录<textarea value={fields.page_text} onChange={e => field('page_text', e.target.value)} maxLength={6000} /></label>
        <label>学段说明页的人工摘录<textarea value={fields.stage_page_text} onChange={e => field('stage_page_text', e.target.value)} maxLength={6000} /></label>
        <label>本条要求的准确原句<textarea value={fields.quote} onChange={e => field('quote', e.target.value)} maxLength={1200} /></label>
        <label>要求标题<input value={fields.title} onChange={e => field('title', e.target.value)} maxLength={200} /></label>
        <label>可观察的学习结果<textarea value={fields.observable_outcome} onChange={e => field('observable_outcome', e.target.value)} maxLength={1200} /></label>
        <button disabled={!canSave} onClick={saveDraft}>保存草稿并读取原页</button>
      </fieldset></details>}
    <h4>已保存记录</h4>{!busy && !items.length && <p>尚无逐条登记记录。</p>}
    {items.map(i => <button key={i.candidate_id} disabled={locked} aria-pressed={packet?.candidate_id === i.candidate_id}
      onClick={() => { if (persist({ focusedId: i.candidate_id, note: '' })) void load(i.candidate_id) }}>{i.title} · {labels[i.state] ?? i.state} · 版本 {i.version}</button>)}
    {packet && <article aria-label="课标原页与审核">
      <h4>{String(packet.payload.title)}</h4><p>{packet.source.title} · {packet.source.version_label} · 完整 {packet.source.page_count} 页 · {labels[packet.state]} · 版本 {packet.version}</p>
      <p>学校知识点：{packet.scope.node?.title ?? '尚未绑定，不能审核通过'}</p>
      <div className="manual-proof-layout"><div className="manual-standard-pages">{packet.pages.map(p => <figure key={p.pdf_page}><figcaption>官方 PDF 第 {p.pdf_page} 页</figcaption>{p.image_data_url ? <img src={p.image_data_url} alt={`已保存的官方数学课标 PDF 第 ${p.pdf_page} 页`} /> : <p>原页暂时无法核对，不能审核通过。</p>}{p.image_data_url && <button onClick={() => setZoom(p.pdf_page)}>放大原页 {p.pdf_page}</button>}</figure>)}</div><aside>
      <h5>准确原句</h5><blockquote>{String(packet.payload.quote)}</blockquote><p>学习结果：{String(packet.payload.observable_outcome)}</p>
      {packet.blockers.length > 0 && <p role="alert">{packet.blockers.map(b => errors[b] ?? '当前依据暂时无法核对，不能授权。').join(' ')}</p>}
      {packet.state === 'draft' && <label><input type="checkbox" checked={confirmed} disabled={locked || !imagesReady || !packet.scope.node || !!packet.blockers.length} onChange={e => setConfirmed(e.target.checked)} />我已核对两张原页、学段、原句和这个孩子当前学校知识点。</label>}
      {['draft', 'approved'].includes(packet.state) && <><label>审核或撤销说明<textarea disabled={locked} value={saved.note} onChange={e => persist({ note: e.target.value })} maxLength={500} /></label>
        {packet.state === 'draft' ? <><button disabled={locked || !confirmed || !saved.note.trim()} onClick={() => decide('approve')}>确认审核这条要求</button><button disabled={locked || !saved.note.trim()} onClick={() => decide('reject')}>退回草稿</button></> : <button disabled={locked || !saved.note.trim()} onClick={() => decide('withdraw')}>撤销这条审核</button>}</>}
      </aside></div>
      {zoom !== null && packet.pages.find(p => p.pdf_page === zoom)?.image_data_url && <PageZoom page={packet.pages.find(p => p.pdf_page === zoom)!} close={() => setZoom(null)} />}
      <details><summary>保存与决定记录</summary><ol>{packet.events.map(e => <li key={`${e.version}-${e.kind}`}>版本 {e.version} · {labels[e.kind] ?? '保存草稿'} · {new Date(e.occurred_at).toLocaleString()}{e.note && <p>{e.note}</p>}</li>)}</ol></details>
      <p>审核不会说明孩子已经掌握，也不会创建复测记录或授予云端上传、题目改编许可。</p>
    </article>}
  </section>
}

function PageZoom({ page, close }: { page: Page; close: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null)
  useEffect(() => { dialog.current?.showModal() }, [])
  return <dialog ref={dialog} className="manual-page-zoom" aria-label={`放大官方 PDF 第 ${page.pdf_page} 页`} onCancel={close}>
    <header><strong>已保存原页 · 官方 PDF 第 {page.pdf_page} 页</strong><button autoFocus onClick={close}>返回审核</button></header>
    <img src={page.image_data_url} alt={`放大的已保存官方 PDF 第 ${page.pdf_page} 页`} />
  </dialog>
}
