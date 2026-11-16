import { createCipheriv, randomBytes } from 'node:crypto'
import { mkdtemp, mkdir, readFile, readdir, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { loadTutorExplanationConfig } from './tutor-explanation-settings'

const storage = vi.hoisted(() => ({ available: true }))
vi.mock('electron', () => ({
  safeStorage: {
    isEncryptionAvailable: () => storage.available,
    encryptString: (value: string) => Buffer.from(value, 'utf8').reverse(),
    decryptString: (value: Buffer) => Buffer.from(value).reverse().toString('utf8'),
  },
}))

let root: string, profile: string, resources: string
const synthetic = 'synthetic-tutor-key-01'
const stored = 'tutor-explanation-settings.json'
const payload = 'tutor-explanation.bin'
const keyName = 'tutor-explanation.key'

beforeEach(async () => {
  root = await mkdtemp(join(tmpdir(), 'hsc-tutor-settings-'))
  profile = join(root, 'profile')
  resources = join(root, 'resources')
  await Promise.all([mkdir(profile), mkdir(resources)])
  storage.available = true
})
afterEach(async () => { await rm(root, { recursive: true, force: true }) })

async function stage(value: string): Promise<void> {
  const magic = Buffer.from('HSCTUT01'), key = randomBytes(32), iv = randomBytes(12)
  const cipher = createCipheriv('aes-256-gcm', key, iv)
  cipher.setAAD(magic)
  const data = Buffer.concat([cipher.update(value, 'utf8'), cipher.final()])
  await Promise.all([
    writeFile(join(resources, keyName), key),
    writeFile(join(resources, payload), Buffer.concat([magic, iv, cipher.getAuthTag(), data])),
  ])
}

it('imports a private pair, then loads the durable encrypted setting on restart and upgrade', async () => {
  expect(await loadTutorExplanationConfig(profile, resources)).toBeNull()
  await stage(synthetic)
  expect(await loadTutorExplanationConfig(profile)).toBeNull()
  expect(await loadTutorExplanationConfig(profile, resources)).toEqual({ apiKey: synthetic })
  expect(await readdir(resources)).toEqual([])
  expect(await readdir(profile)).toEqual([stored])
  expect(await readFile(join(profile, stored), 'utf8')).not.toContain(synthetic)
  expect(await loadTutorExplanationConfig(profile, resources)).toEqual({ apiKey: synthetic })
  await stage(synthetic + '-replacement')
  expect(await loadTutorExplanationConfig(profile, resources)).toEqual({ apiKey: synthetic + '-replacement' })
  expect(await loadTutorExplanationConfig(profile)).toEqual({ apiKey: synthetic + '-replacement' })
})

it('retains the authenticated import files for retry if Windows storage is unavailable', async () => {
  await stage(synthetic)
  storage.available = false
  await expect(loadTutorExplanationConfig(profile, resources)).rejects.toThrow('TUTOR_EXPLANATION_PROVISIONING_FAILED')
  expect((await readdir(resources)).sort()).toEqual([payload, keyName])
  expect(await readdir(profile)).toEqual([])
  storage.available = true
  expect(await loadTutorExplanationConfig(profile, resources)).toEqual({ apiKey: synthetic })
})

it('does not replace the last saved setting with an unauthenticated payload', async () => {
  await stage(synthetic)
  await loadTutorExplanationConfig(profile, resources)
  await stage(synthetic + '-replacement')
  const bytes = await readFile(join(resources, payload))
  bytes[20] ^= 1
  await writeFile(join(resources, payload), bytes)
  await expect(loadTutorExplanationConfig(profile, resources)).rejects.toThrow('TUTOR_EXPLANATION_PROVISIONING_FAILED')
  expect(await loadTutorExplanationConfig(profile)).toEqual({ apiKey: synthetic })
})
