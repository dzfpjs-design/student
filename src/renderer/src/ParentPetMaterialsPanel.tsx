import { useEffect, useRef, useState } from 'react'
import { Check, Headphones, RefreshCw, Square, Undo2, WandSparkles } from 'lucide-react'
import { cancelLocalSpeech, queueLocalPcm } from './LocalInputs'
import type { ParentPetCollection, ParentPetMaterial, ParentPetMaterialsPanelProps, ParentPetMedia, ParentPetPending, ParentPetPrompt } from './petMaterialSupplyTypes'
import { parentPetPendingPrefix } from './petMaterialSupplyTypes'

const mediaKey = (row: ParentPetMedia): string => `${row.media_id}:${row.source_version}:${row.audio.sha256}`
const promptKey = (p: ParentPetPrompt): string => `${p.origin_session_id}:${p.prompt_id}:${p.item_version}`
const statusLabel = (value: string): string => ({ completed: '已制作', processing: '制作中或等待恢复', failed: '制作未完成',
  invalidated: '原题或来源已变化', cancelled: '已取消' }[value] ?? '状态待核实')

function message(error: unknown): string {
  const value = (error as { code?: unknown })?.code
  const code = typeof value === 'string' ? value : ''
  if ((error as { name?: string })?.name === 'SecurityError') return '本机存储不可用，无法保存重试记录；制作与来源修改已暂停。'
  if (code.includes('ENCRYPTION_KEY')) return '本机加密存储尚未配置，未制作音频。'
  if (code.includes('GENERATION_IN_PROGRESS')) return '原制作请求仍在处理中；稍后核对状态，不要重新建一份。'
  if (code.includes('STIMULUS')) return '这道原题缺少已核准的原始听力材料，不能根据题目或答案猜配音。'
  if (code.includes('PARENT_SESSION') || (error as { status?: number })?.status === 401) return '家长认证已失效，请重新解锁后核对原操作。'
  if ((error as { status?: number })?.status === 409) return '原题、材料或操作版本已变化，请刷新核对；未改绑到其他题。'
  return error instanceof Error ? error.message : '本机服务暂不可用，原操作尚未确认。'
}

function readPending(key: string): ParentPetPending | null {
  const raw = localStorage.getItem(key)
  if (!raw) return null
  const value = JSON.parse(raw) as ParentPetPending
  const id = (v: unknown): v is string => typeof v === 'string' && v.length > 0 && v.length <= 200
  if (!id(value.client_action_id) || !['materialize', 'bind', 'review'].includes(value.kind)
    || (value.kind === 'bind' && !id(value.artifact_id))
    || (value.kind === 'review' && (!id(value.media_id) || !/^[a-f0-9]{64}$/.test(value.audio_sha256)
      || !['approve', 'retract'].includes(value.operation) || (value.full_spoken_reference !== undefined && (value.full_spoken_reference !== true || value.operation !== 'approve'))))) throw new Error('上次操作记录损坏，请先核对服务器状态。')
  return value
}

/** Parent audition validates bytes but does not fabricate source_reviewed=true. */
export async function decodeParentPetPreview(bytes: ArrayBuffer, row: ParentPetMedia): Promise<{ pcm: ArrayBuffer; rate: number }> {
  const a = row.audio, rate = a.sample_rate, frames = a.frames
  if (row.kind !== 'listening' || a.status !== 'usable_signal' || !rate || ![16000, 24000, 48000].includes(rate)
    || !frames || !Number.isSafeInteger(frames) || frames < rate / 2 || frames > rate * 600
    || a.channels !== 1 || a.sample_width !== 2 || bytes.byteLength < 44 || bytes.byteLength > 24 * 1024 * 1024
    || !/^[a-f0-9]{64}$/.test(a.sha256 ?? '')) throw new Error('音频格式尚未核实，未播放替代内容。')
  const digest = Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', bytes))).map(v => v.toString(16).padStart(2, '0')).join('')
  if (digest !== a.sha256) throw new Error('音频内容已变化，请重新载入。')
  const decoded = await new OfflineAudioContext(1, frames, rate).decodeAudioData(bytes.slice(0))
  if (decoded.sampleRate !== rate || decoded.length !== frames || decoded.numberOfChannels !== 1) throw new Error('音频长度或采样格式不一致。')
  const samples = decoded.getChannelData(0), pcm = new Int16Array(frames)
  for (let i = 0; i < frames; i++) {
    if (!Number.isFinite(samples[i])) throw new Error('音频采样不可用。')
    pcm[i] = Math.max(-32768, Math.min(32767, Math.round(samples[i] * 32768)))
  }
  return { pcm: pcm.buffer, rate }
}

