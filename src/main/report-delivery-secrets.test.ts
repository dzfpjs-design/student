import { mkdtemp, readFile, readdir, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const electron = vi.hoisted(() => ({
  available: true,
  encryptString: vi.fn((value: string) => Buffer.from(`protected:${value}`, 'utf8')),
  decryptString: vi.fn((value: Buffer) => value.toString('utf8').replace(/^protected:/, '')),
}))

vi.mock('electron', () => ({
  app: { getPath: () => '' },
  safeStorage: { isEncryptionAvailable: () => electron.available, encryptString: electron.encryptString, decryptString: electron.decryptString },
}))

import { ReportDeliverySecretStore, unlinkSecretFile } from './report-delivery-secrets'

describe('ReportDeliverySecretStore', () => {
  let directory = ''
  beforeEach(async () => { directory = await mkdtemp(join(tmpdir(), 'hsc-delivery-secrets-')); electron.available = true })
  afterEach(async () => { await rm(directory, { recursive: true, force: true }) })

  it('uses OS safe storage, never returns values in a summary path, and survives a launcher restart', async () => {
    const store = new ReportDeliverySecretStore(directory)
    const smtpPassword = 'synthetic-smtp-password-not-a-real-account'
    const webhook = 'https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=synthetic'
    await store.save({ smtpPassword, wecomWebhook: webhook })
    const raw = await readFile(join(directory, 'report-delivery-secrets.json'), 'utf8')
    expect(raw).not.toContain(smtpPassword)
    expect(raw).not.toContain(webhook)
    expect(await new ReportDeliverySecretStore(directory).load()).toEqual({ smtpPassword, wecomWebhook: webhook })
    await store.clear(['smtpPassword'])
    expect(await store.load()).toEqual({ wecomWebhook: webhook })
    await store.clear()
    expect(await readdir(directory)).toEqual([])
  })

  it('does not write a replacement when system secure storage is unavailable', async () => {
    electron.available = false
    await expect(new ReportDeliverySecretStore(directory).save({ smtpPassword: 'x' })).rejects.toThrow('系统安全存储不可用')
    expect(await readdir(directory)).toEqual([])
  })

  it('reports unavailable or damaged secure storage without returning a credential', async () => {
    electron.available = false
    expect((await new ReportDeliverySecretStore(directory).inspect()).summary).toEqual({ state: 'unavailable', smtpPasswordConfigured: false, wecomWebhookConfigured: false })
    electron.available = true
    await writeFile(join(directory, 'report-delivery-secrets.json'), '{not-json', 'utf8')
    const inspected = await new ReportDeliverySecretStore(directory).inspect()
    expect(inspected.secrets).toEqual({})
    expect(inspected.summary).toEqual({ state: 'corrupt', smtpPasswordConfigured: false, wecomWebhookConfigured: false })
  })

  it('ignores only ENOENT when removing a secret file, including on Windows', async () => {
    await expect(unlinkSecretFile('synthetic', async () => { throw Object.assign(new Error('gone'), { code: 'ENOENT' }) })).resolves.toBeUndefined()
    await expect(unlinkSecretFile('synthetic', async () => { throw Object.assign(new Error('denied'), { code: 'EPERM' }) })).rejects.toMatchObject({ code: 'EPERM' })
    await expect(unlinkSecretFile('synthetic', async () => { throw Object.assign(new Error('denied'), { code: 'EACCES' }) })).rejects.toMatchObject({ code: 'EACCES' })
  })
})
