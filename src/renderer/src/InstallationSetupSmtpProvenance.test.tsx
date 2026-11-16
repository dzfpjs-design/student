import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { InstallationSetupWizard } from './InstallationSetupWizard'
import { localApi } from './api'
import { canReuseSetupSmtpSecret, householdSetupDefaults } from './householdSetupDefaults'
const preset = { smtp: { host: 'smtp.public.test', port: 465, username: 'sender@public.test', from_address: 'sender@public.test', use_ssl: true, starttls: false, allow_external_delivery: true as const }, recipient: 'parent@public.test' }
const children = [{ child_id: 'public-a', nickname: '公开甲' }, { child_id: 'public-b', nickname: '公开乙' }] as any
let status: any, secretConfigured = true
const defaults = vi.fn(), saveSecrets = vi.fn(), onComplete = vi.fn()
beforeEach(() => {
  status = { schema: 'hsc.installation-setup.v1', complete: false, parent_pin_configured: false, smtp_configured: false,
    smtp_secret_check_required: false, active_child_count: 2, children_missing_email_recipient: 2, children_missing_email_test: 2 }
  secretConfigured = true; defaults.mockResolvedValue(preset)
  window.desktop = { getHouseholdSetupDefaults: defaults, saveReportDeliverySecrets: saveSecrets,
    getReportDeliverySecretStatus: vi.fn(async () => ({ smtpPasswordConfigured: secretConfigured })) } as never
  vi.spyOn(localApi, 'parentUnlock').mockImplementation(async () => { status = { ...status, parent_pin_configured: true } })
  vi.spyOn(localApi, 'installationSetupStatus').mockImplementation(async () => status)
  vi.spyOn(localApi, 'saveReportDeliveryChannel').mockImplementation(async () => { status = { ...status, smtp_configured: true, smtp_secret_check_required: true }; return {} as never })
  vi.spyOn(localApi, 'reportRecipients').mockResolvedValue({ items: [] } as never)
  vi.spyOn(localApi, 'addReportRecipient').mockResolvedValue({} as never)
  vi.spyOn(localApi, 'testReportDeliveryChannel').mockResolvedValue({ status: 'passed' } as never)
})
afterEach(() => { cleanup(); delete window.desktop; vi.restoreAllMocks(); vi.clearAllMocks() })
async function unlock() {
  render(<InstallationSetupWizard children={children} initialStatus={status} initialSmtpSecretConfigured={secretConfigured} onComplete={onComplete} />)
  expect(defaults).not.toHaveBeenCalled()
  fireEvent.change(screen.getByLabelText('初始化家长PIN'), { target: { value: '2468' } })
  fireEvent.change(screen.getByLabelText('再次输入家长PIN'), { target: { value: '2468' } })
  fireEvent.click(screen.getByText('保存 PIN'))
  await screen.findByLabelText('初始化SMTP主机')
}
async function prefilled() { await unlock(); await waitFor(() => expect(screen.getByLabelText('初始化SMTP主机')).toHaveValue(preset.smtp.host)) }

it('reads only after PIN, prefills no password, reuses exact protected sender and leaves recipient/test explicit', async () => {
  await prefilled()
  expect(screen.getByLabelText('初始化SMTP密码')).toHaveValue('')
  fireEvent.click(screen.getByText('保存 SMTP'))
  expect(await screen.findByLabelText('初始化收件邮箱')).toHaveValue(preset.recipient)
  expect(saveSecrets).not.toHaveBeenCalled()
  expect(localApi.saveReportDeliveryChannel).toHaveBeenCalledExactlyOnceWith('email', preset.smtp)
  expect(localApi.addReportRecipient).not.toHaveBeenCalled(); expect(localApi.testReportDeliveryChannel).not.toHaveBeenCalled(); expect(onComplete).not.toHaveBeenCalled()
})

