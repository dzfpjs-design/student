import { describe, expect, it, vi } from 'vitest'
import { FOXMAIL_MAPI_SCRIPT, resolveFoxmailPowerShell, sendWithConfiguredFoxmail } from './foxmail-delivery'

describe('Foxmail Simple MAPI delivery', () => {
  it('passes the message only through stdin and accepts a client receipt', async () => {
    const runner = vi.fn(async (_command: string, input: string) => ({
      exitCode: 0, stdout: JSON.stringify({ accepted: true, client: 'Foxmail' }), stderr: '',
    }))
    const message = { recipient: 'guardian@example.test', subject: '家庭学习测试日报', body: '今日测试摘要' }

    await expect(sendWithConfiguredFoxmail(message, runner)).resolves.toEqual({
      accepted: true, client: 'Foxmail', transport: 'simple_mapi',
    })
    expect(runner).toHaveBeenCalledTimes(1)
    expect(Buffer.from(runner.mock.calls[0][1], 'base64').toString('utf8')).toBe(JSON.stringify(message))
    expect(runner.mock.calls[0][0]).not.toContain(message.recipient)
    expect(runner.mock.calls[0][0]).not.toContain(message.body)
  })

  it('refuses invalid recipients and preserves typed client failures', async () => {
    const runner = vi.fn(async () => ({ exitCode: 0, stdout: '{}', stderr: '' }))
    await expect(sendWithConfiguredFoxmail({ recipient: 'not-mail', subject: 'x', body: 'x' }, runner)).rejects.toThrow('FOXMAIL_RECIPIENT_INVALID')
    await expect(sendWithConfiguredFoxmail({ recipient: 'guardian@example.test', subject: 'x', body: 'x' }, async () => ({
      exitCode: 0, stdout: JSON.stringify({ accepted: false, error_code: 'FOXMAIL_NOT_DEFAULT' }), stderr: '',
    }))).rejects.toThrow('FOXMAIL_NOT_DEFAULT')
    await expect(sendWithConfiguredFoxmail({ recipient: 'guardian@example.test', subject: 'x', body: 'x' }, async () => ({
      exitCode: 13, stdout: JSON.stringify({ accepted: false, error_code: 'FOXMAIL_MAPI_2' }), stderr: '',
    }))).rejects.toThrow('FOXMAIL_MAPI_2')
  })

  it('uses default-client detection and a no-dialog MAPI submission', () => {
    expect(FOXMAIL_MAPI_SCRIPT).toContain("$client -notmatch 'Foxmail'")
    expect(FOXMAIL_MAPI_SCRIPT).toContain('MAPILogon(IntPtr.Zero, null, null, 64 | 2')
    expect(FOXMAIL_MAPI_SCRIPT).toContain('LastStage = "send_without_session"')
    expect(FOXMAIL_MAPI_SCRIPT).toContain('MAPISendMail(IntPtr.Zero, IntPtr.Zero, message, 0, 0)')
    expect(FOXMAIL_MAPI_SCRIPT).toContain('MAPISendMail(session, IntPtr.Zero, message, 0, 0)')
    expect(FOXMAIL_MAPI_SCRIPT).toContain('[Console]::In.ReadToEnd()')
    expect(FOXMAIL_MAPI_SCRIPT).toContain('FromBase64String')
    expect(resolveFoxmailPowerShell('win32', 'x64', 'C:\\Windows')).toBe('C:\\Windows\\SysWOW64\\WindowsPowerShell\\v1.0\\powershell.exe')
    expect(resolveFoxmailPowerShell('win32', 'ia32', 'C:\\Windows')).toBe('powershell.exe')
  })
})
