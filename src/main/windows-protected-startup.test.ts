import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { waitForWindowsProtectedStorage } from './windows-protected-startup'

describe('Windows protected provider durability', () => {
  let root: string
  const durable = JSON.stringify({ os_crypt: { encrypted_key: Buffer.from('DPAPI:synthetic-protected-key').toString('base64') } })
  beforeEach(async () => { root = await mkdtemp(join(tmpdir(), 'hsc-provider-durability-')) })
  afterEach(async () => { await rm(root, { recursive: true, force: true }) })

  it('waits for the provider write rather than treating in-memory encryption as durable', async () => {
    let finished = false
    const waiting = waitForWindowsProtectedStorage(root, 1000, 5).then(() => { finished = true })
    await new Promise(resolve => setTimeout(resolve, 15))
    expect(finished).toBe(false)
    await writeFile(join(root, 'Local State'), durable)
    await waiting
    expect(finished).toBe(true)
    expect(await readFile(join(root, 'Local State'), 'utf8')).toBe(durable)
  })

  it.each(['{}', '{pending', JSON.stringify({ os_crypt: { encrypted_key: 'plain-text-not-protected' } })])(
    'fails closed without modifying an incomplete provider: %s', async raw => {
      await writeFile(join(root, 'Local State'), raw)
      await expect(waitForWindowsProtectedStorage(root, 20, 5)).rejects.toThrow('未创建或更换本地密钥')
      expect(await readFile(join(root, 'Local State'), 'utf8')).toBe(raw)
    },
  )

  it('does not recreate missing provider state on timeout', async () => {
    await expect(waitForWindowsProtectedStorage(root, 10, 2)).rejects.toThrow('尚未可靠保存')
    await expect(readFile(join(root, 'Local State'))).rejects.toMatchObject({ code: 'ENOENT' })
  })
})
