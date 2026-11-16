import { useEffect, useMemo, useRef, useState } from 'react'
import { FilePlus, Plus, Save, Trash2, Upload, RefreshCw, Undo2 } from 'lucide-react'
import { ApiError } from './api'
import type { CurriculumChild, CurriculumParentRequest } from './curriculumSupplyTypes'
import { curriculumStateClearedEvent } from './curriculumSupplyApi'
import { createOralSourceApi, oralAuthorKey, type OralAuthorOptions, type OralImport } from './oralSourceApi'
import { OralTaskPreview, oralFamilyNames, oralPictureSupported, parseOralSource } from './OralTaskPreview'

type Draft = { child_id: string; family: string; title: string; edition: string; prompt: string; material: string
  points: string[]; reviewed: boolean; pending: OralImport | null; saved: string | null }
const empty = (child: string): Draft => ({ child_id: child, family: '', title: '', edition: '', prompt: '', material: '', points: [''], reviewed: false, pending: null, saved: null })

export function OralSourceAuthorPanel(props: { child: CurriculumChild; request: CurriculumParentRequest; disabled?: boolean
  onSaved: (source: string) => void; onRequireParentAuth?: () => void }) {
  const [opened, setOpened] = useState(false)
  return <div className="csr-band"><button disabled={props.disabled} aria-expanded={opened} onClick={() => setOpened(!opened)}><FilePlus />编写口语原题</button>
    {opened && <AuthorForm key={props.child.child_id} {...props} />}</div>
}
function AuthorForm({ child, request, onSaved, onRequireParentAuth }: Parameters<typeof OralSourceAuthorPanel>[0]) {
  const api = useMemo(() => createOralSourceApi(request), [request])
  const [initial] = useState(() => {
    try {
      const raw = localStorage.getItem(oralAuthorKey(child.child_id))
      const v = raw ? JSON.parse(raw) as Draft : empty(child.child_id)
      if (!v || v.child_id !== child.child_id || ['family', 'title', 'edition', 'prompt', 'material'].some(k => typeof v[k as keyof Draft] !== 'string')
        || !Array.isArray(v.points) || v.points.some(p => typeof p !== 'string') || typeof v.reviewed !== 'boolean'
        || !(v.saved === null || typeof v.saved === 'string') || !(v.pending === null || parseOralSource(v.pending.content_text))) throw Error()
      return { value: v, failed: false }
    } catch { return { value: empty(child.child_id), failed: true } }
  })
  const [draft, setDraft] = useState(initial.value)
  const current = useRef(draft)
  const [storageFailed, setStorageFailed] = useState(initial.failed)
  const [options, setOptions] = useState<OralAuthorOptions | null>(null)
  const [error, setError] = useState('')
  const [auth, setAuth] = useState(false)
  const [busy, setBusy] = useState(false)
  const live = useRef(true)
  const sending = useRef(false)
  useEffect(() => {
    live.current = true
    const clear = (event: Event) => { if ((event as CustomEvent).detail == null || (event as CustomEvent).detail === child.child_id) live.current = false }
    window.addEventListener(curriculumStateClearedEvent, clear)
    void api.options(child).then(v => { if (live.current) setOptions(v) }).catch(report)
    return () => { live.current = false; window.removeEventListener(curriculumStateClearedEvent, clear) }
  }, [api, child.child_id, child.grade])
  function report(e: unknown) {
    if (!live.current) return
    setAuth(e instanceof ApiError && [401, 403].includes(e.status ?? 0))
    setError(e instanceof ApiError ? e.message : '请求未能完成核对；草稿仍保留，可重试原保存请求。')
  }
  function persist(patch: Partial<Draft>) {
    if (!live.current) return false
    const next = { ...current.current, ...patch }
    try { localStorage.setItem(oralAuthorKey(child.child_id), JSON.stringify(next)) }
    catch { setStorageFailed(true); return false }
    current.current = next; setDraft(next); return true
  }
  const option = options?.options.find(o => o.task_family === draft.family)
  const source = option ? parseOralSource({ schema: 'pet_speaking_source_v1', subject: '英语', grade: child.grade,
    knowledge_node_id: option.knowledge_node_id, answer_type: option.answer_type, assessment_mode: 'pet_reference_rubric',
    task_family: option.task_family, prompt: draft.prompt.trim(), task_points: draft.points.map(p => p.trim()),
    stimulus_text: draft.material.trim() || null, stimulus_binding: option.stimulus_binding }) : null
  const locked = busy || !!draft.pending || storageFailed || !!draft.saved
  async function save() {
    if (sending.current || storageFailed || !live.current) return
    const body: OralImport | null = current.current.pending ?? (source && draft.reviewed ? {
      source_type: 'school_material', subject: '英语', grade_band: child.grade, title: draft.title.trim(),
      version_label: draft.edition.trim(), source_ref: 'oral-author:' + crypto.randomUUID(), license_status: 'family_owned_index',
      parent_reviewed: true, content_text: JSON.stringify(source)
    } : null)
    if (!body || !persist({ pending: body })) return
    sending.current = true; setBusy(true); setError('')
    try {
      const id = await api.save(child, body)
      if (live.current && persist({ pending: null, saved: id })) onSaved(id)
    } catch (e) { report(e) }
    finally { sending.current = false; if (live.current) setBusy(false) }
  }
  async function importText(file: File | undefined) {
    if (!file || locked) return
    if (!file.name.toLowerCase().endsWith('.txt') || file.size > 24000) { setError('请选择不超过24KB的纯文本材料。'); return }
    const text = await file.text()
    if (!live.current || current.current.pending || current.current.saved) return
    if (!text.trim() || text.length > 6000 || /[\u0000-\u0008\u000b\u000c\u000e-\u001f]/.test(text)) { setError('材料必须为完整可读文本，最多6000字。'); return }
    persist({ material: text, reviewed: false })
  }
  async function refreshOptions() {
    setOptions(null); setError('')
    try { const v = await api.options(child); if (live.current) setOptions(v) }
    catch (e) { report(e) }
  }
  return <section aria-label="口语原题编写" className="csr-band">
    <h3>口语原题 · {child.nickname}</h3>
    {error && <p role="alert" className="csr-notice">{error}</p>}
    {error && !draft.pending && <button title="重新读取题型" aria-label="重新读取题型" disabled={busy} onClick={() => void refreshOptions()}><RefreshCw /></button>}
    {auth && onRequireParentAuth && <button onClick={onRequireParentAuth}>重新解锁家长操作</button>}
    {storageFailed && <div role="alert"><p>本机草稿无法保存，未发送新请求。</p><button onClick={() => { if (persist(empty(child.child_id))) setStorageFailed(false) }}>清除损坏草稿</button></div>}
    {!options && !error && <p role="status">正在读取可用口语题型…</p>}
    {options && !options.options.length && <p className="csr-notice">当前年级没有可核对的口语题型来源，不能创建或发布。</p>}
    <div className="csr-fields"><label>口语任务<select value={draft.family} disabled={locked || !options} onChange={e => persist({ family: e.target.value, reviewed: false })}>
      <option value="">选择任务类型</option>{options?.options.map(o => <option key={o.task_family} value={o.task_family}>{oralFamilyNames[o.task_family]}</option>)}</select></label>
      <label>资料名称<input maxLength={200} disabled={locked} value={draft.title} onChange={e => persist({ title: e.target.value })} /></label>
      <label>资料版本<input maxLength={100} disabled={locked} value={draft.edition} onChange={e => persist({ edition: e.target.value })} /></label></div>
    <label>完整任务要求<textarea maxLength={1200} disabled={locked} value={draft.prompt} onChange={e => persist({ prompt: e.target.value, reviewed: false })} /></label>
    <label>{['read_aloud', 'retell'].includes(draft.family) ? '完整阅读材料（必填）' : '补充材料（可选）'}<textarea maxLength={6000} disabled={locked} value={draft.material} onChange={e => persist({ material: e.target.value, reviewed: false })} /></label>
    <label><span><Upload size={16} /> 导入纯文本材料</span><input type="file" accept=".txt,text/plain" disabled={locked} onChange={e => { void importText(e.target.files?.[0]); e.target.value = '' }} /></label>
    {draft.points.map((point, i) => <div key={i} style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
      <label style={{ flex: 1 }}>任务点 {i + 1}<textarea maxLength={400} disabled={locked} value={point} onChange={e => persist({ points: draft.points.map((p, j) => j === i ? e.target.value : p), reviewed: false })} /></label>
      <button title={`删除任务点 ${i + 1}`} aria-label={`删除任务点 ${i + 1}`} disabled={locked || draft.points.length <= 1} onClick={() => persist({ points: draft.points.filter((_, j) => j !== i), reviewed: false })}><Trash2 /></button></div>)}
    <button title="增加任务点" aria-label="增加任务点" disabled={locked || draft.points.length >= 12} onClick={() => persist({ points: [...draft.points, ''], reviewed: false })}><Plus /></button>
    {source && <OralTaskPreview source={source} />}
    <label style={{ flexDirection: 'row', alignItems: 'start', marginTop: 16 }}><input type="checkbox" style={{ width: 18, flexShrink: 0 }} disabled={locked} checked={draft.reviewed} onChange={e => persist({ reviewed: e.target.checked })} />我已核对完整原题、材料与任务点，确认有权在本家庭本机使用；这不是云端或录音评分授权。</label>
    <div className="csr-actions">
      {!draft.pending && !draft.saved && <button disabled={locked || !source || !oralPictureSupported(source) || !draft.reviewed || !draft.title.trim() || !draft.edition.trim()} onClick={() => void save()}><Save />保存口语原题</button>}
      {draft.pending && <><p role="status">原保存请求待核对，未创建新的请求。</p><button disabled={busy || storageFailed} onClick={() => void save()}><RefreshCw />重试原保存请求</button>
        <button disabled={busy || storageFailed} onClick={() => { if (persist({ pending: null, reviewed: false })) void refreshOptions() }}><Undo2 />放弃重试，返回草稿</button><p className="csr-muted">放弃重试不会撤回服务端已经保存的原题。</p></>}
      {draft.saved && <><p role="status">完整原题已保存并读回，尚须课标核验与发布。</p><button onClick={() => onSaved(draft.saved!)}>选用已保存原题</button><button onClick={() => persist(empty(child.child_id))}><Plus />编写另一道题</button></>}
    </div>
  </section>
}
