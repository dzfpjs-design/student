import { app, safeStorage } from 'electron'
import { randomBytes } from 'node:crypto'
import { readFile, rename, unlink, writeFile } from 'node:fs/promises'
import { join } from 'node:path'

export type ReportDeliverySecrets = { smtpPassword?: string; wecomWebhook?: string }
export type ReportDeliverySecretSummary = { state: 'ready' | 'empty' | 'unavailable' | 'corrupt'; smtpPasswordConfigured: boolean; wecomWebhookConfigured: boolean }
type StoredSecrets = { encryptedSmtpPassword?: string; encryptedWecomWebhook?: string }

export async function unlinkSecretFile(path: string, remove: (path: string) => Promise<void> = unlink): Promise<void> {
  try { await remove(path) } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error
  }
}

/**
 * Provider secrets are owned by Electron's OS-backed safeStorage boundary.
 * The renderer can submit a new value but never receives one back; Python only
 * sees the values in the child process environment while it is running.
 */
export class ReportDeliverySecretStore {
  private readonly path: string

  constructor(userDataPath = app.getPath('userData')) {
    this.path = join(userDataPath, 'report-delivery-secrets.json')
  }

  async load(): Promise<ReportDeliverySecrets> {
    return (await this.inspect()).secrets
  }

  async inspect(): Promise<{ secrets: ReportDeliverySecrets; summary: ReportDeliverySecretSummary }> {
    if (!safeStorage.isEncryptionAvailable()) return { secrets: {}, summary: { state: 'unavailable', smtpPasswordConfigured: false, wecomWebhookConfigured: false } }
    try {
      const stored = JSON.parse(await readFile(this.path, 'utf8')) as StoredSecrets
      const secrets = {
        ...(stored.encryptedSmtpPassword ? { smtpPassword: safeStorage.decryptString(Buffer.from(stored.encryptedSmtpPassword, 'base64')) } : {}),
        ...(stored.encryptedWecomWebhook ? { wecomWebhook: safeStorage.decryptString(Buffer.from(stored.encryptedWecomWebhook, 'base64')) } : {}),
      }
      return { secrets, summary: { state: secrets.smtpPassword || secrets.wecomWebhook ? 'ready' : 'empty', smtpPasswordConfigured: Boolean(secrets.smtpPassword), wecomWebhookConfigured: Boolean(secrets.wecomWebhook) } }
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === 'ENOENT') return { secrets: {}, summary: { state: 'empty', smtpPasswordConfigured: false, wecomWebhookConfigured: false } }
      return { secrets: {}, summary: { state: 'corrupt', smtpPasswordConfigured: false, wecomWebhookConfigured: false } }
    }
  }

  async save(update: ReportDeliverySecrets): Promise<ReportDeliverySecrets> {
    if (!safeStorage.isEncryptionAvailable()) throw new Error('当前系统安全存储不可用，未保存送达通道凭据。')
    const current = await this.load()
    const next = { ...current, ...nonEmpty(update) }
    return this.write(next)
  }

  private async write(next: ReportDeliverySecrets): Promise<ReportDeliverySecrets> {
    const stored: StoredSecrets = {
      ...(next.smtpPassword ? { encryptedSmtpPassword: safeStorage.encryptString(next.smtpPassword).toString('base64') } : {}),
      ...(next.wecomWebhook ? { encryptedWecomWebhook: safeStorage.encryptString(next.wecomWebhook).toString('base64') } : {}),
    }
    const temporary = `${this.path}.${process.pid}.${randomBytes(8).toString('hex')}.tmp`
    try { await writeFile(temporary, JSON.stringify(stored), { encoding: 'utf8', flag: 'wx', mode: 0o600 }); await rename(temporary, this.path) } finally { await unlink(temporary).catch(() => undefined) }
    return next
  }

  async clear(names: Array<keyof ReportDeliverySecrets> = ['smtpPassword', 'wecomWebhook']): Promise<ReportDeliverySecrets> {
    const current = await this.load()
    for (const name of names) delete current[name]
    if (!current.smtpPassword && !current.wecomWebhook) {
      await unlinkSecretFile(this.path)
      return {}
    }
    return this.write(current)
  }
}

function nonEmpty(update: ReportDeliverySecrets): ReportDeliverySecrets {
  const next: ReportDeliverySecrets = {}
  if (typeof update.smtpPassword === 'string' && update.smtpPassword.trim()) next.smtpPassword = update.smtpPassword
  if (typeof update.wecomWebhook === 'string' && update.wecomWebhook.startsWith('https://')) next.wecomWebhook = update.wecomWebhook
  return next
}
