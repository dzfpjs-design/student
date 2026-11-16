import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { InstallationSetupWizard } from './InstallationSetupWizard'
import { localApi } from './api'
import type { InstallationSetupStatus } from './types'

const children = [
  { child_id: 'child-1', nickname: '小航', grade: '初三', curriculum_region: '江苏南京', status: 'active' as const },
  { child_id: 'child-2', nickname: '小语', grade: '四年级', curriculum_region: '江苏南京', status: 'active' as const },
]

const status = (overrides: Partial<InstallationSetupStatus> = {}): InstallationSetupStatus => ({
  schema: 'hsc.installation-setup.v1', complete: false, parent_pin_configured: false,
  smtp_configured: false, smtp_secret_check_required: false, active_child_count: 2,
  children_missing_email_recipient: 2, children_missing_email_test: 2, next_step: 'parent_pin',
  ...overrides,
})

describe('InstallationSetupWizard', () => {
  beforeEach(() => {
    window.localStorage.clear()
    window.desktop = {
      saveReportDeliverySecrets: vi.fn().mockResolvedValue({ smtpPasswordConfigured: true, wecomWebhookConfigured: false }),
      getReportDeliverySecretStatus: vi.fn().mockResolvedValue({ state: 'ready', smtpPasswordConfigured: true, wecomWebhookConfigured: false }),
    } as never
  })

  afterEach(() => { cleanup(); vi.restoreAllMocks(); delete window.desktop })

  it('shows the reauthorized address and requires explicit confirmation before creating a new email test', async () => {
    const current = status({parent_pin_configured:true,smtp_configured:true,children_missing_email_recipient:0,children_missing_email_test:1,next_step:'email_test'})
    vi.spyOn(localApi,'parentUnlock').mockResolvedValue()
    vi.spyOn(localApi,'installationSetupStatus').mockResolvedValue(current)
    vi.spyOn(localApi,'reportDeliveryChannelTests').mockImplementation(async child => [{channel:'email',status:child==='child-1'?'unknown':'passed',message:'原测试结果未知',tested_at:null,...(child==='child-1'?{client_action_id:'unknown-001',recipient_id:'recipient-1'}:{})}])
    vi.spyOn(localApi,'reportRecipients').mockResolvedValue({items:[{recipient_id:'recipient-1',child_id:'child-1',channel:'email',address:'parent@local.test',label:'家长',consent_status:'consented',consented_at:'2026-01-01T00:00:00Z'}],preferences:{quiet_start:'21:00',quiet_end:'07:00',retention_days:90,redaction_level:'standard'}})
    const send=vi.spyOn(localApi,'reissueReauthorizedEmailTest').mockResolvedValue({channel:'email',status:'passed',message:'已接受',tested_at:null})
    render(<InstallationSetupWizard children={children} initialStatus={current} initialSmtpSecretConfigured onComplete={vi.fn()} />)
    fireEvent.change(screen.getByLabelText('初始化家长PIN'),{target:{value:'2468'}})
    fireEvent.click(screen.getByRole('button',{name:'确认身份'}))
    fireEvent.click(await screen.findByRole('button',{name:'核对重新授权的小航邮箱'}))
    expect(await screen.findByText(/parent@local.test/)).toHaveTextContent('邮件可能已经送达')
    expect(send).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button',{name:'取消当前邮箱重测'}))
    expect(send).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button',{name:'核对重新授权的小航邮箱'}))
    fireEvent.click(await screen.findByRole('button',{name:'确认当前邮箱并发送新测试'}))
    await waitFor(()=>expect(send).toHaveBeenCalledOnce())
    expect(send).toHaveBeenCalledWith('child-1','unknown-001',expect.any(String),'recipient-1','parent@local.test')
  })

  it('does not finish setup when a replayed old test passed but current verification is missing', async () => {
    const current = status({parent_pin_configured:true,smtp_configured:true,children_missing_email_recipient:0,children_missing_email_test:1,next_step:'email_test'})
    vi.spyOn(localApi,'installationSetupStatus').mockResolvedValue(current)
    vi.spyOn(localApi,'parentUnlock').mockResolvedValue()
    const reads = vi.spyOn(localApi,'reportDeliveryChannelTests').mockResolvedValue([{channel:'email',status:'blocked',message:'当前配置尚未验证',tested_at:null}])
    const send = vi.spyOn(localApi,'testReportDeliveryChannel').mockResolvedValue({channel:'email',status:'passed',message:'这是历史测试，当前邮箱尚未验证。',tested_at:'2026-01-01',historical_only:true,current_verified:false} as never)
    const onComplete = vi.fn()
    render(<InstallationSetupWizard children={children} initialStatus={current} initialSmtpSecretConfigured onComplete={onComplete} />)
    fireEvent.change(screen.getByLabelText('初始化家长PIN'),{target:{value:'2468'}})
    fireEvent.click(screen.getByRole('button',{name:'确认身份'}))
    await waitFor(() => expect(screen.getByRole('button',{name:'发送并验证'})).toBeEnabled())
    const before = reads.mock.calls.length
    fireEvent.click(screen.getByRole('button',{name:'发送并验证'}))
    expect(await screen.findByText(/这是历史测试/)).toBeInTheDocument()
    expect(reads.mock.calls.length).toBeGreaterThan(before)
    expect(send).toHaveBeenCalledTimes(1)
    expect(onComplete).not.toHaveBeenCalled()
  })

  it('requires the PIN, SMTP secret, recipients and a real delivery test before completion', async () => {
    let current = status()
    vi.spyOn(localApi, 'installationSetupStatus').mockImplementation(async () => current)
    vi.spyOn(localApi, 'parentUnlock').mockImplementation(async () => { current = status({ parent_pin_configured: true, next_step: 'smtp' }) })
    const saveChannel = vi.spyOn(localApi, 'saveReportDeliveryChannel').mockImplementation(async () => { current = status({ parent_pin_configured: true, smtp_configured: true, smtp_secret_check_required: true, next_step: 'email_recipients' }) })
    vi.spyOn(localApi, 'reportRecipients').mockResolvedValue({ items: [] } as never)
    const addRecipient = vi.spyOn(localApi, 'addReportRecipient').mockImplementation(async () => {
      if (addRecipient.mock.calls.length === 2) current = status({ parent_pin_configured: true, smtp_configured: true, smtp_secret_check_required: true, children_missing_email_recipient: 0, next_step: 'email_test' })
      return {} as never
    })
    vi.spyOn(localApi, 'reportDeliveryChannelTests').mockResolvedValue([])
    const testChannel = vi.spyOn(localApi, 'testReportDeliveryChannel').mockImplementation(async () => {
      if (testChannel.mock.calls.length === 2) current = status({ complete: true, parent_pin_configured: true, smtp_configured: true, smtp_secret_check_required: true, children_missing_email_recipient: 0, children_missing_email_test: 0, next_step: 'complete' })
      return { status: 'passed', message: '邮件已接受' } as never
    })
    const onComplete = vi.fn()
    render(<InstallationSetupWizard children={children} initialStatus={current} onComplete={onComplete} />)

    fireEvent.change(screen.getByLabelText('初始化家长PIN'), { target: { value: '2468' } })
    fireEvent.change(screen.getByLabelText('再次输入家长PIN'), { target: { value: '2468' } })
    fireEvent.click(screen.getByRole('button', { name: '保存 PIN' }))
    await screen.findByLabelText('初始化SMTP主机')

    fireEvent.change(screen.getByLabelText('初始化SMTP主机'), { target: { value: 'smtp.example.com' } })
    fireEvent.change(screen.getByLabelText('初始化SMTP用户名'), { target: { value: 'parent@example.com' } })
    fireEvent.change(screen.getByLabelText('初始化SMTP密码'), { target: { value: 'synthetic-c0741d74e1d41a97476cbf1e' } })
    fireEvent.change(screen.getByLabelText('初始化SMTP发件地址'), { target: { value: 'parent@example.com' } })
    fireEvent.click(screen.getByRole('button', { name: '保存 SMTP' }))
    await screen.findByLabelText('初始化收件邮箱')
    expect(saveChannel).toHaveBeenCalledWith('email', expect.objectContaining({ host: 'smtp.example.com', password: 'synthetic-c0741d74e1d41a97476cbf1e' }))

    fireEvent.change(screen.getByLabelText('初始化收件邮箱'), { target: { value: 'family@example.com' } })
    fireEvent.click(screen.getByRole('button', { name: '保存收件人' }))
    await screen.findByRole('button', { name: '发送并验证' })
    await waitFor(() => expect(screen.getByRole('button', { name: '发送并验证' })).toBeEnabled())
    fireEvent.click(screen.getByRole('button', { name: '发送并验证' }))
    await waitFor(() => expect(onComplete).toHaveBeenCalledTimes(1))
    expect(addRecipient).toHaveBeenCalledTimes(2)
    expect(testChannel).toHaveBeenCalledTimes(2)
  })

  it('returns to SMTP entry when the database configuration exists but the secure password is missing', async () => {
    vi.spyOn(localApi, 'parentUnlock').mockResolvedValue()
    vi.spyOn(localApi, 'installationSetupStatus').mockResolvedValue(status({ complete: true, parent_pin_configured: true, smtp_configured: true, smtp_secret_check_required: true, children_missing_email_recipient: 0, children_missing_email_test: 0, next_step: 'complete' }))
    window.desktop!.getReportDeliverySecretStatus = vi.fn().mockResolvedValue({ state: 'ready', smtpPasswordConfigured: false, wecomWebhookConfigured: false })
    render(<InstallationSetupWizard children={children} initialStatus={status({ complete: true, parent_pin_configured: true, smtp_configured: true, smtp_secret_check_required: true, children_missing_email_recipient: 0, children_missing_email_test: 0, next_step: 'complete' })} initialSmtpSecretConfigured={false} onComplete={vi.fn()} />)
    fireEvent.change(screen.getByLabelText('初始化家长PIN'), { target: { value: '2468' } })
    fireEvent.click(screen.getByRole('button', { name: '确认身份' }))
    expect(await screen.findByLabelText('初始化SMTP密码')).toBeInTheDocument()
  })

  it('lets an authenticated parent repair quarantined SMTP without skipping consent or a new accepted test', async () => {
    // Exact production quarantine status, asserted by the paired real SQLite/SMTP control.
    let current = status({ parent_pin_configured: true, smtp_configured: true, smtp_secret_check_required: true, next_step: 'email_recipients' })
    vi.spyOn(localApi, 'installationSetupStatus').mockImplementation(async () => current)
    const unlock = vi.spyOn(localApi, 'parentUnlock').mockRejectedValueOnce(new Error('PIN 不正确')).mockResolvedValue()
    const saveChannel = vi.spyOn(localApi, 'saveReportDeliveryChannel').mockRejectedValueOnce(new Error('配置保存失败')).mockResolvedValue()
    vi.spyOn(localApi, 'reportRecipients').mockResolvedValue({ items: [{ channel: 'email', address: 'family@example.test', consent_status: 'pending' }] } as never)
    const addRecipient = vi.spyOn(localApi, 'addReportRecipient').mockImplementation(async () => {
      if (addRecipient.mock.calls.length === 2) current = { ...current, children_missing_email_recipient: 0, next_step: 'email_test' }
      return {} as never
    })
    vi.spyOn(localApi, 'reportDeliveryChannelTests').mockResolvedValue([{ channel: 'email', status: 'blocked' }] as never)
    const testChannel = vi.spyOn(localApi, 'testReportDeliveryChannel').mockResolvedValueOnce({ status: 'failed', message: 'SMTP 拒绝测试邮件' } as never).mockImplementation(async () => {
      if (testChannel.mock.calls.length === 3) current = { ...current, complete: true, children_missing_email_test: 0, next_step: 'complete' }
      return { status: 'passed', message: 'SMTP 已接受' } as never
    })
    const onComplete = vi.fn()
    render(<InstallationSetupWizard children={children} initialStatus={current} initialSmtpSecretConfigured onComplete={onComplete} />)
    expect(screen.queryByRole('button', { name: '修改 SMTP 配置' })).not.toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('初始化家长PIN'), { target: { value: '2468' } })
    fireEvent.click(screen.getByRole('button', { name: '确认身份' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('PIN 不正确')
    expect(screen.queryByLabelText('初始化SMTP主机')).not.toBeInTheDocument()
    expect(saveChannel).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: '确认身份' }))
    await screen.findByLabelText('初始化收件邮箱')
    expect(unlock).toHaveBeenLastCalledWith('2468', false)
    fireEvent.click(screen.getByRole('button', { name: '修改 SMTP 配置' }))
    expect(saveChannel).not.toHaveBeenCalled()
    expect(testChannel).not.toHaveBeenCalled()
    fireEvent.change(screen.getByLabelText('初始化SMTP主机'), { target: { value: 'smtp.new.example.test' } })
    fireEvent.change(screen.getByLabelText('初始化SMTP用户名'), { target: { value: 'new@example.test' } })
    fireEvent.change(screen.getByLabelText('初始化SMTP密码'), { target: { value: 'new-public-test-password' } })
    fireEvent.change(screen.getByLabelText('初始化SMTP发件地址'), { target: { value: 'new@example.test' } })
    fireEvent.click(screen.getByRole('button', { name: '保存 SMTP' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('配置保存失败')
    expect(screen.getByLabelText('初始化SMTP主机')).toBeInTheDocument()
    expect(onComplete).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: '保存 SMTP' }))
    await screen.findByLabelText('初始化收件邮箱')
    expect(window.desktop!.saveReportDeliverySecrets).toHaveBeenLastCalledWith({ smtpPassword: 'new-public-test-password' })
    expect(saveChannel).toHaveBeenLastCalledWith('email', expect.objectContaining({ host: 'smtp.new.example.test', username: 'new@example.test', password: 'new-public-test-password' }))
    expect(addRecipient).not.toHaveBeenCalled()
    expect(testChannel).not.toHaveBeenCalled()
    fireEvent.change(screen.getByLabelText('初始化收件邮箱'), { target: { value: 'family@example.test' } })
    fireEvent.click(screen.getByRole('button', { name: '保存收件人' }))
    await screen.findByRole('button', { name: '发送并验证' })
    expect(addRecipient).toHaveBeenCalledTimes(2)
    await waitFor(() => expect(screen.getByRole('button', { name: '发送并验证' })).toBeEnabled())
    fireEvent.click(screen.getByRole('button', { name: '发送并验证' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('SMTP 拒绝测试邮件')
    expect(onComplete).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: '修改 SMTP 配置' }))
    await screen.findByLabelText('初始化SMTP主机')
    fireEvent.click(screen.getByRole('button', { name: '取消修改 SMTP' }))
    await screen.findByRole('button', { name: '发送并验证' })
    expect(testChannel).toHaveBeenCalledTimes(1)
    await waitFor(() => expect(screen.getByRole('button', { name: '发送并验证' })).toBeEnabled())
    fireEvent.click(screen.getByRole('button', { name: '发送并验证' }))
    await waitFor(() => expect(onComplete).toHaveBeenCalledTimes(1))
    expect(testChannel).toHaveBeenCalledTimes(3)
  })

  it.each(['processing', 'unknown'] as const)('never starts an ordinary new test while a prior %s test exists', async (testStatus) => {
    const current = status({ parent_pin_configured: true, smtp_configured: true, smtp_secret_check_required: true, children_missing_email_recipient: 0, next_step: 'email_test' })
    vi.spyOn(localApi, 'parentUnlock').mockResolvedValue()
    vi.spyOn(localApi, 'installationSetupStatus').mockResolvedValue(current)
    vi.spyOn(localApi, 'reportDeliveryChannelTests').mockImplementation(async (child) => [{ channel: 'email', status: child === children[0].child_id ? testStatus : 'passed', message: '合成状态', tested_at: null, client_action_id: 'old-synthetic-action' }])
    const ordinary = vi.spyOn(localApi, 'testReportDeliveryChannel').mockResolvedValue({ status: 'passed' } as never)
    const onComplete = vi.fn()
    render(<InstallationSetupWizard children={children} initialStatus={current} initialSmtpSecretConfigured onComplete={onComplete} />)
    fireEvent.change(screen.getByLabelText('初始化家长PIN'), { target: { value: '2468' } })
    fireEvent.click(screen.getByRole('button', { name: '确认身份' }))
    await screen.findByRole('button', { name: '发送并验证' })
    expect(await screen.findByText(testStatus === 'processing' ? /测试仍在处理中/ : /上次测试结果未知/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '发送并验证' })).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: '发送并验证' }))
    await waitFor(() => expect(ordinary).not.toHaveBeenCalled())
    expect(onComplete).not.toHaveBeenCalled()
    expect(await screen.findByText(testStatus === 'processing' ? /测试仍在处理中/ : /上次测试结果未知/)).toBeInTheDocument()
  })

  it('preserves the unknown request when keeping it and uses the original reissue API only after explicit confirmation', async () => {
    let current = status({ parent_pin_configured: true, smtp_configured: true, smtp_secret_check_required: true, children_missing_email_recipient: 0, next_step: 'email_test' })
    let unresolved = true, kept = false
    vi.spyOn(localApi, 'parentUnlock').mockResolvedValue()
    vi.spyOn(localApi, 'installationSetupStatus').mockImplementation(async () => current)
    vi.spyOn(localApi, 'reportDeliveryChannelTests').mockImplementation(async (child) => [{ channel: 'email', status: child === children[0].child_id && unresolved ? 'unknown' : 'passed', message: '合成状态', tested_at: null, client_action_id: 'original-unknown-action', kept_without_resend: kept }])
    const ordinary = vi.spyOn(localApi, 'testReportDeliveryChannel').mockResolvedValue({ status: 'passed' } as never)
    const keep = vi.spyOn(localApi, 'keepUnknownReportDeliveryChannelTest').mockImplementation(async () => { kept = true; return {} as never })
    const reissue = vi.spyOn(localApi, 'reissueReportDeliveryChannelTest').mockRejectedValueOnce(new Error('响应尚未确认')).mockImplementation(async () => {
      unresolved = false; current = { ...current, complete: true, children_missing_email_test: 0, next_step: 'complete' }
      return { status: 'passed' } as never
    })
    const onComplete = vi.fn()
    render(<InstallationSetupWizard children={children} initialStatus={current} initialSmtpSecretConfigured onComplete={onComplete} />)
    fireEvent.change(screen.getByLabelText('初始化家长PIN'), { target: { value: '2468' } })
    fireEvent.click(screen.getByRole('button', { name: '确认身份' }))
    fireEvent.click(await screen.findByRole('button', { name: '暂不重发小航的测试' }))
    await screen.findByText(/已选择暂不重发/)
    expect(keep).toHaveBeenCalledWith('child-1', 'email', 'original-unknown-action')
    expect(ordinary).not.toHaveBeenCalled()
    expect(onComplete).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: '重新测试小航的邮箱' }))
    fireEvent.click(screen.getByRole('button', { name: '取消重新测试' }))
    expect(reissue).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: '重新测试小航的邮箱' }))
    fireEvent.click(screen.getByRole('button', { name: '确认重新发送固定测试邮件' }))
    await screen.findByText('响应尚未确认')
    expect(onComplete).not.toHaveBeenCalled()
    const dispatchedId = reissue.mock.calls[0][3]
    expect(reissue).toHaveBeenLastCalledWith('child-1', 'email', 'original-unknown-action', dispatchedId)
    fireEvent.click(screen.getByRole('button', { name: '确认重新发送固定测试邮件' }))
    await screen.findByRole('button', { name: '完成初始化' })
    expect(reissue).toHaveBeenNthCalledWith(2, 'child-1', 'email', 'original-unknown-action', dispatchedId)
    expect(ordinary).not.toHaveBeenCalled()
    expect(onComplete).not.toHaveBeenCalled()
    await waitFor(() => expect(screen.getByRole('button', { name: '完成初始化' })).toBeEnabled())
    fireEvent.click(screen.getByRole('button', { name: '完成初始化' }))
    await waitFor(() => expect(onComplete).toHaveBeenCalledTimes(1))
    expect(ordinary).not.toHaveBeenCalled()
  })

  it('rechecks a newly processing test before dispatch and refuses an unknown record without its original action', async () => {
    const current = status({ parent_pin_configured: true, smtp_configured: true, smtp_secret_check_required: true, children_missing_email_recipient: 0, next_step: 'email_test' })
    let phase: 'ready' | 'processing' | 'unknown' = 'ready'
    vi.spyOn(localApi, 'parentUnlock').mockResolvedValue()
    vi.spyOn(localApi, 'installationSetupStatus').mockResolvedValue(current)
    vi.spyOn(localApi, 'reportDeliveryChannelTests').mockImplementation(async () => [{ channel: 'email', status: phase === 'ready' ? 'blocked' : phase, message: '合成状态', tested_at: null }])
    const ordinary = vi.spyOn(localApi, 'testReportDeliveryChannel').mockResolvedValue({ status: 'passed' } as never)
    const reissue = vi.spyOn(localApi, 'reissueReportDeliveryChannelTest').mockResolvedValue({ status: 'passed' } as never)
    const onComplete = vi.fn()
    render(<InstallationSetupWizard children={children} initialStatus={current} initialSmtpSecretConfigured onComplete={onComplete} />)
    fireEvent.change(screen.getByLabelText('初始化家长PIN'), { target: { value: '2468' } })
    fireEvent.click(screen.getByRole('button', { name: '确认身份' }))
    await waitFor(() => expect(screen.getByRole('button', { name: '发送并验证' })).toBeEnabled())
    phase = 'processing'
    fireEvent.click(screen.getByRole('button', { name: '发送并验证' }))
    await screen.findByText(/有邮件测试仍在处理或结果未知/)
    expect(ordinary).not.toHaveBeenCalled()
    phase = 'unknown'
    fireEvent.click(screen.getByRole('button', { name: '核对测试状态' }))
    await screen.findAllByText(/原测试记录不完整/)
    expect(screen.queryByRole('button', { name: '确认重新发送固定测试邮件' })).not.toBeInTheDocument()
    expect(reissue).not.toHaveBeenCalled()
    expect(onComplete).not.toHaveBeenCalled()
  })

  it('keeps dispatch closed after a status-read failure and a status-only refresh never sends', async () => {
    const current = status({ parent_pin_configured: true, smtp_configured: true, smtp_secret_check_required: true, children_missing_email_recipient: 0, next_step: 'email_test' })
    let available = false
    vi.spyOn(localApi, 'parentUnlock').mockResolvedValue()
    vi.spyOn(localApi, 'installationSetupStatus').mockResolvedValue(current)
    vi.spyOn(localApi, 'reportDeliveryChannelTests').mockImplementation(async () => {
      if (!available) throw new Error('Synthetic status unavailable')
      return [{ channel: 'email', status: 'blocked', message: '', tested_at: null }]
    })
    const ordinary = vi.spyOn(localApi, 'testReportDeliveryChannel').mockResolvedValue({ status: 'passed' } as never)
    const onComplete = vi.fn()
    render(<InstallationSetupWizard children={children} initialStatus={current} initialSmtpSecretConfigured onComplete={onComplete} />)
    fireEvent.change(screen.getByLabelText('初始化家长PIN'), { target: { value: '2468' } })
    fireEvent.click(screen.getByRole('button', { name: '确认身份' }))
    await screen.findByText(/测试状态暂不可用/)
    expect(screen.getByRole('button', { name: '发送并验证' })).toBeDisabled()
    available = true
    fireEvent.click(screen.getByRole('button', { name: '核对测试状态' }))
    await waitFor(() => expect(screen.getByRole('button', { name: '发送并验证' })).toBeEnabled())
    expect(ordinary).not.toHaveBeenCalled()
    expect(onComplete).not.toHaveBeenCalled()
  })

  it('retires only the acknowledged original dispatch key before a later SMTP configuration test', async () => {
    let current = status({ parent_pin_configured: true, smtp_configured: true, smtp_secret_check_required: true, children_missing_email_recipient: 0, next_step: 'email_test' })
    let phase: 'initial' | 'unknown' | 'reissued' | 'reconfigured' = 'initial'
    let originalId = ''
    const baseKey = 'hsc.installation-email-test.v1:child-1:original'
    const unrelatedKey = 'hsc.installation-email-test.v1:child-2:original'
    window.localStorage.setItem(unrelatedKey, 'unrelated-original-dispatch')
    vi.spyOn(localApi, 'parentUnlock').mockResolvedValue()
    vi.spyOn(localApi, 'installationSetupStatus').mockImplementation(async () => current)
    vi.spyOn(localApi, 'reportDeliveryChannelTests').mockImplementation(async (child) => [{ channel: 'email',
      status: child === 'child-2' ? 'passed' : phase === 'unknown' ? 'unknown' : phase === 'reissued' ? 'passed' : 'blocked',
      message: '合成状态', tested_at: null, ...(phase === 'unknown' ? { client_action_id: originalId } : {}) }])
    const ordinary = vi.spyOn(localApi, 'testReportDeliveryChannel').mockImplementation(async (_child, _channel, id) => {
      if (phase === 'initial') { originalId = id; phase = 'unknown'; throw new Error('原测试 HTTP 回执丢失') }
      if (id === originalId) return { status: 'superseded', message: '历史请求已被替代' } as never
      current = { ...current, complete: true, children_missing_email_test: 0, next_step: 'complete' }
      return { status: 'passed', message: 'SMTP 已接受' } as never
    })
    const reissue = vi.spyOn(localApi, 'reissueReportDeliveryChannelTest').mockRejectedValueOnce(new Error('重新测试 HTTP 回执丢失')).mockImplementation(async () => {
      phase = 'reissued'; current = { ...current, complete: true, children_missing_email_test: 0, next_step: 'complete' }
      return { status: 'passed', message: 'SMTP 已接受' } as never
    })
    vi.spyOn(localApi, 'saveReportDeliveryChannel').mockImplementation(async () => {
      phase = 'reconfigured'; current = { ...current, complete: false, children_missing_email_test: 1, next_step: 'email_test' }
    })
    const onComplete = vi.fn()
    render(<InstallationSetupWizard children={children} initialStatus={current} initialSmtpSecretConfigured onComplete={onComplete} />)
    fireEvent.change(screen.getByLabelText('初始化家长PIN'), { target: { value: '2468' } })
    fireEvent.click(screen.getByRole('button', { name: '确认身份' }))
    await waitFor(() => expect(screen.getByRole('button', { name: '发送并验证' })).toBeEnabled())
    fireEvent.click(screen.getByRole('button', { name: '发送并验证' }))
    await screen.findByText('原测试 HTTP 回执丢失')
    expect(window.localStorage.getItem(baseKey)).toBe(originalId)
    fireEvent.click(screen.getByRole('button', { name: '核对测试状态' }))
    fireEvent.click(await screen.findByRole('button', { name: '重新测试小航的邮箱' }))
    fireEvent.click(screen.getByRole('button', { name: '确认重新发送固定测试邮件' }))
    await screen.findByText('重新测试 HTTP 回执丢失')
    expect(window.localStorage.getItem(baseKey)).toBe(originalId)
    expect(window.localStorage.getItem(unrelatedKey)).toBe('unrelated-original-dispatch')
    const replacementId = reissue.mock.calls[0][3]
    fireEvent.click(screen.getByRole('button', { name: '确认重新发送固定测试邮件' }))
    await screen.findByRole('button', { name: '完成初始化' })
    expect(reissue).toHaveBeenNthCalledWith(2, 'child-1', 'email', originalId, replacementId)
    expect(window.localStorage.getItem(baseKey)).toBeNull()
    expect(window.localStorage.getItem(unrelatedKey)).toBe('unrelated-original-dispatch')
    fireEvent.click(screen.getByRole('button', { name: '修改 SMTP 配置' }))
    fireEvent.change(screen.getByLabelText('初始化SMTP主机'), { target: { value: 'smtp.new.example.test' } })
    fireEvent.change(screen.getByLabelText('初始化SMTP用户名'), { target: { value: 'new@example.test' } })
    fireEvent.change(screen.getByLabelText('初始化SMTP密码'), { target: { value: 'public-synthetic-password' } })
    fireEvent.change(screen.getByLabelText('初始化SMTP发件地址'), { target: { value: 'new@example.test' } })
    fireEvent.click(screen.getByRole('button', { name: '保存 SMTP' }))
    await waitFor(() => expect(screen.getByRole('button', { name: '发送并验证' })).toBeEnabled())
    fireEvent.click(screen.getByRole('button', { name: '发送并验证' }))
    await waitFor(() => expect(onComplete).toHaveBeenCalledTimes(1))
    expect(ordinary).toHaveBeenCalledTimes(2)
    expect(ordinary.mock.calls[1][2]).not.toBe(originalId)
    expect(ordinary.mock.calls[1][2]).not.toBe(replacementId)
  })
})