it.each([
  ['初始化SMTP主机', 'smtp.other.test'], ['初始化SMTP端口', '587'], ['初始化SMTP用户名', 'other@public.test'],
  ['初始化SMTP发件地址', 'other@public.test'], ['初始化SMTP加密', 'starttls'],
] as const)('requires a new explicit password after %s changes and never sends the old protected secret', async (label, value) => {
  await prefilled(); fireEvent.change(screen.getByLabelText(label), { target: { value } }); fireEvent.click(screen.getByText('保存 SMTP'))
  expect(await screen.findByRole('alert')).toHaveTextContent('请完整填写')
  expect(saveSecrets).not.toHaveBeenCalled(); expect(localApi.saveReportDeliveryChannel).not.toHaveBeenCalled()
})

it('does not treat installer defaults as proof a protected password exists', async () => {
  secretConfigured = false; await prefilled(); fireEvent.click(screen.getByText('保存 SMTP'))
  expect(await screen.findByRole('alert')).toHaveTextContent('请完整填写'); expect(localApi.saveReportDeliveryChannel).not.toHaveBeenCalled()
})

it('a late defaults reply cannot overwrite a user-entered endpoint or recipient', async () => {
  let release!: (value: unknown) => void
  defaults.mockImplementationOnce(() => new Promise(resolve => { release = resolve }))
  await unlock()
  for (const [label, value] of [['初始化SMTP主机', 'smtp.chosen.test'], ['初始化SMTP用户名', 'chosen@public.test'], ['初始化SMTP发件地址', 'chosen@public.test'], ['初始化SMTP密码', 'PUBLIC_SYNTHETIC_NEW_PASSWORD']]) {
    fireEvent.change(screen.getByLabelText(label), { target: { value } })
  }
  fireEvent.click(screen.getByText('保存 SMTP')); await screen.findByLabelText('初始化收件邮箱')
  fireEvent.change(screen.getByLabelText('初始化收件邮箱'), { target: { value: 'chosen-parent@public.test' } })
  await act(async () => { release(preset) })
  expect(screen.getByLabelText('初始化收件邮箱')).toHaveValue('chosen-parent@public.test')
  expect(localApi.saveReportDeliveryChannel).toHaveBeenCalledWith('email', expect.objectContaining({ host: 'smtp.chosen.test', password: 'PUBLIC_SYNTHETIC_NEW_PASSWORD' }))
  expect(localApi.addReportRecipient).not.toHaveBeenCalled(); expect(localApi.testReportDeliveryChannel).not.toHaveBeenCalled()
})

it('rejects secret/unknown default fields, insecure remote SMTP and nonexact protected-identity reuse', () => {
  expect(householdSetupDefaults(preset)).toEqual(preset)
  for (const raw of [{ ...preset, password: 'PUBLIC_SENTINEL' }, { ...preset, smtp: { ...preset.smtp, password: 'PUBLIC_SENTINEL' } }, { ...preset, smtp: { ...preset.smtp, use_ssl: false } }, { ...preset, smtp: { ...preset.smtp, starttls: true } }]) expect(householdSetupDefaults(raw)).toBeNull()
  const fields = { host: preset.smtp.host, port: '465', username: preset.smtp.username, fromAddress: preset.smtp.from_address, security: 'ssl' as const }
  expect(canReuseSetupSmtpSecret(preset, true, fields)).toBe(true)
  expect(canReuseSetupSmtpSecret(preset, false, fields)).toBe(false)
  expect(canReuseSetupSmtpSecret(preset, true, { ...fields, host: fields.host + ' ' })).toBe(false)
  expect(canReuseSetupSmtpSecret(preset, true, { ...fields, port: '0465' })).toBe(false)
})

it('a protected-origin override after initial prefill must be re-read and cannot authorize password omission', async () => {
  await prefilled(); defaults.mockResolvedValue(null)
  fireEvent.click(screen.getByText('保存 SMTP'))
  expect(await screen.findByRole('alert')).toHaveTextContent('请完整填写')
  expect(localApi.saveReportDeliveryChannel).not.toHaveBeenCalled(); expect(saveSecrets).not.toHaveBeenCalled()
  expect(defaults).toHaveBeenCalledTimes(2)
  expect(screen.queryByText(/本机已安全保存这组发件配置/)).not.toBeInTheDocument()
})

