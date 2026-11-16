import { useEffect, useRef, useState } from 'react'
import { RefreshCw, Save, Trash2 } from 'lucide-react'
import { PET_AUDIO_PURPOSE, type PetAudioSettingsSummary } from '../../shared/petAudioSettings'
import { requestPetAudioSettings, resetRuntimeCache } from './api'
import { parentPetPendingPrefix } from './petMaterialSupplyTypes'
import { PET_AUDIO_TARIFF, currentPetAudioTariff } from '../../shared/petAudioTariffs'
import type { PetCatalogPreview } from '../../shared/petAudioCatalog'

export function ParentPetAudioSettings({ childId }: { childId: string }): React.JSX.Element {
  const [summary, setSummary] = useState<PetAudioSettingsSummary | null>(null)
  const [baseUrl, setBaseUrl] = useState('https://dashscope.aliyuncs.com/compatible-mode/v1')
  const [model, setModel] = useState(''), [apiKey, setApiKey] = useState('')
  const [enabled, setEnabled] = useState(false)
  const [notice, setNotice] = useState(''), [busy, setBusy] = useState(false), [pending, setPending] = useState(false)
  const [catalogPreview, setCatalogPreview] = useState<PetCatalogPreview | null>(null), [useImported, setUseImported] = useState(false)
  const tariff = useImported && catalogPreview ? catalogPreview.tariff : summary?.catalog?.tariff ?? PET_AUDIO_TARIFF
  const epoch = useRef(0), lock = useRef(false)
  const observedPending = useRef<string | null>(null)
  const key = parentPetPendingPrefix(childId) + 'original-audio-settings'

  async function read(run: number): Promise<void> {
    if (lock.current || epoch.current !== run) return
    lock.current = true; setBusy(true); setSummary(null)
    try {
      const value = await requestPetAudioSettings(childId, { kind: 'read' })
      if (epoch.current !== run) return
      if (value.purpose !== PET_AUDIO_PURPOSE) throw Error('配置回执类型不一致。')
      setSummary(value); setBaseUrl(value.baseUrl); setModel(value.model || PET_AUDIO_TARIFF.model); setEnabled(value.enabled)
      setCatalogPreview(null); setUseImported(false)
      const raw = localStorage.getItem(key)
      observedPending.current = raw; setPending(raw !== null)
      if (raw) {
        let prior: { actionId?: string; expectedRevision?: number } = {}
        try { prior = JSON.parse(raw) ?? {} } catch { /* A damaged marker still blocks sending until explicitly discarded. */ }
        if (typeof prior.actionId === 'string' && typeof prior.expectedRevision === 'number' && Number.isSafeInteger(prior.expectedRevision)
          && value.actionId === prior.actionId && value.revision === prior.expectedRevision + 1 && localStorage.getItem(key) === raw) {
          localStorage.removeItem(key); setNotice('原配置操作已找到，没有重新发送。')
        } else setNotice('尚未核实本机原操作；没有重复保存。')
      }
      setPending(localStorage.getItem(key) !== null)
    } finally { if (epoch.current === run) { lock.current = false; setBusy(false) } }
  }
  useEffect(() => {
    const run = ++epoch.current
    lock.current = false; observedPending.current = null; setPending(false); setBusy(false); setApiKey(''); setSummary(null); setNotice('')
    setCatalogPreview(null); setUseImported(false)
    const invalidate = (): void => { ++epoch.current; setApiKey(''); setSummary(null); setCatalogPreview(null); setUseImported(false); setBusy(false); lock.current = false; setNotice('身份或服务已变化，请重新读取原配置。') }
    window.addEventListener('hsc-privacy-pause', invalidate)
    window.addEventListener('hsc-runtime-restarted', invalidate)
    void read(run).catch(() => { if (epoch.current === run) setNotice('无法读取原音配置，请在桌面家长中心解锁后重试。') })
    return () => { ++epoch.current; window.removeEventListener('hsc-privacy-pause', invalidate); window.removeEventListener('hsc-runtime-restarted', invalidate) }
  }, [childId])

  async function change(kind: 'save' | 'clear'): Promise<void> {
    if (lock.current || !summary || summary.managed === false || pending) return
    const run = epoch.current, actionId = crypto.randomUUID(), expectedRevision = summary.revision
    const command = { kind, actionId, expectedRevision,
      ...(kind === 'save' ? { config: { purpose: PET_AUDIO_PURPOSE, baseUrl, model, apiKey, enabled, tariffId: tariff.id },
        ...(useImported && catalogPreview ? { catalogToken: catalogPreview.token } : {}) } : {}) }
    lock.current = true; setBusy(true); setNotice('')
    // Only non-secret recovery identity goes into the existing privacy-owned journal prefix.
    const raw = JSON.stringify({ actionId, expectedRevision })
    try {
      if (localStorage.getItem(key) !== null) throw Error('原操作尚待核对。')
      localStorage.setItem(key, raw)
      if (localStorage.getItem(key) !== raw || epoch.current !== run) throw Error('本机记录未保存，未发送配置。')
      setPending(true)
      observedPending.current = raw
      const value = await requestPetAudioSettings(childId, command)
      if (epoch.current !== run) return
      if (value.purpose !== PET_AUDIO_PURPOSE || value.actionId !== actionId || value.revision !== expectedRevision + 1) throw Error('回执未确认。')
      if (localStorage.getItem(key) !== raw) throw Error('原操作记录已变化。')
      localStorage.removeItem(key); setPending(false); setSummary(value)
      setCatalogPreview(null); setUseImported(false)
      setEnabled(value.enabled); setBaseUrl(value.baseUrl); setModel(value.model || PET_AUDIO_TARIFF.model)
      setNotice(kind === 'save' ? value.enabled
        ? '原音配置已保存。请核对服务应用状态，再单独确认当前孩子的用途许可与预算。'
        : '配置已加密保存，自动原音教学保持关闭。' : '原音密钥已清除，请核对服务应用状态；孩子原有用途记录保留。')
    } catch { if (epoch.current === run) {
      try { const current = localStorage.getItem(key); setPending(current !== null); observedPending.current = current } catch { setPending(true) }
      setNotice('保存未确认或配置被拒绝。请读取原配置核对；不会自动重发。')
    } }
    finally { if (epoch.current === run) { setApiKey(''); setBusy(false); lock.current = false } }
  }

  async function importCatalog(): Promise<void> {
    if (lock.current || pending || !summary || summary.managed === false) return
    const run = epoch.current; lock.current = true; setBusy(true)
    try {
      const value = await requestPetAudioSettings(childId, { kind: 'previewCatalog' })
      if (epoch.current !== run) return
      setCatalogPreview(value.catalogPreview ?? null); setUseImported(false)
      setNotice(value.catalogPreview ? '目录已核验，配置尚未改变。请查看价格和期限，明确选用后再保存。' : '已取消导入，原配置未改变。')
    } catch (error) { if (epoch.current === run) setNotice(error instanceof Error && error.message.includes('PET_CATALOG_SIGNER_NOT_CONFIGURED')
      ? '此版本尚未配置维护者签名公钥，未导入；原有效配置保持。' : '目录签名、期限或格式未通过核验，未导入；原配置未改变。') }
    finally { if (epoch.current === run) { lock.current = false; setBusy(false) } }
  }

  async function apply(): Promise<void> {
    if (!summary?.actionId || summary.managed === false || lock.current) return
    const run = epoch.current
    lock.current = true; setBusy(true)
    try {
      const value = await requestPetAudioSettings(childId, { kind: 'apply', expectedRevision: summary.revision, actionId: summary.actionId })
      if (run !== epoch.current) return
      setSummary(value); setNotice(value.runtimeApplied ? '原配置已应用，请刷新用途许可状态；没有重新保存或重复扣额。' : '原配置尚未应用，请核对本地服务状态。')
    } catch { if (run === epoch.current) setNotice('服务应用未确认。请重新解锁家长中心并读取原配置；不会自动重复保存。') }
    finally { if (run === epoch.current) { lock.current = false; setBusy(false) } }
  }

  return <section aria-label="原音模型设置">
    <style>{`.pet-audio-teaching label.pet-config-consent{display:grid;grid-template-columns:18px minmax(0,1fr);gap:8px;max-width:360px;align-items:start}.pet-audio-teaching .pet-config-consent input{width:18px;height:18px;min-height:0;margin:2px 0;padding:0}`}</style>
    <h4>原音模型设置</h4>
    {summary?.managed === false && <p>当前连接外部启动的后台，桌面不能更改其配置。原授权路径保持不变；桌面托管模式下才可保存并应用。</p>}
    <p>{!summary ? '原音配置尚待读取核对。' : summary.enabled ? summary.runtimeApplied ? '已核验原音模型配置已应用，仍需当前孩子的用途许可。' : '原音配置已保存，服务应用状态待核对。'
      : summary?.state === 'tariff_unavailable' ? '费率或地域核验已失效，自动发送已关闭，请更新费率目录后重新确认。'
        : summary?.configured ? '密钥已保存，自动原音教学未启用。' : '尚未保存原音专属模型配置。'}</p>
    {!useImported && !summary?.catalog && summary?.tariffId && summary.tariffId !== tariff.id && <p role="alert">当前已存目录无法核验，下方为软件内置参考目录，不能据此发送；已存型号为 {summary.model}。</p>}
    <p>北京区固定模型：{tariff.model}。人民币原价每百万token：文本输入{tariff.textInputCnyPerMillion}元、音频输入{tariff.audioInputCnyPerMillion}元、文本输出{tariff.textOutputWithAudioCnyPerMillion}元。音频每秒{tariff.audioTokensPerSecond} token，不足1秒按1秒；每次发送按向上取整的上限预留，未知结果仍保留占用。</p>
    <p>费率复核期限：{tariff.reviewBefore.replace('T', ' ').replace('Z', ' UTC')}。到期后自动发送会暂停；需要复核目录并重新确认配置与当前孩子的用途许可。不使用普通对话套餐或语音识别授权。保存配置将重启本地服务，并要求重新确认孩子的用途许可。</p>
    <button type="button" disabled={busy || pending || !summary || summary.managed === false} onClick={() => { void importCatalog() }}>导入已核型号与费率目录</button>
    {catalogPreview && <div role="region" aria-label="待选用目录">
      <p>项目维护者签发：{catalogPreview.issuer}；目录版本：{catalogPreview.revision}。这是维护者对官方资料的核验记录，不是阿里官方签名。</p>
      <p>新型号：{catalogPreview.tariff.model}；北京区；文本输入/音频输入/文本输出每百万token：{catalogPreview.tariff.textInputCnyPerMillion}/{catalogPreview.tariff.audioInputCnyPerMillion}/{catalogPreview.tariff.textOutputWithAudioCnyPerMillion}元；复核期限：{catalogPreview.tariff.reviewBefore}。</p>
      {catalogPreview.sourceUrls.map(url => <p key={url}>{url}</p>)}
      <button type="button" disabled={busy || pending} onClick={() => { setUseImported(true); setModel(catalogPreview.tariff.model); setEnabled(false); setNotice('已选用，尚未保存。请确认北京区地址、费用和启用选项；保存后须重新确认当前孩子的用途许可与预算。') }}>选用此目录</button>
    </div>}
    {summary?.state === 'tariff_unavailable' && <p role="alert">已选目录过期或不再适用，自动发送保持关闭。请导入维护者新核验的目录；原密钥和历史费用记录保留。</p>}
    {summary?.state === 'catalog_recovery_required' && <p role="alert">目录与配置尚未共同确认，自动发送保持关闭。请先重新读取并导入当前已核目录；若本机控制记录仍无法核实，请保留原记录联系维护者，勿重复保存。原密钥未清除，旧目录不能回退启用。</p>}
    <div className="audio-controls">
      <label>原音服务地址<input aria-label="原音服务地址" value={baseUrl} disabled={busy || pending || !summary || summary.managed === false} onChange={event => setBaseUrl(event.target.value)} /></label>
      <label>原音模型名称<input aria-label="原音模型名称" value={model} disabled={busy || pending || !summary || summary.managed === false} onChange={event => setModel(event.target.value)} /></label>
      {summary?.configured && summary.model !== tariff.model && <div>
        <p>已保存的型号不在当前核验目录中。可以先选用当前支持的型号，再显式保存并重新确认用途许可与预算。</p>
        <button type="button" disabled={busy || pending || summary.managed === false} onClick={() => {
          setModel(tariff.model)
          setNotice('已选用当前支持的型号，但尚未保存。请显式保存配置，再重新确认当前孩子的用途许可与预算。')
        }}>选用当前支持的型号</button>
      </div>}
      <label>专属 API Key<input aria-label="原音专属密钥" type="password" autoComplete="off" value={apiKey} disabled={busy || pending || !summary || summary.managed === false} onChange={event => setApiKey(event.target.value)} /></label>
      <label className="pet-config-consent"><input type="checkbox" aria-label="启用已核验原音配置" checked={enabled} disabled={busy || pending || !summary || summary.managed === false || !currentPetAudioTariff(tariff.id, baseUrl, model, Date.now(), tariff)} onChange={event => setEnabled(event.target.checked)} /><span>确认此固定模型和费率，仅启用原音练习建议配置</span></label>
      <button disabled={busy || pending || !summary || summary.managed === false || !model.trim()} onClick={() => { void change('save') }}><Save />保存原音配置</button>
      <button disabled={busy || pending || !summary?.configured || summary.managed === false} onClick={() => { void change('clear') }}><Trash2 />清除原音密钥</button>
      <button disabled={busy} onClick={() => { const run = epoch.current; void read(run).catch(() => { if (run === epoch.current) setNotice('读取未完成，原记录未改变。') }) }}><RefreshCw />读取原配置</button>
      <button disabled={busy} onClick={() => { resetRuntimeCache(); window.dispatchEvent(new Event('hsc-runtime-restarted')) }}><RefreshCw />重新解锁家长中心</button>
      {summary?.actionId && summary.runtimeApplied === false && summary.managed !== false && <button disabled={busy || pending} onClick={() => { void apply() }}><RefreshCw />重新应用原配置</button>}
    </div>
    {pending && <button disabled={busy} onClick={() => {
      try {
        const raw = observedPending.current
        if (raw !== null && localStorage.getItem(key) === raw) localStorage.removeItem(key)
        const current = localStorage.getItem(key)
        setPending(current !== null); observedPending.current = current
        setNotice(current !== null ? '本机待核对记录已变化，未清除新记录；请重新读取核对。' : '仅放弃本机待核对标记，服务器配置未更改；保存前请重新读取。'); setSummary(null)
      } catch { setNotice('本机记录无法读取，未清除或重新发送。') }
    }}>放弃本机配置标记</button>}
    {notice && <p role="status">{notice}</p>}
  </section>
}
