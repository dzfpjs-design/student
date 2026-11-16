import { useEffect, useRef, useState } from 'react'
import { ApiError } from './api'
import { clearSourceRevisionLocalState, readSourceRevisionJournal, saveSourceRevisionJournal,
  sourceRevisionCleared, sourceRevisionPath, sourceRevisionRequest,
  type RevisionSource, type SourceRevisionCommand, type SourceRevisionJournal,
  type SourceRevisionPreview, type SourceRevisionView } from './sourceRevisionApi'
import './KnowledgeSourceRevisionPanel.css'

type Props = {
  childId: string; initialSourceId?: string; initialContent?: string;
  request?: typeof sourceRevisionRequest; onChanged?: () => Promise<void>
}

function DocumentText({ text }: { text: string }): React.JSX.Element {
  try {
    const item = JSON.parse(text) as Record<string, unknown>
    if (item && typeof item.prompt === 'string') return <div className="source-revision-document">
      <p>{item.prompt}</p>
      {typeof item.stimulus === 'string' && <p>{item.stimulus}</p>}
      {Array.isArray(item.task_points) && <ul>{item.task_points.map((p, n) => <li key={n}>{String(p)}</li>)}</ul>}
      <details><summary>逐字核对完整文件</summary><pre>{text}</pre></details>
    </div>
  } catch { /* A damaged legacy excerpt is shown honestly, not parsed into invented fields. */ }
  return <pre className="source-revision-document">{text}</pre>
}