export function ParentPetMaterialsPanel(props: ParentPetMaterialsPanelProps): React.JSX.Element {
  return <ChildMaterials key={props.childId} {...props} />
}

function ChildMaterials(props: ParentPetMaterialsPanelProps): React.JSX.Element {
  const prompts = props.prompts.filter(p => p.child_id === props.childId && p.item.pet_audio_modality === 'listening')
  const [selected, setSelected] = useState('')
  const prompt = prompts.find(p => promptKey(p) === selected)
  return <section className="parent-pet-materials" aria-label="听力原材料准备">
    <style>{`
      .parent-pet-materials { color:#263b38; font-size:14px; line-height:1.6; letter-spacing:0; min-width:0; overflow-wrap:anywhere; }
      .parent-pet-materials * { box-sizing:border-box; min-width:0; }
      .parent-pet-materials header,.parent-pet-materials .pet-actions { display:flex; gap:10px; align-items:center; flex-wrap:wrap; }
      .parent-pet-materials header { justify-content:space-between; }
      .parent-pet-materials h3 { font-size:18px; margin:0; }
      .parent-pet-materials h4 { font-size:15px; margin:14px 0 6px; }
      .parent-pet-materials select { width:100%; max-width:100%; padding:9px; margin:6px 0; border:1px solid #c7d3ce; border-radius:4px; background:#fff; color:inherit; font:inherit; }
      .parent-pet-materials button { display:inline-flex; align-items:center; justify-content:center; gap:6px; min-height:38px; padding:7px 11px; border:1px solid #bccbc5; border-radius:4px; background:#fff; color:#23483b; box-shadow:none; font:inherit; white-space:normal; cursor:pointer; }
      .parent-pet-materials button:hover:not(:disabled) { background:#eef5f1; border-color:#6e9584; }
      .parent-pet-materials button:disabled { background:#f3f5f4; color:#858f8b; border-color:#e0e6e3; cursor:default; }
      .parent-pet-materials button:focus-visible,.parent-pet-materials select:focus-visible { outline:2px solid #497cab; outline-offset:2px; }
      .parent-pet-materials svg { width:18px; height:18px; flex-shrink:0; }
      .parent-pet-materials .pet-band { border-top:1px solid #dbe3df; padding:14px 0; }
      .parent-pet-materials .pet-note { color:#596a69; }
      .parent-pet-materials .pet-warning { color:#8c3946; }
      .parent-pet-materials blockquote { margin:10px 0; padding:8px 12px; border-left:3px solid #759790; white-space:pre-wrap; }
      .parent-pet-materials label { display:block; }
      .parent-pet-materials input[type=checkbox] { width:18px; height:18px; margin:0 8px 0 0; vertical-align:middle; }
    `}</style>
    <header><h3>听力原材料</h3>{props.onRefreshPrompts && <button type="button" title="刷新原题" aria-label="刷新原题" disabled={props.loading} onClick={props.onRefreshPrompts}><RefreshCw /></button>}</header>
    <p className="pet-warning">本机合成练习音频，不是官方真题录音。家长试听与来源确认不代表孩子听过、听懂或考试通过。</p>
    {props.loading ? <p role="status">正在核对原题…</p> : props.error ? <p role="alert">原题暂未核实，请刷新后再准备材料。</p> : <>
      <label>选择原题<select aria-label="选择原题" value={prompt ? selected : ''} onChange={e => setSelected(e.target.value)}>
        <option value="">{prompts.length ? '请选择已保存的听力原题' : '当前没有已保存的听力原题'}</option>
        {prompts.map(p => <option key={promptKey(p)} value={promptKey(p)}>{p.item.track === 'school' ? '校内' : 'PET'} · {p.item.prompt} · {new Date(p.created_at).toLocaleDateString()}</option>)}
      </select></label>
      {prompt && <PromptMaterials key={promptKey(prompt)} {...props} prompt={prompt} />}
    </>}
  </section>
}

