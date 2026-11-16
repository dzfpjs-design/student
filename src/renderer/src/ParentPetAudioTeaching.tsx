import { useEffect, useRef, useState } from 'react'
import { Check, RefreshCw, RotateCcw, ShieldOff, Settings2 } from 'lucide-react'
import { requestParentPetAudioPurpose } from './api'
import { parentPetPendingPrefix } from './petMaterialSupplyTypes'
import { ParentPetAudioSettings } from './ParentPetAudioSettings'

type Grant = { revision: number; allowed: boolean; expires_at: string; day_limit_micros: number; request_limit_micros: number; action_id: string }
type Authorization = { child_id: string; configured: boolean; current: boolean; grant: Grant | null }
type Work = { work_id: string; child_id: string; state: string; generation: number; question: string | null; created_at: string;
  source_verification?: string; delivery_status?: string }
type Pending = { childId: string; kind: 'grant' | 'retry'; workId?: string; body: Record<string, unknown> }
type Requester = <T>(path: string, init?: RequestInit) => Promise<T>

function readPending(key: string, childId: string, text = localStorage.getItem(key)): Pending | null {
  if (!text) return null
  const value = JSON.parse(text) as Pending
  if (value.childId !== childId || !['grant', 'retry'].includes(value.kind)
    || typeof value.body?.action_id !== 'string' || value.body.action_id.length > 200
    || JSON.stringify(value) !== text
    || (value.kind === 'retry' && (typeof value.workId !== 'string' || value.workId.length > 200))) {
    throw new Error('上次操作记录无法核对，未发送任何请求。')
  }
  return value
}

function statusName(work: Work): string {
  // Current source verification takes precedence over a historical delivery receipt.
  if (work.state === 'verification_pending' || work.source_verification === 'unavailable') return '录音暂时无法读取，等待核验；不会重新发送'
  if (work.state === 'invalidated' || work.source_verification === 'changed') return '授权或原回答已变化，当前结果不可用于交付'
  if (work.source_verification !== undefined && work.source_verification !== 'current') return '原回答与建议尚待核验，未确认交付'
  if (work.delivery_status === 'awaiting_receipt') return '建议已保存，正在核对学习伙伴的原交付回执；不会重新发送'
  if (work.delivery_status === 'retired') return '建议保留为历史记录，已停止向当前学习交付'
  if (work.state === 'delivered') return work.source_verification === 'current' && work.delivery_status === 'runtime_delivered'
    ? '建议已交给学习伙伴' : '建议交付状态待核对'
  return { queued: '等待处理', running: '正在听原音', response_saved: '建议已保存，等待交给学习伙伴',
    unknown: '结果未确认，不会自动重发', failed: '发送前未完成' }[work.state] ?? '状态待核对'
}

function canRetry(work: Work): boolean {
  return ['unknown', 'failed'].includes(work.state)
    && (work.source_verification === undefined || work.source_verification === 'current')
    && (work.delivery_status === undefined || work.delivery_status === 'pending')
}

