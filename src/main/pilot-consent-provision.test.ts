import { generateKeyPairSync, sign } from 'node:crypto'
import { mkdtemp, readFile, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { expect, it, vi } from 'vitest'
import { importPilotConsent } from './pilot-consent-provision'

const keys = generateKeyPairSync('ed25519')
const id = '11111111-1111-4111-8111-111111111111'
const resource = (recipient: string) => ({ version:1, bootstrap_id:id, recipient, signature:sign(null, Buffer.from(`hsc-pilot-diagnostic-consent-v1\n${id}\n${recipient}`), keys.privateKey).toString('base64') })
const issuerPublicKey = keys.publicKey.export({type:'spki',format:'pem'}).toString()

it('only consumes the private artifact after successful server receipt and is idempotent on restart', async () => {
  const root = await mkdtemp(join(tmpdir(), 'pilot-consent-'))
  const path = join(root, 'pilot-diagnostic-consent.json')
  await writeFile(path, JSON.stringify(resource('parent@example.test')))
  const fetchImpl = vi.fn().mockResolvedValueOnce(new Response('',{status:503})).mockResolvedValueOnce(Response.json({processed:true}))
  const options = { resourcesPath:root, recipient:'parent@example.test', runtime:{status:'ready',localApiUrl:'http://127.0.0.1:1234',sessionToken:'synthetic'}, sign:()=>'a'.repeat(64), fetchImpl, issuerPublicKey }
  await expect(importPilotConsent(options)).rejects.toThrow('PILOT_PROVISION_FAILED')
  expect(await readFile(path,'utf8')).toContain('parent@example.test')
  expect(await importPilotConsent(options)).toBe('processed')
  expect(await importPilotConsent(options)).toBe('absent')
  expect(fetchImpl).toHaveBeenCalledTimes(2)
})
it('rejects mismatched recipient before network', async () => {
  const root = await mkdtemp(join(tmpdir(), 'pilot-consent-'))
  await writeFile(join(root,'pilot-diagnostic-consent.json'), JSON.stringify({version:1,bootstrap_id:'11111111-1111-4111-8111-111111111111',recipient:'different@example.test'}))
  const fetchImpl = vi.fn()
  await expect(importPilotConsent({resourcesPath:root, recipient:'parent@example.test', runtime:{status:'ready',localApiUrl:'http://127.0.0.1',sessionToken:'synthetic'},sign:()=>'',fetchImpl,issuerPublicKey})).rejects.toThrow('PILOT_PROVISION_INVALID')
  expect(fetchImpl).not.toHaveBeenCalled()
})

it('rejects a forged installer artifact before network', async () => {
  const root = await mkdtemp(join(tmpdir(), 'pilot-consent-'))
  const forged = {...resource('parent@example.test'), signature: Buffer.alloc(64).toString('base64')}
  await writeFile(join(root,'pilot-diagnostic-consent.json'), JSON.stringify(forged))
  const fetchImpl = vi.fn()
  await expect(importPilotConsent({resourcesPath:root,recipient:'parent@example.test',runtime:{status:'ready',localApiUrl:'http://127.0.0.1',sessionToken:'synthetic'},sign:()=>'',fetchImpl,issuerPublicKey})).rejects.toThrow('PILOT_PROVISION_INVALID')
  expect(fetchImpl).not.toHaveBeenCalled()
})

it('can provision a signed recipient when no address is embedded in the app', async () => {
  const root = await mkdtemp(join(tmpdir(), 'pilot-consent-'))
  const signed = resource('guardian@example.test')
  await writeFile(join(root, 'pilot-diagnostic-consent.json'), JSON.stringify(signed))
  const fetchImpl = vi.fn().mockResolvedValue(Response.json({ processed: true }))
  const result = await importPilotConsent({resourcesPath: root, recipient: '', runtime:{status:'ready',localApiUrl:'http://127.0.0.1:1234',sessionToken:'synthetic'},sign:()=> 'a'.repeat(64),fetchImpl,issuerPublicKey})
  expect(result).toBe('processed')
  expect(JSON.parse(String(fetchImpl.mock.calls[0][1].body))).toMatchObject({recipient:'guardian@example.test',issuer_signature:signed.signature})
})

it('keeps a signed installer resource while restored data is quarantined', async () => {
  const root = await mkdtemp(join(tmpdir(), 'pilot-consent-'))
  const path = join(root, 'pilot-diagnostic-consent.json')
  await writeFile(path, JSON.stringify(resource('guardian@example.test')))
  const fetchImpl = vi.fn().mockResolvedValueOnce(Response.json({detail:{code:'PILOT_CONSENT_RESTORE_QUARANTINED'}},{status:409}))
    .mockResolvedValueOnce(Response.json({processed:true}))
  const options = {resourcesPath:root, recipient:'', runtime:{status:'ready',localApiUrl:'http://127.0.0.1:1234',sessionToken:'synthetic'},sign:()=> 'a'.repeat(64),fetchImpl,issuerPublicKey}
  await expect(importPilotConsent(options)).rejects.toThrow('PILOT_PROVISION_FAILED')
  expect(await readFile(path,'utf8')).toContain('guardian@example.test')
  expect(await importPilotConsent(options)).toBe('processed')
})