function PromptMaterials({ prompt, request, requestPreviewAudio }: ParentPetMaterialsPanelProps & { prompt: ParentPetPrompt }): React.JSX.Element {
  const base = `/v1/sessions/${encodeURIComponent(prompt.origin_session_id)}/pet-media`
  const storageKey = parentPetPendingPrefix(prompt.child_id) + promptKey(prompt)
  const [recovery] = useState(() => { try { return { pending: readPending(storageKey), error: '' } } catch (e) { return { pending: null, error: message(e) } } })
  const [pending, setPending] = useState(recovery.pending)
  const [collection, setCollection] = useState<ParentPetCollection | null>(null)
  const [notice, setNotice] = useState(recovery.error)
  const [busy, setBusy] = useState(false)
  const [ready, setReady] = useState(false)
  const [artifactId, setArtifactId] = useState('')
  const [mediaId, setMediaId] = useState('')
  const [confirmed, setConfirmed] = useState(false)
  const [listened, setListened] = useState('')
  const [previewing, setPreviewing] = useState(false)
  const alive = useRef(true), lock = useRef(false), generation = useRef(0)
  const preview = useRef<AbortController | null>(null), rendered = useRef(false), previewSpeechKey = useRef<string | null>(null)
  const row = collection?.listening.find(r => r.media_id === mediaId)
  const material = collection?.materials.find(r => r.artifact_id === artifactId)
  const stalePrompt = ['cancelled', 'invalidated'].includes(prompt.status)
  const validRow = (r: ParentPetMedia): boolean => r.child_id === prompt.child_id && r.session_id === prompt.origin_session_id
    && r.prompt_id === prompt.prompt_id && r.item_version === prompt.item_version && r.diagnostic_item_id === prompt.diagnostic_item_id
    && r.kind === 'listening' && r.local_only === true && r.cloud_transmission_allowed === false
  const reload = async (): Promise<void> => {
    const current = ++generation.current
    setReady(false)
    const loaded = await request<ParentPetCollection>(`${base}/prompt/${encodeURIComponent(prompt.prompt_id)}?item_version=${encodeURIComponent(prompt.item_version)}`)
    if (!alive.current || current !== generation.current) return
    if (loaded.child_id !== prompt.child_id || loaded.session_id !== prompt.origin_session_id || loaded.prompt_id !== prompt.prompt_id
      || loaded.item_version !== prompt.item_version || loaded.local_only !== true || loaded.cloud_transmission_allowed !== false
      || !Array.isArray(loaded.listening) || !Array.isArray(loaded.materials) || !loaded.listening.every(validRow)
      || !loaded.materials.every(m => m.prompt_id === prompt.prompt_id && m.item_version === prompt.item_version
        && m.diagnostic_item_id === prompt.diagnostic_item_id && m.local_only === true && m.cloud_transmission_allowed === false)) {
      throw new Error('返回材料不属于当前孩子的这道原题，未显示或确认。')
    }
    setCollection(loaded); setReady(true)
  }
  const stop = (): void => { preview.current?.abort(); preview.current = null; if (rendered.current && previewSpeechKey.current) cancelLocalSpeech(previewSpeechKey.current); rendered.current = false; previewSpeechKey.current = null; setPreviewing(false) }
  useEffect(() => {
    alive.current = true
    void reload().catch(e => { if (alive.current) setNotice(message(e)) })
    return () => { alive.current = false; generation.current++; preview.current?.abort(); if (rendered.current && previewSpeechKey.current) cancelLocalSpeech(previewSpeechKey.current); rendered.current = false; previewSpeechKey.current = null }
  }, []) // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { if (stalePrompt) { stop(); setListened(''); setConfirmed(false) } }, [stalePrompt]) // eslint-disable-line react-hooks/exhaustive-deps
  const execute = async (action: ParentPetPending): Promise<void> => {
    if (lock.current || !ready || stalePrompt) return
    lock.current = true; setBusy(true); stop(); setNotice('')
    try {
      localStorage.setItem(storageKey, JSON.stringify(action)); setPending(action)
      if (action.kind === 'materialize') {
        const result = await request<{ material: ParentPetMaterial }>(`${base}/prompt/${encodeURIComponent(prompt.prompt_id)}/materialize`,
          { method: 'POST', body: JSON.stringify({ item_version: prompt.item_version, client_action_id: action.client_action_id }) })
        if (result.material.prompt_id !== prompt.prompt_id || result.material.item_version !== prompt.item_version) throw new Error('制作回执原题不一致。')
        if (alive.current) setArtifactId(result.material.artifact_id)
      } else if (action.kind === 'bind') {
        const result = await request<{ media: ParentPetMedia }>(`${base}/listening/begin`, { method: 'POST', body: JSON.stringify({
          prompt_id: prompt.prompt_id, item_version: prompt.item_version, artifact_id: action.artifact_id, client_action_id: action.client_action_id }) })
        if (!validRow(result.media) || result.media.artifact_id !== action.artifact_id) throw new Error('试听绑定回执不一致。')
        if (alive.current) setMediaId(result.media.media_id)
      } else {
        const result = await request<{ media: ParentPetMedia }>(`${base}/${encodeURIComponent(action.media_id)}/source-review`, { method: 'POST', body: JSON.stringify({
          audio_sha256: action.audio_sha256, operation: action.operation, client_action_id: action.client_action_id,
          ...(action.full_spoken_reference ? { full_spoken_reference: true } : {}) }) })
        if (!validRow(result.media) || result.media.media_id !== action.media_id || result.media.audio.sha256 !== action.audio_sha256) throw new Error('来源确认回执不一致。')
      }
      localStorage.removeItem(storageKey)
      if (!alive.current) return
      setPending(null); setConfirmed(false); await reload()
      if (alive.current) setNotice(action.kind === 'review' ? '来源操作回执已核对；当前状态已重新载入，没有计入孩子播放或成绩。' : '材料记录已保存，可以核对并试听。')
    } catch (e) { if (alive.current) setNotice(message(e)) }
    finally { lock.current = false; if (alive.current) setBusy(false) }
  }
  const audition = async (): Promise<void> => {
    if (!row || !ready || busy || !row.audio.sha256) return
    stop(); const controller = new AbortController(); preview.current = controller
    const key = mediaKey(row), current = (): boolean => alive.current && !controller.signal.aborted && preview.current === controller
    setPreviewing(true); setNotice(''); setListened(''); setConfirmed(false)
    try {
      const bytes = await requestPreviewAudio(prompt.origin_session_id, row.media_id, row.audio.sha256, controller.signal)
      if (!current()) return
      const decoded = await decodeParentPetPreview(bytes, row)
      if (!current()) return
      previewSpeechKey.current = `parent-pet-preview:${key}`
      const result = await queueLocalPcm(decoded.pcm, decoded.rate, prompt.origin_session_id, () => { if (current()) rendered.current = true },
        { localOnly: true, isCurrent: current, speechKey: `parent-pet-preview:${key}`, expiresAt: Date.now() + 60_000 })
      if (current()) { if (result === 'finished' && rendered.current) setListened(key); else setNotice('试听未完整结束，尚未确认来源。') }
    } catch (e) { if (current()) setNotice(message(e)) }
    finally { if (current()) { rendered.current = false; previewSpeechKey.current = null; setPreviewing(false); preview.current = null } }
  }
  const writeBlocked = busy || !ready || !!pending || !!recovery.error || stalePrompt
  const rowUnavailable = !row || ['cancelled', 'invalidated'].includes(row.status) || row.audio.status !== 'usable_signal'
  return <div>
    <div className="pet-band"><h4>原题与原文</h4><p>{prompt.item.prompt}</p>
      {prompt.item.stimulus_text ? <blockquote>{prompt.item.stimulus_text}</blockquote> : <p className="pet-warning">缺已核准的原始听力文本，不制作猜测配音。</p>}
      {stalePrompt && <p role="alert">这道原题已取消或失效，不能继续制作或确认。</p>}
      <button type="button" disabled={writeBlocked || prompt.status !== 'waiting_answer' || !prompt.item.stimulus_text}
        onClick={() => void execute({ kind: 'materialize', client_action_id: crypto.randomUUID() })}><WandSparkles />制作本机练习音频</button>
    </div>
    <div className="pet-band"><h4>准备的材料</h4>
      {!ready && <p role="status">材料状态待核对。</p>}
      {ready && !collection?.materials.length && <p>尚无本机制作的材料。</p>}
      {!!collection?.materials.length && <><label>选择材料<select aria-label="选择材料" disabled={busy || !ready} value={artifactId} onChange={e => setArtifactId(e.target.value)}>
        <option value="">请选择材料</option>{collection.materials.map((m, i) => <option key={m.artifact_id} value={m.artifact_id}>本机合成练习 {i + 1} · {statusLabel(m.status)}</option>)}
      </select></label><button disabled={writeBlocked || material?.status !== 'completed'} onClick={() => {
        const existing = collection.listening.find(r => r.artifact_id === artifactId && r.status !== 'cancelled')
        if (existing) { stop(); setMediaId(existing.media_id); setListened(''); setConfirmed(false) }
        else void execute({ kind: 'bind', artifact_id: artifactId, client_action_id: crypto.randomUUID() })
      }}><Headphones />载入供试听</button></>}
    </div>
    <div className="pet-band"><h4>原音试听与来源核对</h4>
      <label>选择原音<select aria-label="选择原音" disabled={busy || !ready} value={mediaId} onChange={e => { stop(); setMediaId(e.target.value); setListened(''); setConfirmed(false) }}>
        <option value="">请选择这道原题的音频</option>{collection?.listening.map((m, i) => <option key={m.media_id} value={m.media_id}>音频 {i + 1} · {m.source_reviewed ? '来源已确认' : '来源未确认或已撤回'}</option>)}
      </select></label>
      {row && <>{row.qualification.full_audio_control_reference_available !== true && <p className="pet-warning">要让孩子在播放中说“停一下”，还需确认上方原文包含整份录音的全部说话内容。试听与核对保存后，孩子才能开始听题。</p>}<p>{row.source_reviewed ? '来源已确认' : '来源未确认或已撤回'} · {row.qualification.required_play_count ? `原题要求 ${row.qualification.required_play_count} 遍` : '原题播放次数待核实'}</p>
        <div className="pet-actions"><button disabled={rowUnavailable || busy || !ready || previewing || stalePrompt} onClick={() => void audition()}><Headphones />试听原音</button>
          {previewing && <button onClick={stop}><Square />停止试听</button>}</div>
        <label><input type="checkbox" checked={confirmed} disabled={writeBlocked || listened !== mediaKey(row) || previewing || rowUnavailable}
          onChange={e => setConfirmed(e.target.checked)} />已完整试听：上方原文包含整份录音的全部说话内容（含开头和结尾），没有遗漏、额外话语或答案讲解</label>
        <div className="pet-actions"><button disabled={writeBlocked || !confirmed || listened !== mediaKey(row) || previewing || rowUnavailable}
          onClick={() => void execute({ kind: 'review', media_id: row.media_id, audio_sha256: row.audio.sha256!, operation: 'approve', full_spoken_reference: true, client_action_id: crypto.randomUUID() })}><Check />确认来源</button>
          <button disabled={writeBlocked || !row.source_reviewed} onClick={() => void execute({ kind: 'review', media_id: row.media_id, audio_sha256: row.audio.sha256!, operation: 'retract', client_action_id: crypto.randomUUID() })}><Undo2 />撤回来源确认</button></div>
      </>}
    </div>
    <div className="pet-actions"><button disabled={busy || previewing} onClick={() => { stop(); void reload().catch(e => setNotice(message(e))) }}><RefreshCw />核对已保存状态</button>
      {pending && <><button disabled={busy || !ready || stalePrompt || !!recovery.error} onClick={() => void execute(pending)}><RefreshCw />重试原操作</button>
        <button disabled={busy || !ready} onClick={() => { localStorage.removeItem(storageKey); setPending(null); setNotice('仅停止本机待重试操作；服务器已有材料与来源确认均保留。') }}>放弃本机重试</button></>}</div>
    {pending && <p role="status">上次操作结果尚未确认。重启不会自动重试、制作或播放。</p>}
    {notice && <p role="status">{notice}</p>}
  </div>
}
