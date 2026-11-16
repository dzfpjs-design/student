import { useEffect, useState } from 'react'
import { localApi } from './api'

type Status = Awaited<ReturnType<typeof localApi.pilotDiagnosticStatus>>

/** Family-wide diagnostic consent is separate from each child's learning report. */
export function ParentDiagnosticEmail(): React.JSX.Element {
  const [value, setValue] = useState<Status | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [editing, setEditing] = useState(false)
  const [recipient, setRecipient] = useState('')
  const [confirmation, setConfirmation] = useState('')
  async function refresh(): Promise<void> {
    try { setValue(await localApi.pilotDiagnosticStatus()); setError('') }
    catch { setValue(null); setError('暂时无法读取运行日志邮件设置，请重试。') }
  }
  useEffect(() => { void refresh() }, [])
  async function change(consented: boolean): Promise<void> {
    if (!value || busy || editing) return
    setBusy(true)
    try { await localApi.setPilotDiagnosticConsent(value.recipient, consented); await refresh() }
    catch { setError('设置未确认，请检查家长身份和本机服务后重试。') }
    finally { setBusy(false) }
  }
  async function configure(): Promise<void> {
    if (busy) return
    const address = recipient.trim().toLowerCase(), confirmed = confirmation.trim().toLowerCase()
    if (!/^[^\s@]{1,64}@[^\s@.]+(?:\.[^\s@.]+)+$/.test(address) || address.length > 320) {
      setError('请输入有效的收件邮箱。'); return
    }
    if (address !== confirmed) { setError('两次输入的邮箱不一致，请检查。'); return }
    setBusy(true); setError('')
    try {
      await localApi.setPilotDiagnosticRecipient(address, confirmed)
      setEditing(false); setRecipient(''); setConfirmation('')
      await refresh()
    } catch { setError('邮箱未能保存，请检查家长身份和本机服务后重试。') }
    finally { setBusy(false) }
  }
  const latest = value?.latest
  return <section className="parent-diagnostic-email" aria-label="家庭运行日志邮件"><h2>家庭运行日志邮件</h2>
    <p>家庭运行日志按这里的授权发送。学习日报收件人请在对应设置中核对；电脑及客户端需运行并联网。发送结果未知时系统会暂停重发并提示核对。</p>
    {error && <p role="alert">{error}</p>}
    {!value ? <div className="parent-diagnostic-email-actions"><button className="secondary-button" disabled={busy} onClick={() => void refresh()}>重新读取</button></div> : <>
      <p>收件邮箱：{value.recipient || '尚未配置'} · {value.consent === 'consented' ? '已授权' : value.consent === 'revoked' ? '已停止' : '尚未授权'}{!value.scheduler_enabled && ' · 定时发送已关闭'}</p>
      {!value.recipient && <p role="alert">尚未找到唯一的已授权收件邮箱，运行日志邮件已暂停。请设置并授权收件邮箱。</p>}
      {value.recipient_configuration_conflict && <p role="alert">本机环境配置与保存的日志邮箱不一致，发送已暂停。请统一这两项配置并重启客户端。</p>}
      {value.blocked_children.length > 0 && <p role="alert">该邮箱对{value.blocked_children.join('、')}的报告授权已撤销，家庭邮件包含全家信息，因此暂不能发送。请先到对应孩子的报告收件人设置核对。</p>}
      <p>最近发送记录：{latest ? `${latest.date} · ${{sent:'邮件服务器已接收', failed:'发送失败，将检查重试', processing:'正在处理', pending:'等待处理', unknown:'结果未知，未自动重发'}[latest.status] ?? '状态待核对'}` : '尚无记录'}</p>
      <div className="parent-diagnostic-email-actions">
        {value.recipient_editable !== false && !editing && <button className="secondary-button" disabled={busy} onClick={() => { setRecipient(''); setConfirmation(''); setError(''); setEditing(true) }}>{value.recipient ? '更换收件邮箱' : '设置收件邮箱'}</button>}
        {value.recipient_editable === false && <p>收件邮箱由本机环境配置管理。</p>}
        {value.recipient && !editing && !value.recipient_configuration_conflict && value.blocked_children.length === 0 && (value.consent === 'consented'
          ? <button className="secondary-button" disabled={busy} onClick={() => void change(false)}>停止发送运行日志</button>
          : <button className="secondary-button" disabled={busy} onClick={() => void change(true)}>授权发送试用摘要和运行日志</button>)}
        <button className="secondary-button" disabled={busy} onClick={() => void refresh()}>刷新发送状态</button>
      </div>
      {editing && <div className="parent-diagnostic-email-recipient">
        <p>收件邮箱用于家庭运行日志。更换邮箱会停止向旧邮箱发送，新邮箱需要单独授权；再次保存相同邮箱会保留原授权。</p>
        <label>收件邮箱<input type="email" autoComplete="off" value={recipient} onChange={event => setRecipient(event.target.value)} /></label>
        <label>再次输入邮箱<input type="email" autoComplete="off" value={confirmation} onChange={event => setConfirmation(event.target.value)} /></label>
        <div className="parent-diagnostic-email-actions">
          <button className="primary-button" disabled={busy} onClick={() => void configure()}>确认保存邮箱</button>
          <button className="secondary-button" disabled={busy} onClick={() => { setEditing(false); setRecipient(''); setConfirmation(''); setError('') }}>取消</button>
        </div>
      </div>}
    </>}
  </section>
}
