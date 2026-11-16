import { useEffect, useMemo, useRef, useState } from 'react'
import { CheckCircle2, LockKeyhole, MailCheck, ServerCog } from 'lucide-react'
import { ApiError, localApi } from './api'
import type { ChildProfile, InstallationSetupStatus } from './types'
import { canReuseSetupSmtpSecret, householdSetupDefaults, type HouseholdSetupDefaults } from './householdSetupDefaults'

type EmailTest = Awaited<ReturnType<typeof localApi.reportDeliveryChannelTests>>[number]
type EmailTests = Record<string, EmailTest | null>

type Props = {
  children: ChildProfile[]
  initialStatus: InstallationSetupStatus
  initialSmtpSecretConfigured?: boolean
  onComplete: () => Promise<void>
}

export function InstallationSetupWizard({ children, initialStatus, initialSmtpSecretConfigured = false, onComplete }: Props): React.JSX.Element {
  const [status, setStatus] = useState(initialStatus)
  const [smtpSecretConfigured, setSmtpSecretConfigured] = useState(initialSmtpSecretConfigured)
  const [pin, setPin] = useState('')
  const [confirmPin, setConfirmPin] = useState('')
  const [authenticated, setAuthenticated] = useState(false)
  const [editingSmtp, setEditingSmtp] = useState(false)
  const [host, setHost] = useState('')
  const [port, setPort] = useState('465')
  const [security, setSecurity] = useState<'ssl' | 'starttls' | 'none'>('ssl')
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [fromAddress, setFromAddress] = useState('')
  const [recipient, setRecipient] = useState('')
  const [defaults, setDefaults] = useState<HouseholdSetupDefaults | null>(null)
  const smtpTouched = useRef(false), recipientTouched = useRef(false), defaultsVersion = useRef(0)
  useEffect(() => {
    if (!authenticated) return
    const bridge = window.desktop as (typeof window.desktop & { getHouseholdSetupDefaults?: () => Promise<unknown> }) | undefined
    if (!bridge?.getHouseholdSetupDefaults) return
    let current = true
    const version = defaultsVersion.current
    void bridge.getHouseholdSetupDefaults().then(raw => {
      const preset = householdSetupDefaults(raw)
      if (!current || version !== defaultsVersion.current || !preset) return
      setDefaults(preset)
      if (!smtpTouched.current) {
        setHost(preset.smtp.host); setPort(String(preset.smtp.port)); setUsername(preset.smtp.username)
        setFromAddress(preset.smtp.from_address)
        setSecurity(preset.smtp.use_ssl ? 'ssl' : preset.smtp.starttls ? 'starttls' : 'none')
      }
      if (!recipientTouched.current) setRecipient(preset.recipient)
    }).catch(() => { /* Unavailable defaults leave the original explicit setup intact. */ })
    return () => { current = false }
  }, [authenticated])
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState('')
  const [error, setError] = useState('')

  const [emailTests, setEmailTests] = useState<EmailTests>({})
  const [testsChecked, setTestsChecked] = useState(false)
  const [testReadError, setTestReadError] = useState(false)
  const [pendingReissue, setPendingReissue] = useState<{ childId: string; previousId: string } | null>(null)
  const [pendingReauthorized, setPendingReauthorized] = useState<{ childId: string; previousId: string; recipientId: string; address: string } | null>(null)

  const needsRecipients = status.active_child_count > 0
  const step = useMemo(() => {
    if (!authenticated) return 'pin'
    if (editingSmtp || !status.smtp_configured || !smtpSecretConfigured) return 'smtp'
    if (status.children_missing_email_recipient > 0) return 'recipient'
    if (status.children_missing_email_test > 0) return 'test'
    return 'complete'
  }, [authenticated, editingSmtp, smtpSecretConfigured, status])

  const readEmailTests = async (): Promise<EmailTests> => Object.fromEntries(await Promise.all(children.map(async (child) => {
    const items = (await localApi.reportDeliveryChannelTests(child.child_id)).filter((item) => item.channel === 'email')
    if (items.length > 1) throw new Error('邮件测试记录尚未核对，未发送新邮件。')
    return [child.child_id, items[0] ?? null] as const
  })))

  useEffect(() => {
    if (!authenticated || !['test', 'complete'].includes(step)) return
    let current = true
    setTestsChecked(false); setTestReadError(false); setPendingReissue(null)
    void readEmailTests().then((next) => {
      if (current) { setEmailTests(next); setTestsChecked(true) }
    }).catch(() => { if (current) setTestReadError(true) })
    return () => { current = false }
  }, [authenticated, step, children]) // The status read never sends a test.

  const uncertainTests = children.filter((child) => ['processing', 'unknown'].includes(emailTests[child.child_id]?.status ?? ''))
  const testsBlocked = !testsChecked || testReadError || uncertainTests.length > 0
  const actionKey = (childId: string, previousId?: string): string => `hsc.installation-email-test.v1:${encodeURIComponent(childId)}:${previousId ? `reissue:${encodeURIComponent(previousId)}` : 'original'}`
  const actionId = (childId: string, previousId?: string): string => {
    const key = actionKey(childId, previousId)
    const existing = window.localStorage.getItem(key)
    if (existing) return existing
    const created = `install-smtp-test:${crypto.randomUUID()}`
    window.localStorage.setItem(key, created) // Retry an unacknowledged dispatch with the same action.
    return created
  }

  const run = async (task: () => Promise<void>): Promise<void> => {
    setBusy(true); setError(''); setMessage('')
    try { await task() }
    catch (cause) { setError(cause instanceof ApiError ? cause.message : cause instanceof Error ? cause.message : '初始化未完成，请重试。') }
    finally { setBusy(false) }
  }

  const refresh = async (): Promise<InstallationSetupStatus> => {
    const next = await localApi.installationSetupStatus()
    setStatus(next)
    const secret = await window.desktop?.getReportDeliverySecretStatus?.().catch(() => null) ?? null
    setSmtpSecretConfigured(Boolean(secret?.smtpPasswordConfigured))
    return next
  }

  const unlock = (): Promise<void> => run(async () => {
    if (!/^\d{4,8}$/.test(pin)) throw new Error('家长 PIN 必须为 4 至 8 位数字。')
    if (!status.parent_pin_configured && pin !== confirmPin) throw new Error('两次输入的家长 PIN 不一致。')
    await localApi.parentUnlock(pin, !status.parent_pin_configured)
    setPin(''); setConfirmPin(''); setAuthenticated(true)
    await refresh()
    setMessage(status.parent_pin_configured ? '家长身份已确认。' : '家长 PIN 已保存。')
  })

  const saveSmtp = (): Promise<void> => run(async () => {
    if (!window.desktop?.saveReportDeliverySecrets) throw new Error('请在安装后的桌面应用中完成 SMTP 初始化。')
    let reuseSecret = false
    if (!password) {
      const bridge = window.desktop as (typeof window.desktop & { getHouseholdSetupDefaults?: () => Promise<unknown> }) | undefined
      const [raw, protectedStatus] = await Promise.all([
        bridge?.getHouseholdSetupDefaults?.().catch(() => null) ?? null,
        window.desktop?.getReportDeliverySecretStatus?.().catch(() => null) ?? null,
      ])
      const current = householdSetupDefaults(raw)
      const protectedPassword = protectedStatus?.smtpPasswordConfigured === true
      // Cached prefill is never authority after a user changed the protected file.
      setDefaults(current); setSmtpSecretConfigured(protectedPassword)
      reuseSecret = canReuseSetupSmtpSecret(current, protectedPassword, { host, port, username, fromAddress, security })
    }
    if (!host.trim() || !/^\d+$/.test(port) || !fromAddress.includes('@') || !username.trim() || (!password && !reuseSecret)) {
      throw new Error('请完整填写 SMTP 主机、端口、用户名、应用专用密码和发件地址。')
    }
    if (password) {
      await window.desktop.saveReportDeliverySecrets({ smtpPassword: password })
      defaultsVersion.current += 1
      setDefaults(null)
      setSmtpSecretConfigured(true)
    }
    await localApi.saveReportDeliveryChannel('email', {
      host: host.trim(), port: Number(port), username: username.trim(), ...(password ? { password } : {}),
      from_address: fromAddress.trim(), use_ssl: security === 'ssl', starttls: security === 'starttls',
      allow_external_delivery: true,
    })
    setPassword('')
    await refresh()
    setEditingSmtp(false)
    setMessage('SMTP 已保存到本机安全存储。')
  })

  const saveRecipients = (): Promise<void> => run(async () => {
    if (!recipient.includes('@')) throw new Error('请输入有效的家长收件邮箱。')
    for (const child of children) {
      const existing = await localApi.reportRecipients(child.child_id)
      const matched = existing.items.some((item) => item.channel === 'email' && item.address.toLowerCase() === recipient.trim().toLowerCase() && item.consent_status === 'consented')
      if (!matched) await localApi.addReportRecipient({ child_id: child.child_id, channel: 'email', address: recipient.trim(), label: `${child.nickname}家长`, consented: true })
    }
    await refresh()
    setMessage('每个孩子的日报收件人已保存。')
  })

  const refreshTests = async (): Promise<void> => {
    setTestsChecked(false); setTestReadError(false)
    try {
      const next = await readEmailTests()
      setEmailTests(next); setTestsChecked(true)
      await refresh()
    } catch (cause) { setTestReadError(true); throw cause }
  }

  const keepUnknown = (childId: string, previousId: string): Promise<void> => run(async () => {
    const current = (await readEmailTests())[childId]
    if (current?.status !== 'unknown' || current.client_action_id !== previousId) throw new Error('原测试状态已变化，请先核对测试状态。')
    await localApi.keepUnknownReportDeliveryChannelTest(childId, 'email', previousId)
    await refreshTests()
    setMessage('已保留原测试，不会重发；结果未知不能视为验证通过。')
  })

  const reissueUnknown = (): Promise<void> => run(async () => {
    if (!pendingReissue) return
    const { childId, previousId } = pendingReissue
    const current = (await readEmailTests())[childId]
    if (current?.status !== 'unknown' || current.client_action_id !== previousId) throw new Error('原测试状态已变化，请先核对测试状态。')
    const result = await localApi.reissueReportDeliveryChannelTest(childId, 'email', previousId, actionId(childId, previousId))
    const originalKey = actionKey(childId)
    if (window.localStorage.getItem(originalKey) === previousId) window.localStorage.removeItem(originalKey)
    window.localStorage.removeItem(actionKey(childId, previousId))
    setPendingReissue(null)
    await refreshTests()
    if (result.historical_only === true || result.current_verified === false) {
      setMessage(result.message)
      return
    }
    if (result.status !== 'passed') throw new Error('新的测试尚未通过，请核对当前结果；系统不会自动再发。')
    setMessage('新的固定测试邮件已被 SMTP 接受，请继续核对完整设置。')
  })

  const prepareReauthorized = (childId: string, previousId: string): Promise<void> => run(async () => {
    const current = (await readEmailTests())[childId]
    if (current?.status !== 'unknown' || current.client_action_id !== previousId || !current.recipient_id) throw new Error('原测试状态或收件人无法核对，未发送邮件。')
    const { items } = await localApi.reportRecipients(childId)
    const recipient = items.find(item => item.recipient_id === current.recipient_id && item.channel === 'email' && item.consent_status === 'consented')
    if (!recipient) throw new Error('原收件邮箱当前未授权，请先恢复该邮箱授权。')
    setPendingReauthorized({ childId, previousId, recipientId: recipient.recipient_id, address: recipient.address })
  })

  const reissueReauthorized = (): Promise<void> => run(async () => {
    if (!pendingReauthorized) return
    const { childId, previousId, recipientId, address } = pendingReauthorized
    const current = (await readEmailTests())[childId]
    if (current?.status !== 'unknown' || current.client_action_id !== previousId || current.recipient_id !== recipientId) throw new Error('原测试状态已变化，请重新核对。')
    const result = await localApi.reissueReauthorizedEmailTest(childId, previousId, actionId(childId, previousId), recipientId, address)
    if (result.historical_only === true || result.current_verified === false) { await refreshTests(); setMessage(result.message); return }
    if (result.status !== 'passed') throw new Error(result.message)
    window.localStorage.removeItem(actionKey(childId, previousId))
    setPendingReauthorized(null); await refreshTests(); setMessage('固定测试邮件已被 SMTP 接受，请继续核对完整设置。')
  })

  const verifyDelivery = (): Promise<void> => run(async () => {
    const checked = await readEmailTests()
    setEmailTests(checked); setTestsChecked(true); setTestReadError(false)
    if (children.some((child) => ['processing', 'unknown'].includes(checked[child.child_id]?.status ?? ''))) {
      throw new Error('有邮件测试仍在处理或结果未知，请先核对原测试；未发送新邮件。')
    }
    for (const child of children) {
      if (checked[child.child_id]?.status === 'passed') continue
      const dispatched = actionId(child.child_id)
      const result = await localApi.testReportDeliveryChannel(child.child_id, 'email', dispatched)
      window.localStorage.removeItem(actionKey(child.child_id))
      if (result.historical_only === true || result.current_verified === false) {
        await refreshTests()
        setMessage(result.message)
        return
      }
      setEmailTests((items) => ({ ...items, [child.child_id]: { ...result, client_action_id: result.client_action_id ?? dispatched } }))
      if (result.status !== 'passed') throw new Error(`${child.nickname}的邮件测试未通过：${result.message}`)
    }
    const next = await refresh()
    const secret = await window.desktop?.getReportDeliverySecretStatus?.()
    if (!next.complete || !secret?.smtpPasswordConfigured) throw new Error('SMTP 配置尚未形成可恢复的完整状态。')
    setMessage('初始化已验证，正在进入家庭学习伙伴。')
    await onComplete()
  })

  return <main className="installation-setup-shell">
    <section className="installation-setup-panel" aria-label="安装初始化">
      <header><p className="eyebrow">首次使用设置</p><h1>先完成家长与日报配置</h1><p>这些设置完成并验证后才会开放孩子端；应用专用密码不会写入学习数据库或日志。</p></header>
      <ol className="installation-progress">
        <li className={status.parent_pin_configured ? 'done' : ''} aria-current={step === 'pin' ? 'step' : undefined}><LockKeyhole size={18} />家长 PIN</li>
        <li className={status.smtp_configured ? 'done' : ''} aria-current={step === 'smtp' ? 'step' : undefined}><ServerCog size={18} />SMTP</li>
        <li className={status.children_missing_email_recipient === 0 ? 'done' : ''} aria-current={step === 'recipient' ? 'step' : undefined}><MailCheck size={18} />收件人</li>
        <li className={status.complete ? 'done' : ''} aria-current={step === 'test' || step === 'complete' ? 'step' : undefined}><CheckCircle2 size={18} />实发验证</li>
      </ol>

      {step === 'pin' && <fieldset><legend>{status.parent_pin_configured ? '输入家长 PIN' : '设置家长 PIN'}</legend>
        <label>家长 PIN<input aria-label="初始化家长PIN" type="password" inputMode="numeric" value={pin} onChange={(event) => setPin(event.target.value.replace(/\D/g, '').slice(0, 8))} /></label>
        {!status.parent_pin_configured && <label>再次输入<input aria-label="再次输入家长PIN" type="password" inputMode="numeric" value={confirmPin} onChange={(event) => setConfirmPin(event.target.value.replace(/\D/g, '').slice(0, 8))} /></label>}
        <button className="primary-button" disabled={busy} onClick={() => void unlock()}>{status.parent_pin_configured ? '确认身份' : '保存 PIN'}</button>
      </fieldset>}

      {step === 'smtp' && <fieldset><legend>配置日报 SMTP</legend>
        <div className="delivery-settings-grid">
          <label>SMTP 主机<input aria-label="初始化SMTP主机" value={host} onChange={(event) => { smtpTouched.current = true; setHost(event.target.value) } } /></label>
          <label>端口<input aria-label="初始化SMTP端口" value={port} onChange={(event) => { smtpTouched.current = true; setPort(event.target.value) } } /></label>
          <label>加密<select aria-label="初始化SMTP加密" value={security} onChange={(event) => { smtpTouched.current = true; const value = event.target.value as 'ssl' | 'starttls' | 'none'; setSecurity(value); if (value === 'ssl') setPort('465'); else if (value === 'starttls') setPort('587') }}><option value="ssl">SSL/TLS</option><option value="starttls">STARTTLS</option><option value="none">无加密（仅本机测试）</option></select></label>
          <label>用户名<input aria-label="初始化SMTP用户名" value={username} onChange={(event) => { smtpTouched.current = true; setUsername(event.target.value) } } /></label>
          <label>应用专用密码<input aria-label="初始化SMTP密码" type="password" value={password} onChange={(event) => { smtpTouched.current = true; setPassword(event.target.value) } } /></label>
          <label>发件地址<input aria-label="初始化SMTP发件地址" value={fromAddress} onChange={(event) => { smtpTouched.current = true; setFromAddress(event.target.value) } } /></label>
        </div>
        {canReuseSetupSmtpSecret(defaults, smtpSecretConfigured, { host, port, username, fromAddress, security }) && <p>本机已安全保存这组发件配置的授权码，无需再次输入；更改主机、账号或加密方式时必须提供新的授权码。</p>}
        <div className="installation-setup-actions">
          <button className="primary-button" disabled={busy} onClick={() => void saveSmtp()}>保存 SMTP</button>
          {editingSmtp && <button className="secondary-button" disabled={busy} onClick={() => { setPassword(''); setEditingSmtp(false); setError(''); setMessage('') }}>取消修改 SMTP</button>}
        </div>
      </fieldset>}

      {step === 'recipient' && <fieldset><legend>设置每日报告收件人</legend><p>同一个家长邮箱将分别绑定到两个孩子，报告和学习档案仍严格隔离。</p>
        <label>家长邮箱<input aria-label="初始化收件邮箱" type="email" value={recipient} onChange={(event) => { recipientTouched.current = true; setRecipient(event.target.value) }} /></label>
        <button className="primary-button" disabled={busy || !needsRecipients} onClick={() => void saveRecipients()}>保存收件人</button>
      </fieldset>}

      {step === 'test' && <fieldset><legend>发送固定测试邮件</legend><p>测试邮件不含孩子姓名、题目、成绩或家庭影像。只有 SMTP 服务实际接受邮件后初始化才通过。</p>
        <button className="primary-button" disabled={busy || testsBlocked} onClick={() => void verifyDelivery()}>发送并验证</button>
      </fieldset>}

      {step === 'complete' && <button className="primary-button" disabled={busy || testsBlocked} onClick={() => void verifyDelivery()}>完成初始化</button>}
      {authenticated && ['test', 'complete'].includes(step) && <section className="installation-test-status" aria-label="邮件测试状态">
        {!testsChecked && !testReadError && <p role="status">正在核对原邮件测试记录；尚未发送新邮件。</p>}
        {testReadError && <p role="alert">测试状态暂不可用，未发送新邮件。请核对状态后再继续。</p>}
        {uncertainTests.map((child) => {
          const test = emailTests[child.child_id]!
          return <div key={child.child_id}>
            <p>{child.nickname}：{test.status === 'processing' ? '测试仍在处理中，核对状态不会再次发送。' : '上次测试结果未知，可能已发送。系统不会自动重发，也不能把未知结果视为通过。'}</p>
            {test.status === 'unknown' && test.client_action_id && <>
              {test.kept_without_resend && <p>已选择暂不重发；你仍可明确决定发送新的固定测试邮件。</p>}
              <div className="installation-setup-actions">
                {!test.kept_without_resend && <button className="secondary-button" disabled={busy} onClick={() => void keepUnknown(child.child_id, test.client_action_id!)}>暂不重发{child.nickname}的测试</button>}
                <button className="secondary-button" disabled={busy} onClick={() => { setPendingReissue({ childId: child.child_id, previousId: test.client_action_id! }); setError('') }}>重新测试{child.nickname}的邮箱</button>
                <button className="secondary-button" disabled={busy} onClick={() => void prepareReauthorized(child.child_id, test.client_action_id!)}>核对重新授权的{child.nickname}邮箱</button>
              </div>
            </>}
            {test.status === 'unknown' && !test.client_action_id && <p role="alert">原测试记录不完整，请先核对状态；未开放重新发送。</p>}
          </div>
        })}
        <button className="secondary-button" disabled={busy} onClick={() => void run(refreshTests)}>核对测试状态</button>
        {pendingReissue && <div role="alert"><p>原测试邮件可能已经送达。确认后将另外发送一封固定测试邮件，不含孩子学习数据。</p>
          <div className="installation-setup-actions">
            <button className="secondary-button" disabled={busy} onClick={() => setPendingReissue(null)}>取消重新测试</button>
            <button className="primary-button" disabled={busy} onClick={() => void reissueUnknown()}>确认重新发送固定测试邮件</button>
          </div>
        </div>}
        {pendingReauthorized && <div role="alert"><p>原测试结果未知，邮件可能已经送达。确认后将向当前重新授权的 {pendingReauthorized.address} 再发送一封不含孩子数据的固定测试邮件。</p>
          <div className="installation-setup-actions">
            <button className="secondary-button" disabled={busy} onClick={() => setPendingReauthorized(null)}>取消当前邮箱重测</button>
            <button className="primary-button" disabled={busy} onClick={() => void reissueReauthorized()}>确认当前邮箱并发送新测试</button>
          </div>
        </div>}
      </section>}
      {authenticated && step !== 'smtp' && <button className="secondary-button" disabled={busy} onClick={() => { setEditingSmtp(true); setError(''); setMessage('') }}>修改 SMTP 配置</button>}
      {message && <p role="status" className="setup-message">{message}</p>}
      {error && <p role="alert" className="parent-error">{error}</p>}
    </section>
  </main>
}