export function KnowledgeSourceRevisionPanel({ childId, initialSourceId = '', initialContent = '',
  request = sourceRevisionRequest, onChanged }: Props): React.JSX.Element {
  const [sources, setSources] = useState<RevisionSource[]>([])
  const [journal, setJournal] = useState<SourceRevisionJournal>({ schema: 1, childId, sourceId: '', text: '', pending: null })
  const [view, setView] = useState<SourceRevisionView | null>(null)
  const [preview, setPreview] = useState<SourceRevisionPreview | null>(null)
  const [confirmed, setConfirmed] = useState(false)
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState('')
  const [blocked, setBlocked] = useState(false)
  const epoch = useRef(0)
  const listEpoch = useRef(0)
  const consumedPreset = useRef('')
  const childRef = useRef(childId)
  childRef.current = childId
  const owns = (generation: number, child = childId): boolean => epoch.current === generation && childRef.current === child
  const persist = (next: SourceRevisionJournal): void => { saveSourceRevisionJournal(next); setJournal(next) }

  useEffect(() => {
    const generation = ++listEpoch.current
    ++epoch.current
    setSources([]); setView(null); setPreview(null); setConfirmed(false); setBusy(false); setBlocked(false); setMessage('')
    let saved: SourceRevisionJournal | null = null
    try { saved = readSourceRevisionJournal(childId) } catch (e) {
      setBlocked(true); setMessage(e instanceof Error ? e.message : '本机恢复记录无法读取。')
    }
    setJournal(saved ?? { schema: 1, childId, sourceId: initialSourceId, text: initialContent, pending: null })
    void request<{ items: RevisionSource[] }>(`/v1/parent/children/${encodeURIComponent(childId)}/knowledge-sources`, { cache: 'no-store' })
      .then((result) => {
        if (result.items.some((item) => item.child_id !== childId)) throw new Error('资料归属不符')
        if (listEpoch.current === generation && childRef.current === childId) setSources(result.items)
      })
      .catch(() => { if (listEpoch.current === generation && childRef.current === childId) setMessage('资料列表未能读取，请重新打开此页。') })
    return () => { ++epoch.current; ++listEpoch.current }
  }, [childId, request])

  useEffect(() => {
    // An import mismatch is an explicit new selection. Clearing an already
    // consumed preset is not a child switch and must not discard a live read.
    if (!initialSourceId) return
    const stamp = JSON.stringify([childId, initialSourceId, initialContent])
    if (consumedPreset.current === stamp) return
    consumedPreset.current = stamp
    try {
      const saved = readSourceRevisionJournal(childId)
      if (saved?.pending) return
      persist({ schema: 1, childId, sourceId: initialSourceId, text: initialContent, pending: null })
      ++epoch.current; setView(null); setPreview(null); setConfirmed(false); setBusy(false)
    } catch {
      setBlocked(true); setMessage('本机恢复记录无法保存，未发送修订。请先核对原记录。')
    }
  }, [childId, initialSourceId, initialContent])

  useEffect(() => {
    const cleared = (event: Event): void => {
      const owner = (event as CustomEvent<{ childId?: string }>).detail?.childId
      if (owner && owner !== childId) return
      ++epoch.current; ++listEpoch.current; setSources([]); setBusy(false); setBlocked(false); setView(null); setPreview(null); setConfirmed(false)
      setJournal({ schema: 1, childId, sourceId: '', text: '', pending: null })
      setMessage('本机修订记录已清除。请重新读取当前资料；没有自动重放操作。')
    }
    window.addEventListener(sourceRevisionCleared, cleared)
    return () => window.removeEventListener(sourceRevisionCleared, cleared)
  }, [childId])

  const read = async (sourceId = journal.sourceId): Promise<void> => {
    if (!sourceId) return
    const generation = ++epoch.current
    setBusy(true); setView(null); setPreview(null); setConfirmed(false); setMessage('')
    try {
      const result = await request<SourceRevisionView>(sourceRevisionPath(childId, sourceId), { cache: 'no-store' })
      if (!owns(generation)) return
      if (result.current.child_id !== childId || result.current.source_id !== sourceId) throw new Error('资料归属已变化。')
      setView(result)
    } catch (e) { if (owns(generation)) setMessage(e instanceof ApiError && e.status === 404 ? '资料已删除或不属于当前孩子。没有恢复旧副本。' : '资料读取失败，没有发送修订。') }
    finally { if (owns(generation)) setBusy(false) }
  }

  const compare = async (): Promise<void> => {
    const generation = ++epoch.current
    setBusy(true); setPreview(null); setConfirmed(false); setMessage('')
    try {
      persist(journal)
      const result = await request<SourceRevisionPreview>(sourceRevisionPath(childId, journal.sourceId) + '/preview', {
        method: 'POST', body: JSON.stringify({ content_text: journal.text }) })
      if (!owns(generation)) return
      if (result.source.child_id !== childId || result.source.source_id !== journal.sourceId) throw new Error('资料归属不符。')
      setPreview(result)
    } catch (e) { if (owns(generation)) setMessage(e instanceof Error ? e.message : '未能核对完整原文。') }
    finally { if (owns(generation)) setBusy(false) }
  }

  const submit = async (operation: 'revise' | 'review', retry = false): Promise<void> => {
    const generation = ++epoch.current
    setBusy(true); setMessage('')
    let committed = false
    try {
      const command: SourceRevisionCommand = retry && journal.pending ? journal.pending : {
        sourceId: journal.sourceId, operation, body: {
          expected_version: operation === 'revise' ? preview!.source_version : view!.source_version,
          client_action_id: crypto.randomUUID(),
          confirmation: operation === 'revise' ? '保留旧稿并创建待审核新版本' : '已核对新版完整内容与来源',
          ...(operation === 'revise' ? { content_text: journal.text } : {}) } }
      persist({ ...journal, pending: command })
      const result = await request<{ current: RevisionSource; source_version: string }>(
        sourceRevisionPath(childId, command.sourceId) + (command.operation === 'review' ? '/review' : ''),
        { method: 'POST', body: JSON.stringify(command.body) })
      if (!owns(generation)) return
      if (result.current.child_id !== childId) throw new Error('回执归属不符，请核对原操作。')
      committed = true
      persist({ ...journal, sourceId: result.current.source_id, text: '', pending: null })
      setPreview(null); setConfirmed(false); setView(null)
      setSources((rows) => [result.current, ...rows.filter((r) => r.source_id !== result.current.source_id)
        .map((r) => r.source_id === result.current.predecessor_id ? { ...r, current_version: 0, review_status: 'superseded' } : r)])
      setMessage(result.current.review_status === 'approved'
        ? '新版来源审核已保存。具体题目的用途授权仍需重新确认；旧题不会自动恢复。'
        : '新版本已保存，旧稿已保留为历史。请读取新版并单独核对审核。')
      try { await onChanged?.() } catch { if (owns(generation)) setMessage('保存已成功，父页面刷新失败。请只读刷新，不要重新提交。') }
    } catch (e) {
      if (owns(generation)) setMessage(committed ? '服务端已保存，但本机记录未清理。可用原操作重试核对回执。'
        : e instanceof Error ? e.message : '结果未确认，请核对原操作或用原操作重试。')
    } finally { if (owns(generation)) setBusy(false) }
  }

  const edit = (changes: Partial<SourceRevisionJournal>): void => {
    try { persist({ ...journal, ...changes }); setPreview(null); setView(null); setConfirmed(false); ++epoch.current }
    catch { setMessage('本机保存失败，未发送操作。'); setBlocked(true) }
  }
  return <section className="source-revision-panel" aria-label="修复不完整的旧资料">
    <h3>修复不完整的旧资料</h3>
    <p>核对同一份完整原文件。旧摘录和孩子历史作答保留；新版另行审核，不继承旧题的用途许可。</p>
    <label>待核对资料<select value={journal.sourceId} disabled={busy || !!journal.pending || blocked}
      onChange={(e) => edit({ sourceId: e.target.value, text: '' })}>
      <option value="">选择资料</option>{sources.map((s) => <option key={s.source_id} value={s.source_id}>{s.title} · {s.review_status === 'superseded' ? '历史旧稿' : s.review_status === 'approved' ? '已审核' : '待审核'}</option>)}
    </select></label>
    <button type="button" disabled={busy || !journal.sourceId} onClick={() => void read()}>读取当前资料与历史</button>
    {!journal.pending && !blocked && (!view || !view.current.predecessor_id) && <>
      <label>完整原文件<input type="file" accept=".txt,.md,.json,.jsonl" disabled={busy} onChange={(e) => {
        const file = e.target.files?.[0]; const generation = epoch.current
        if (file && file.size <= 400000) void file.text().then((text) => { if (owns(generation)) edit({ text }) })
        else if (file) setMessage('请选择不超过400KB的原文件。')
      }} /></label>
      <label>完整原文<textarea value={journal.text} disabled={busy} maxLength={100000} onChange={(e) => edit({ text: e.target.value })} /></label>
      <button type="button" disabled={busy || !journal.sourceId || !journal.text.trim()} onClick={() => void compare()}>仅核对，不保存版本</button>
    </>}
    {preview && <div className="source-revision-comparison">
      <h4>{preview.status === 'incomplete' ? '保存片段与完整原文不一致' : '已保存完整，无需修订'}</h4>
      <p>原文 {preview.replacement_length} 字符；已保存 {preview.stored_length} 字符。整份原文件指纹一致。</p>
      <details><summary>查看旧保存片段</summary>{preview.stored_excerpts.map((x) => <DocumentText key={x.sequence} text={x.text} />)}</details>
      <h4>待保存的完整内容</h4><DocumentText text={preview.replacement_text} />
      {preview.status === 'incomplete' && !journal.pending && <>
        <label className="source-revision-consent"><input type="checkbox" checked={confirmed} onChange={(e) => setConfirmed(e.target.checked)} /><span>我确认保留旧稿，创建独立待审核新版本</span></label>
        <button type="button" disabled={busy || !confirmed} onClick={() => void submit('revise')}>确认创建新版本</button>
        <button type="button" disabled={busy} onClick={() => { setPreview(null); setConfirmed(false) }}>取消，本次不写入</button>
      </>}
    </div>}
    {view && <div><h4>{view.current.title} · {view.current.review_status === 'superseded' ? '历史旧稿，不再用于当前学习' : view.current.review_status === 'approved' ? '新版来源已审核' : '新版待审核'}</h4>
      {view.stored_excerpts.map((x) => <DocumentText key={x.sequence} text={x.text} />)}
      {view.history.map((x) => <p key={x.event_id}>{x.action === 'revise' ? '已创建独立修订版本' : '家长已核对新版来源'} · {new Date(x.occurred_at).toLocaleString('zh-CN')}</p>)}
      {!!view.current.predecessor_id && <button type="button" disabled={busy || !!journal.pending} onClick={() => { setView(null); setConfirmed(false) }}>重新核对完整原文件</button>}
      {!!view.current.current_version && view.current.review_status === 'approved' && <p>请在课程供给中重新选择新版资料、审核题目及其用途许可。旧题仍保持失效，不会自动恢复使用。</p>}
      {!!view.current.current_version && !!view.current.predecessor_id && view.current.review_status === 'quarantined' && !journal.pending && !blocked && <>
        <label className="source-revision-consent"><input type="checkbox" checked={confirmed} onChange={(e) => setConfirmed(e.target.checked)} /><span>我已重新核对新版完整内容与来源。这不授予云处理权限。</span></label>
        <button type="button" disabled={busy || !confirmed} onClick={() => void submit('review')}>保存新版来源审核</button>
      </>}
    </div>}
    {journal.pending && <div role="status"><p>原操作尚待核对。不会自动发送或改变原版本。</p><button type="button" disabled={busy || blocked} onClick={() => void submit(journal.pending!.operation, true)}>用原操作重试并核对回执</button></div>}
    {(journal.pending || blocked) && <button type="button" disabled={busy} onClick={() => clearSourceRevisionLocalState(childId)}>放弃本机恢复记录，不撤销服务端保存</button>}
    {message && <p role="status">{message}</p>}
  </section>
}
