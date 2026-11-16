import { app, safeStorage } from 'electron'
import { lstat, mkdir, readFile, rename, unlink, writeFile } from 'node:fs/promises'
import { dirname, join, resolve } from 'node:path'
import process from 'node:process'

const MAX_KEY_BYTES = 8 * 1024
const MODEL = 'qwen3.8-omni-flash-realtime'
const DEFAULT_VOICE = 'Tina'

async function readBoundedKey() {
  const chunks = []
  let size = 0
  for await (const chunk of process.stdin) {
    size += chunk.length
    if (size > MAX_KEY_BYTES) throw new Error('Realtime voice credential input exceeded the size limit.')
    chunks.push(chunk)
  }
  const key = Buffer.concat(chunks).toString('utf8').trim()
  if (key.length < 16) throw new Error('Realtime voice credential input is incomplete.')
  return key
}

function normalizeEndpoint(value) {
  let endpoint
  try { endpoint = new URL(String(value ?? '').trim()) } catch { throw new Error('Realtime voice endpoint is invalid.') }
  if (endpoint.protocol !== 'https:' || endpoint.username || endpoint.password
    || !/^[a-zA-Z0-9_-]+\.(?:cn-beijing|ap-southeast-1)\.maas\.aliyuncs\.com$/.test(endpoint.hostname)) {
    throw new Error('Realtime voice endpoint must be an approved Bailian workspace HTTPS origin.')
  }
  return endpoint.origin
}

async function main() {
  const targetUserData = process.env.HSC_TARGET_USER_DATA_DIR?.trim()
  if (!targetUserData) throw new Error('HSC_TARGET_USER_DATA_DIR is required.')
  const endpoint = normalizeEndpoint(process.env.HSC_QWEN_REALTIME_ENDPOINT)
  const inheritedKey = process.env.HSC_PROVISION_QWEN_REALTIME_KEY?.trim()
  delete process.env.HSC_PROVISION_QWEN_REALTIME_KEY
  app.setPath('userData', resolve(targetUserData))
  await app.whenReady()
  for (const name of ['qwen-key-rotation-required.bin','qwen-key-rotation']) {
    try { await lstat(join(app.getPath('userData'),name)); throw new Error('QWEN_ROTATION_MANAGED_SETTINGS_REQUIRED') }
    catch (error) { if (error?.code !== 'ENOENT') throw error }
  }
  if (!safeStorage.isEncryptionAvailable()) throw new Error('Electron safeStorage is unavailable for this Windows user.')

  const apiKey = inheritedKey || await readBoundedKey()
  if (apiKey.length < 16) throw new Error('Realtime voice credential input is incomplete.')
  const targetPath = join(app.getPath('userData'), 'qwen-realtime-settings.json')
  const stored = {
    endpoint,
    model: MODEL,
    voice: DEFAULT_VOICE,
    enabled: true,
    encryptedApiKey: safeStorage.encryptString(apiKey).toString('base64'),
  }
  await mkdir(dirname(targetPath), { recursive: true })
  const temporaryPath = `${targetPath}.${process.pid}.tmp`
  try {
    await writeFile(temporaryPath, JSON.stringify(stored), { encoding: 'utf8', flag: 'wx', mode: 0o600 })
    await rename(temporaryPath, targetPath)
  } finally {
    await unlink(temporaryPath).catch(() => undefined)
  }
  const raw = await readFile(targetPath, 'utf8')
  if (raw.includes(apiKey)) throw new Error('Realtime voice credential plaintext was detected after provisioning.')
  process.stdout.write(JSON.stringify({
    configured: true,
    endpoint,
    model: MODEL,
    voice: DEFAULT_VOICE,
    plaintextAbsent: true,
  }))
}

main()
  .then(() => process.exit(0))
  .catch((error) => {
    const message = error instanceof Error ? error.message : String(error)
    process.stderr.write(`Realtime voice settings provisioning failed: ${message}\n`)
    process.exit(1)
  })
