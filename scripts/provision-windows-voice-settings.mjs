import { app, safeStorage } from 'electron'
import { lstat, mkdir, readFile, rename, unlink, writeFile } from 'node:fs/promises'
import { dirname, join, resolve } from 'node:path'
import process from 'node:process'

const MAX_INPUT_BYTES = 16 * 1024
const MODEL = 'qwen3-tts-vc-2026-01-22'

async function readBoundedStdin() {
  const chunks = []
  let size = 0
  for await (const chunk of process.stdin) {
    size += chunk.length
    if (size > MAX_INPUT_BYTES) throw new Error('Voice settings input exceeded the size limit.')
    chunks.push(chunk)
  }
  return Buffer.concat(chunks).toString('utf8')
}

async function emitResult(result) {
  const resultPath = process.env.HSC_VOICE_PROVISION_RESULT_PATH?.trim()
  const serialized = JSON.stringify(result)
  if (resultPath) {
    await mkdir(dirname(resolve(resultPath)), { recursive: true })
    await writeFile(resolve(resultPath), serialized, { encoding: 'utf8', mode: 0o600 })
  }
  process.stdout.write(serialized)
}

function normalizeConfig(value) {
  const apiKey = String(value?.apiKey ?? '').trim()
  const voiceId = String(value?.voiceId ?? '').trim()
  if (value?.provider !== 'qwen_voice_clone') throw new Error('Only the enrolled Qwen reference voice is supported by this provisioner.')
  if (apiKey.length < 8) throw new Error('The voice API key is incomplete.')
  if (!/^[a-zA-Z0-9._:-]{2,120}$/.test(voiceId)) throw new Error('The enrolled voice ID is invalid.')
  if (value?.model !== MODEL) throw new Error('The reference voice model is invalid.')
  return { provider: 'qwen_voice_clone', relayUrl: '', apiKey, voiceId, model: MODEL, region: 'cn-beijing' }
}

async function main() {
  const targetUserData = process.env.HSC_TARGET_USER_DATA_DIR?.trim()
  if (!targetUserData) throw new Error('HSC_TARGET_USER_DATA_DIR is required.')
  const inheritedInput = process.env.HSC_PROVISION_VOICE_SETTINGS_JSON
  delete process.env.HSC_PROVISION_VOICE_SETTINGS_JSON
  app.setPath('userData', resolve(targetUserData))
  await app.whenReady()
  for (const name of ['qwen-key-rotation-required.bin','qwen-key-rotation']) {
    try { await lstat(join(app.getPath('userData'),name)); throw new Error('QWEN_ROTATION_MANAGED_SETTINGS_REQUIRED') }
    catch (error) { if (error?.code !== 'ENOENT') throw error }
  }
  if (!safeStorage.isEncryptionAvailable()) throw new Error('Electron safeStorage is unavailable for this Windows user.')

  const targetPath = join(app.getPath('userData'), 'voice-settings.json')
  if (process.env.HSC_VERIFY_VOICE_SETTINGS === '1') {
    const raw = await readFile(targetPath, 'utf8')
    const stored = JSON.parse(raw)
    const config = normalizeConfig({ ...stored, apiKey: safeStorage.decryptString(Buffer.from(stored.encryptedApiKey, 'base64')) })
    await emitResult({
      configured: true, decryptable: true, plaintextAbsent: !raw.includes(config.apiKey),
      provider: config.provider, voiceId: config.voiceId, model: config.model, region: config.region,
    })
    return
  }

  const serialized = inheritedInput ?? await readBoundedStdin()
  if (Buffer.byteLength(serialized, 'utf8') > MAX_INPUT_BYTES) throw new Error('Voice settings input exceeded the size limit.')
  const config = normalizeConfig(JSON.parse(serialized))
  await mkdir(dirname(targetPath), { recursive: true })
  const stored = {
    provider: config.provider, relayUrl: config.relayUrl, voiceId: config.voiceId,
    model: config.model, region: config.region,
    encryptedApiKey: safeStorage.encryptString(config.apiKey).toString('base64'),
  }
  const temporaryPath = `${targetPath}.${process.pid}.tmp`
  try {
    await writeFile(temporaryPath, JSON.stringify(stored), { encoding: 'utf8', flag: 'wx', mode: 0o600 })
    await rename(temporaryPath, targetPath)
  } finally {
    await unlink(temporaryPath).catch(() => undefined)
  }
  const raw = await readFile(targetPath, 'utf8')
  if (raw.includes(config.apiKey)) throw new Error('Voice credential plaintext was detected after provisioning.')
  await emitResult({ configured: true, provider: config.provider, voiceId: config.voiceId, model: config.model, region: config.region, plaintextAbsent: true })
}

main()
  .then(() => process.exit(0))
  .catch(async (error) => {
    const message = error instanceof Error ? error.message : String(error)
    const resultPath = process.env.HSC_VOICE_PROVISION_RESULT_PATH?.trim()
    if (resultPath) {
      await mkdir(dirname(resolve(resultPath)), { recursive: true }).catch(() => undefined)
      await writeFile(resolve(resultPath), JSON.stringify({ configured: false, error: message }), { encoding: 'utf8', mode: 0o600 }).catch(() => undefined)
    }
    process.stderr.write(`Voice settings provisioning failed: ${message}\n`)
    process.exit(1)
  })