export function ParentPetAudioTeaching({ childId, request = requestParentPetAudioPurpose }: { childId: string; request?: Requester }): React.JSX.Element {
  const [authorization, setAuthorization] = useState<Authorization | null>(null)
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [works, setWorks] = useState<Work[]>([])
  const [allowed, setAllowed] = useState(false)
  const [daily, setDaily] = useState('1.00')
  const [single, setSingle] = useState('0.10')
  const [hours, setHours] = useState('24')
  const [pending, setPending] = useState<Pending | null>(null)
  const [damagedPending, setDamagedPending] = useState(false)
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState('')
  const [acceptedRetry, setAcceptedRetry] = useState<string | null>(null)
  const epoch = useRef(0)
  const lock = useRef(false)
  const activeRequest = useRef<AbortController | null>(null)
  const damagedRaw = useRef<string | null>(null)
  const base = `/v1/parent/children/${encodeURIComponent(childId)}/pet-audio`
  // Existing parent PET privacy cleanup already owns this child-scoped prefix.
  const storageKey = parentPetPendingPrefix(childId) + 'original-audio-purpose'

  function refreshPending(): void {
    let text: string | null = null
    try { text = localStorage.getItem(storageKey); setPending(readPending(storageKey, childId, text)); setDamagedPending(false) }
    catch { damagedRaw.current = text; setDamagedPending(true); setNotice('上次操作记录无法核对，未发送任何请求。') }
  }
  function releasePending(value: Pending): boolean {
    if (localStorage.getItem(storageKey) !== JSON.stringify(value)) { refreshPending(); return false }
    localStorage.removeItem(storageKey)
    return true
  }

  async function load(run: number): Promise<void> {
    const [auth, list] = await Promise.all([request<Authorization>(`${base}/authorization`, { cache: 'no-store' }),
      request<{ child_id: string; items: Work[] }>(`${base}/work`, { cache: 'no-store' })])
    if (run !== epoch.current) return
    if (auth.child_id !== childId || list.child_id !== childId || list.items.some(row => row.child_id !== childId)) throw new Error('孩子归属未核实，未展示旧结果。')
    setAuthorization(auth); setWorks(list.items); setAllowed(auth.current)
    if (auth.grant) { setDaily((auth.grant.day_limit_micros / 1_000_000).toFixed(2)); setSingle((auth.grant.request_limit_micros / 1_000_000).toFixed(2)) }
  }
  useEffect(() => {
    const run = ++epoch.current
    lock.current = false; setBusy(false)
    setAuthorization(null); setWorks([]); setPending(null); setDamagedPending(false); setNotice(''); setAcceptedRetry(null)
    refreshPending()
    const invalidate = (): void => {
      ++epoch.current; activeRequest.current?.abort(); lock.current = false; setBusy(false); setAuthorization(null); setWorks([])
      setNotice('隐私或本地服务状态已变化。已停止继续发送，请刷新并核对原操作。')
    }
    window.addEventListener('hsc-privacy-pause', invalidate)
    window.addEventListener('hsc-runtime-restarted', invalidate)
    void load(run).catch(() => { if (run === epoch.current) setNotice('原音教学状态暂不可用，请刷新核对。') })
    return () => {
      ++epoch.current; activeRequest.current?.abort()
      window.removeEventListener('hsc-privacy-pause', invalidate)
      window.removeEventListener('hsc-runtime-restarted', invalidate)
    }
  }, [childId, request])

  async function send(value: Pending): Promise<void> {
    if (lock.current || damagedPending) return
    lock.current = true; setBusy(true); setNotice('')
    const run = epoch.current
    const controller = new AbortController(); activeRequest.current = controller
    let retained = false
    try {
      if (localStorage.getItem(storageKey) !== null) { refreshPending(); throw new Error('Another operation owns this key') }
      const raw = JSON.stringify(value)
      localStorage.setItem(storageKey, raw)
      if (localStorage.getItem(storageKey) !== raw) { refreshPending(); throw new Error('Journal readback failed') }
      retained = true
      setPending(value)
      if (run !== epoch.current || controller.signal.aborted) throw new Error('Context changed before send')
      const path = value.kind === 'grant' ? `${base}/authorization` : `${base}/work/${encodeURIComponent(value.workId!)}/retry`
      const receipt = await request<{ child_id?: string; work_id?: string; retry_action_id?: string; retry_generation?: number; grant?: Grant }>(path, { method: 'POST', body: JSON.stringify(value.body), signal: controller.signal })
      if (run !== epoch.current || controller.signal.aborted) return
      if (value.kind === 'grant' ? receipt.child_id !== childId || receipt.grant?.action_id !== value.body.action_id
        : receipt.child_id !== childId || receipt.work_id !== value.workId || receipt.retry_action_id !== value.body.action_id || receipt.retry_generation !== Number(value.body.expected_generation) + 1) {
        throw new Error('操作回执归属不一致。')
      }
      if (!releasePending(value)) throw new Error('Journal owner changed')
      setPending(null); setAcceptedRetry(null)
      await load(run)
      if (run === epoch.current) setNotice('操作已保存。')
    } catch {
      if (run === epoch.current) setNotice(retained ? '操作结果尚未确认。请核对原操作，不会自动重复发送。' : '本机存储不可用，未发送授权或重试。')
    } finally {
      if (activeRequest.current === controller) activeRequest.current = null
      if (run === epoch.current) { lock.current = false; setBusy(false) }
    }
  }

  async function recover(): Promise<void> {
    if (!pending || lock.current) return
    lock.current = true; setBusy(true)
    const run = epoch.current
    try {
      let found = false
      if (pending.kind === 'grant') {
        const receipt = await request<{ child_id: string; state: string; grant: Grant | null }>(
          `${base}/authorization/actions/${encodeURIComponent(String(pending.body.action_id))}`, { cache: 'no-store' })
        found = receipt.child_id === childId && receipt.state === 'applied' && receipt.grant?.action_id === pending.body.action_id
      } else {
        const receipt = await request<{ child_id: string; work_id: string; action_id: string; state: string; generation: number | null }>(
          `${base}/work/${encodeURIComponent(pending.workId!)}/retry/actions/${encodeURIComponent(String(pending.body.action_id))}`, { cache: 'no-store' })
        found = receipt.child_id === childId && receipt.work_id === pending.workId && receipt.action_id === pending.body.action_id
          && receipt.state === 'applied' && receipt.generation === Number(pending.body.expected_generation) + 1
      }
      if (run !== epoch.current) return
      if (found && releasePending(pending)) { setPending(null); setNotice('原操作已找到，没有补发请求。') }
      else setNotice('尚未找到原操作。已保持暂停；可放弃本机操作后重新确认。')
      await load(run)
    } catch { if (run === epoch.current) setNotice('仍未取得回执，没有补发请求。') }
    finally { if (run === epoch.current) { lock.current = false; setBusy(false) } }
  }

  const dayMicros = Math.round(Number(daily) * 1_000_000), requestMicros = Math.round(Number(single) * 1_000_000)
  const valid = Number.isFinite(dayMicros) && requestMicros > 0 && requestMicros <= dayMicros && dayMicros <= 100_000_000
  function save(nextAllowed: boolean): void {
    if (!authorization || (nextAllowed && !valid) || pending || damagedPending) return
    void send({ childId, kind: 'grant', body: { allowed: nextAllowed,
      expires_at: new Date(Date.now() + Number(hours) * 3_600_000).toISOString(),
      day_limit_micros: nextAllowed ? dayMicros : authorization.grant?.day_limit_micros ?? 1,
      request_limit_micros: nextAllowed ? requestMicros : authorization.grant?.request_limit_micros ?? 1,
      expected_revision: authorization.grant?.revision ?? 0, action_id: crypto.randomUUID() } })
  }
  return <section className="pet-audio-teaching" aria-label="PET原音教学授权">
    <style>{`.pet-audio-teaching{padding:18px 0;border-bottom:1px solid #cfd9d5;letter-spacing:0;overflow-wrap:anywhere;color:#293d39}
      .pet-audio-teaching h3{font-size:18px;margin:0 0 10px}.pet-audio-teaching p{font-size:14px;line-height:1.6}
      .pet-audio-teaching .audio-controls{display:flex;gap:12px;flex-wrap:wrap;align-items:end}
      .pet-audio-teaching label{display:flex;gap:8px;align-items:center;flex-wrap:wrap}.pet-audio-teaching input[type=number]{width:90px;max-width:100%}
      .pet-audio-teaching label.audio-check{display:grid;grid-template-columns:18px minmax(0,1fr);align-items:start}.pet-audio-teaching .audio-check input{margin:3px 0}
      .pet-audio-teaching button{display:inline-flex;align-items:center;gap:6px;min-height:36px;border:1px solid #adbfb5;border-radius:4px;background:#fff;padding:6px 10px;color:inherit}
      .pet-audio-teaching button:disabled{opacity:.5}.pet-audio-teaching svg{width:16px;height:16px;flex-shrink:0}
      .pet-audio-teaching ul{padding:0;list-style:none}.pet-audio-teaching li{padding:12px 0;border-top:1px solid #dce3df}.pet-audio-teaching .audio-warning{color:#8e3746}`}</style>
    <h3>PET原音教学</h3>
    <button onClick={() => setSettingsOpen(value => !value)}><Settings2 />{settingsOpen ? '收起原音模型配置' : '配置原音模型'}</button>
    {settingsOpen && <ParentPetAudioSettings key={childId} childId={childId} />}
    <p>仅发送当前原回答录音及必要题目。模型只给练习建议，不认证掌握或考试分数。每次发送占用保守预算，断流或结果未知也保留占用。</p>
    <p>{authorization ? authorization.current ? '此用途已获许可' : '此用途尚未获得当前许可' : '正在读取授权状态'}{authorization && !authorization.configured ? ' · 原音模型与费用上限尚未配置' : ''}</p>
    <div className="audio-controls">
      <label className="audio-check"><input type="checkbox" checked={allowed} onChange={event => setAllowed(event.target.checked)} disabled={busy || !!pending || !authorization?.configured} /><span>允许原音练习建议</span></label>
      <label>每日预算（元）<input aria-label="原音每日预算" type="number" min="0.01" max="100" step="0.01" value={daily} onChange={event => setDaily(event.target.value)} disabled={busy || !!pending} /></label>
      <label>单次上限（元）<input aria-label="原音单次上限" type="number" min="0.01" max="100" step="0.01" value={single} onChange={event => setSingle(event.target.value)} disabled={busy || !!pending} /></label>
      <label>有效期<select aria-label="原音授权有效期" value={hours} onChange={event => setHours(event.target.value)} disabled={busy || !!pending}><option value="24">24小时</option><option value="168">7天</option></select></label>
      <button disabled={busy || !!pending || damagedPending || !authorization || !valid || (allowed && !authorization.configured)} onClick={() => save(allowed)}><Check />保存授权</button>
      <button disabled={busy || !!pending || !authorization?.grant?.allowed} onClick={() => save(false)}><ShieldOff />撤销用途许可</button>
      <button title="刷新原音教学状态" aria-label="刷新原音教学状态" disabled={busy} onClick={() => { void load(epoch.current).catch(() => setNotice('刷新未完成。')) }}><RefreshCw /></button>
    </div>
    {pending && <div><p className="audio-warning">上次操作待核对</p><button disabled={busy} onClick={() => { void recover() }}><RefreshCw />核对原操作</button>{' '}
      <button disabled={busy} onClick={() => { try { if (releasePending(pending)) { setPending(null); setNotice('已放弃本机待处理操作；服务器记录未撤销。') } } catch { setNotice('本机记录仍无法清理，没有发送请求。') } }}>放弃本机操作</button></div>}
    {damagedPending && <button disabled={busy} onClick={() => { try {
      if (localStorage.getItem(storageKey) !== damagedRaw.current) { refreshPending(); return }
      localStorage.removeItem(storageKey); setDamagedPending(false); setNotice('仅清除本机损坏记录；服务器操作未撤销，请先刷新核对。')
    } catch { setNotice('本机记录仍无法清理，没有发送请求。') } }}>清除本机损坏记录</button>}
    {notice && <p role="status">{notice}</p>}
    <ul>{works.map(work => <li key={work.work_id}><strong>{work.question ?? '原回答录音'}</strong><p>{statusName(work)} · {new Date(work.created_at).toLocaleString()}</p>
      {canRetry(work) && <><label className="audio-check"><input type="checkbox" checked={acceptedRetry === work.work_id} onChange={event => setAcceptedRetry(event.target.checked ? work.work_id : null)} disabled={busy || !!pending} /><span>确认重新发送同一原音，可能再次收费；旧未知占用不退还</span></label>
        <button disabled={busy || !!pending || acceptedRetry !== work.work_id || !authorization?.current} onClick={() => { void send({ childId, kind: 'retry', workId: work.work_id, body: { expected_generation: work.generation, action_id: crypto.randomUUID(), acknowledge_possible_charge: true } }) }}><RotateCcw />确认重新发送</button></>}
    </li>)}</ul>
  </section>
}
