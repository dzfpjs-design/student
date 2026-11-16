import { afterEach, describe, expect, it, vi } from 'vitest'
import { clearReportDeliveryChannelWithDesktop, resetRuntimeCache } from './api'

describe('clearReportDeliveryChannelWithDesktop', () => {
  afterEach(() => { window.desktop = undefined; resetRuntimeCache(); vi.restoreAllMocks() })

  it.each([
    ['email', 'smtpPassword'],
    ['wechat_work_bot', 'wecomWebhook'],
  ] as const)('uses the atomic desktop channel clear for %s', async (channel, _secretName) => {
    const restarted = vi.fn()
    window.desktop = {
      clearReportDeliveryChannel: vi.fn().mockResolvedValue({ smtpPasswordConfigured: false, wecomWebhookConfigured: false, credentialsCleared: true, metadataCleared: true, runtimeRestarted: true, runtimeRestartFailed: false }),
    } as unknown as NonNullable<typeof window.desktop>
    window.addEventListener('hsc-runtime-restarted', restarted, { once: true })

    await expect(clearReportDeliveryChannelWithDesktop(channel)).resolves.toEqual({ runtimeRestarted: true })
    expect(window.desktop.clearReportDeliveryChannel).toHaveBeenCalledWith(channel, '')
    expect(restarted).toHaveBeenCalledTimes(1)
  })

  it('does not delete agent metadata when the desktop bridge is missing or safe-storage clearing fails', async () => {
    await expect(clearReportDeliveryChannelWithDesktop('email')).rejects.toThrow('只能在桌面应用中清除')

    window.desktop = {
      clearReportDeliveryChannel: vi.fn().mockRejectedValue(new Error('secure storage failed')),
    } as unknown as NonNullable<typeof window.desktop>
    await expect(clearReportDeliveryChannelWithDesktop('email')).rejects.toThrow('secure storage failed')
  })

  it('reports a retryable metadata cleanup state after credentials are cleared and the new agent is running', async () => {
    window.desktop = {
      clearReportDeliveryChannel: vi.fn().mockResolvedValue({ smtpPasswordConfigured: false, wecomWebhookConfigured: false, credentialsCleared: true, metadataCleared: false, runtimeRestarted: true, runtimeRestartFailed: false }),
    } as unknown as NonNullable<typeof window.desktop>
    await expect(clearReportDeliveryChannelWithDesktop('email')).rejects.toThrow('凭据已清、配置元数据待重试')
  })

  it('closes the stale parent session and reports a local-service restart failure', async () => {
    let restartError = ''
    window.addEventListener('hsc-runtime-restarted', (event) => { restartError = (event as CustomEvent<{ error?: string }>).detail?.error ?? '' }, { once: true })
    window.desktop = {
      clearReportDeliveryChannel: vi.fn().mockResolvedValue({ smtpPasswordConfigured: false, wecomWebhookConfigured: false, credentialsCleared: true, metadataCleared: true, runtimeRestarted: false, runtimeRestartFailed: true }),
    } as unknown as NonNullable<typeof window.desktop>
    await expect(clearReportDeliveryChannelWithDesktop('email')).rejects.toThrow('本地服务重启失败')
    expect(restartError).toContain('本地服务重启失败')
  })
})