it('saving B with a new secret then returning to preset A cannot reuse the cached A provenance', async () => {
  await prefilled(); defaults.mockResolvedValue(null)
  for (const [label, value] of [['初始化SMTP主机', 'smtp.other.test'], ['初始化SMTP用户名', 'other@public.test'], ['初始化SMTP发件地址', 'other@public.test'], ['初始化SMTP密码', 'PUBLIC_OTHER_PASSWORD']]) {
    fireEvent.change(screen.getByLabelText(label), { target: { value } })
  }
  fireEvent.click(screen.getByText('保存 SMTP')); await screen.findByLabelText('初始化收件邮箱')
  fireEvent.click(screen.getByText('修改 SMTP 配置'))
  for (const [label, value] of [['初始化SMTP主机', preset.smtp.host], ['初始化SMTP用户名', preset.smtp.username], ['初始化SMTP发件地址', preset.smtp.from_address]]) {
    fireEvent.change(screen.getByLabelText(label), { target: { value } })
  }
  expect(screen.getByLabelText('初始化SMTP密码')).toHaveValue('')
  fireEvent.click(screen.getByText('保存 SMTP'))
  expect(await screen.findByRole('alert')).toHaveTextContent('请完整填写')
  expect(localApi.saveReportDeliveryChannel).toHaveBeenCalledTimes(1)
  expect(localApi.saveReportDeliveryChannel).toHaveBeenLastCalledWith('email', expect.objectContaining({ host: 'smtp.other.test', password: 'PUBLIC_OTHER_PASSWORD' }))
})

it('re-reads the protected password status rather than trusting its earlier boolean', async () => {
  await prefilled(); secretConfigured = false
  fireEvent.click(screen.getByText('保存 SMTP'))
  expect(await screen.findByRole('alert')).toHaveTextContent('请完整填写')
  expect(localApi.saveReportDeliveryChannel).not.toHaveBeenCalled()
})

it('a fresh protected-default read failure closes reuse and never displays its raw exception', async () => {
  await prefilled(); defaults.mockRejectedValue(Error('PUBLIC_RAW_ERROR_SENTINEL'))
  fireEvent.click(screen.getByText('保存 SMTP'))
  expect(await screen.findByRole('alert')).toHaveTextContent('请完整填写')
  expect(document.body).not.toHaveTextContent('PUBLIC_RAW_ERROR_SENTINEL')
  expect(localApi.saveReportDeliveryChannel).not.toHaveBeenCalled()
  expect(screen.queryByText(/本机已安全保存这组发件配置/)).not.toBeInTheDocument()
})

it('any successful protected-secret write immediately revokes the original cached omission hint', async () => {
  await prefilled(); fireEvent.change(screen.getByLabelText('初始化SMTP密码'), { target: { value: 'PUBLIC_ROTATED_PASSWORD' } })
  fireEvent.click(screen.getByText('保存 SMTP')); await screen.findByLabelText('初始化收件邮箱')
  fireEvent.click(screen.getByText('修改 SMTP 配置'))
  expect(screen.queryByText(/本机已安全保存这组发件配置/)).not.toBeInTheDocument()
  expect(saveSecrets).toHaveBeenCalledOnce()
})

it('an initial defaults read arriving after a successful secret write cannot restore old provenance', async () => {
  let release!: (value: unknown) => void
  defaults.mockImplementationOnce(() => new Promise(resolve => { release = resolve }))
  await unlock()
  for (const [label, value] of [['初始化SMTP主机', preset.smtp.host], ['初始化SMTP用户名', preset.smtp.username], ['初始化SMTP发件地址', preset.smtp.from_address], ['初始化SMTP密码', 'PUBLIC_ROTATED_PASSWORD']]) {
    fireEvent.change(screen.getByLabelText(label), { target: { value } })
  }
  fireEvent.click(screen.getByText('保存 SMTP')); await screen.findByLabelText('初始化收件邮箱')
  await act(async () => { release(preset) })
  fireEvent.click(screen.getByText('修改 SMTP 配置'))
  expect(screen.queryByText(/本机已安全保存这组发件配置/)).not.toBeInTheDocument()
})
