import { lstat, open, unlink } from 'node:fs/promises'
import { join } from 'node:path'
import { verify } from 'node:crypto'
import type { PilotRuntime } from './pilot-test-email'

const ISSUER_PUBLIC_KEY = `-----BEGIN PUBLIC KEY-----
MCowBQYDK2VwAyEA2lNvpp59ZjMepjDgy0tRCp5xLYlpVvsorhXaGXO7G4U=
-----END PUBLIC KEY-----`

/** A private installer explicitly supplies this resource. Ordinary SMTP defaults grant no consent. */
export async function importPilotConsent(options: { resourcesPath: string; recipient: string; runtime: PilotRuntime;
  sign: (bootstrapId: string, recipient: string) => string; fetchImpl?: typeof fetch; issuerPublicKey?: string }): Promise<'absent' | 'processed'> {
  const path = join(options.resourcesPath, 'pilot-diagnostic-consent.json')
  let before
  try { before = await lstat(path) } catch (error) { if ((error as NodeJS.ErrnoException).code === 'ENOENT') return 'absent'; throw Error('PILOT_PROVISION_READ_FAILED') }
  if (!before.isFile() || before.isSymbolicLink() || before.size > 2048) throw Error('PILOT_PROVISION_INVALID')
  const file = await open(path, 'r')
  let data: unknown
  try {
    const stat = await file.stat()
    if (!stat.isFile() || stat.ino !== before.ino || stat.dev !== before.dev || stat.size > 2048) throw Error('PILOT_PROVISION_INVALID')
    data = JSON.parse((await file.readFile()).toString('utf8'))
  } finally { await file.close() }
  if (!data || typeof data !== 'object' || Array.isArray(data)) throw Error('PILOT_PROVISION_INVALID')
  const payload = data as Record<string, unknown>
  const configured = options.recipient.trim().toLowerCase()
  const expected = configured || (typeof payload.recipient === 'string' ? payload.recipient.trim().toLowerCase() : '')
  if (Object.keys(payload).sort().join(',') !== 'bootstrap_id,recipient,signature,version' || payload.version !== 1
    || typeof payload.bootstrap_id !== 'string' || !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(payload.bootstrap_id)
    || !expected || expected.length > 320 || !/^[^\s@]+@[^\s@]+$/.test(expected)
    || typeof payload.recipient !== 'string' || payload.recipient.trim().toLowerCase() !== expected
    || typeof payload.signature !== 'string' || !/^[A-Za-z0-9+/]{86}==$/.test(payload.signature)) throw Error('PILOT_PROVISION_INVALID')
  const signed = Buffer.from(`hsc-pilot-diagnostic-consent-v1\n${payload.bootstrap_id}\n${expected}`, 'utf8')
  if (!verify(null, signed, options.issuerPublicKey ?? ISSUER_PUBLIC_KEY, Buffer.from(payload.signature, 'base64'))) throw Error('PILOT_PROVISION_INVALID')
  if (options.runtime.status !== 'ready' || !options.runtime.sessionToken) throw Error('PILOT_PROVISION_RUNTIME_UNAVAILABLE')
  const response = await (options.fetchImpl ?? fetch)(new URL('/v1/pilot-test-digest/provision-consent', options.runtime.localApiUrl), {
    method: 'POST', headers: { 'Content-Type': 'application/json', 'X-HSC-Session': options.runtime.sessionToken },
    body: JSON.stringify({ recipient: expected, bootstrap_id: payload.bootstrap_id, signature: options.sign(payload.bootstrap_id, expected), issuer_signature: payload.signature }),
    signal: AbortSignal.timeout(15_000), cache: 'no-store',
  })
  if (!response.ok || (await response.json() as { processed?: unknown }).processed !== true) throw Error('PILOT_PROVISION_FAILED')
  await unlink(path)
  return 'processed'
}
