import { app, safeStorage } from 'electron'
import { lstat, mkdir, readFile, rename, unlink, writeFile } from 'node:fs/promises'
import { dirname, join, resolve } from 'node:path'
import process from 'node:process'

const MAX_INPUT_BYTES = 16 * 1024

async function readBoundedStdin() {
  const chunks = []
  let size = 0
  for await (const chunk of process.stdin) {
    size += chunk.length
    if (size > MAX_INPUT_BYTES) throw new Error('Model settings input exceeded the size limit.')
    chunks.push(chunk)
  }
  return Buffer.concat(chunks).toString('utf8')
}

async function emitResult(result) {
  const resultPath = process.env.HSC_MODEL_PROVISION_RESULT_PATH?.trim()
  const serialized = JSON.stringify(result)
  if (resultPath) {
    await mkdir(dirname(resolve(resultPath)), { recursive: true })
    await writeFile(resolve(resultPath), serialized, { encoding: 'utf8', mode: 0o600 })
  }
  process.stdout.write(serialized)
}

async function probeModel(config, model) {
  const started = Date.now()
  const response = await fetch(`${config.baseUrl}/chat/completions`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${config.apiKey}`, 'Content-Type': 'application/json' },
    body: JSON.stringify({ model, messages: [{ role: 'user', content: 'Reply with OK.' }], max_tokens: 128, stream: false }),
    signal: AbortSignal.timeout(30_000),
  })
  if (!response.ok) throw new Error(`The ${model} probe returned HTTP ${response.status}.`)
  const body = await response.json()
  if (!body?.choices?.[0]?.message?.content) throw new Error(`The ${model} probe returned no assistant message.`)
  return { model, status: response.status, latencyMs: Date.now() - started }
}

function normalizeConfig(value) {
  const apiKey = String(value?.apiKey ?? '').trim()
  const baseUrl = String(value?.baseUrl ?? '').trim().replace(/\/+$/, '')
  const model = String(value?.model ?? '').trim()
  const voiceIntentModel = String(value?.voiceIntentModel ?? '').trim()
  if (value?.provider !== 'deepseek') throw new Error('Only the DeepSeek provider is supported by this provisioner.')
  if (apiKey.length < 8) throw new Error('The API key is incomplete.')
  if (baseUrl !== 'https://api.deepseek.com') throw new Error('The DeepSeek endpoint must use the official HTTPS origin.')
  if (!/^[a-zA-Z0-9._:-]{2,120}$/.test(model) || !/^[a-zA-Z0-9._:-]{2,120}$/.test(voiceIntentModel)) {
    throw new Error('A model name is invalid.')
  }
  return { provider: 'deepseek', apiKey, baseUrl, model, voiceIntentModel }
}

async function main() {
  const targetUserData = process.env.HSC_TARGET_USER_DATA_DIR?.trim()
  if (!targetUserData) throw new Error('HSC_TARGET_USER_DATA_DIR is required.')
  const inheritedInput = process.env.HSC_PROVISION_MODEL_SETTINGS_JSON
  delete process.env.HSC_PROVISION_MODEL_SETTINGS_JSON
  app.setPath('userData', resolve(targetUserData))
  await app.whenReady()
  for (const name of ['qwen-key-rotation-required.bin','qwen-key-rotation']) {
    try { await lstat(join(app.getPath('userData'),name)); throw new Error('QWEN_ROTATION_MANAGED_SETTINGS_REQUIRED') }
    catch (error) { if (error?.code !== 'ENOENT') throw error }
  }
  if (!safeStorage.isEncryptionAvailable()) throw new Error('Electron safeStorage is unavailable for this Windows user.')

  const targetPath = join(app.getPath('userData'), 'model-settings.json')
  if (process.env.HSC_VERIFY_MODEL_SETTINGS === '1') {
    const raw = await readFile(targetPath, 'utf8')
    const stored = JSON.parse(raw)
    const config = normalizeConfig({
      ...stored,
      apiKey: safeStorage.decryptString(Buffer.from(stored.encryptedApiKey, 'base64')),
    })
    const tutorProbe = await probeModel(config, config.model)
    const intentProbe = await probeModel(config, config.voiceIntentModel)
    await emitResult({
      configured: true,
      decryptable: true,
      plaintextAbsent: !raw.includes(config.apiKey),
      provider: config.provider,
      baseUrl: config.baseUrl,
      tutorProbe,
      intentProbe,
    })
    return
  }

  const serialized = inheritedInput ?? await readBoundedStdin()
  if (Buffer.byteLength(serialized, 'utf8') > MAX_INPUT_BYTES) throw new Error('Model settings input exceeded the size limit.')
  const config = normalizeConfig(JSON.parse(serialized))
  await mkdir(dirname(targetPath), { recursive: true })
  const stored = {
    provider: config.provider,
    baseUrl: config.baseUrl,
    model: config.model,
    voiceIntentModel: config.voiceIntentModel,
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
  if (raw.includes(config.apiKey)) throw new Error('Credential plaintext was detected after provisioning.')
  await emitResult({
    configured: true,
    provider: config.provider,
    baseUrl: config.baseUrl,
    model: config.model,
    voiceIntentModel: config.voiceIntentModel,
    plaintextAbsent: true,
  })
}

main()
  .then(() => process.exit(0))
  .catch(async (error) => {
    const message = error instanceof Error ? error.message : String(error)
    const resultPath = process.env.HSC_MODEL_PROVISION_RESULT_PATH?.trim()
    if (resultPath) {
      await mkdir(dirname(resolve(resultPath)), { recursive: true }).catch(() => undefined)
      await writeFile(resolve(resultPath), JSON.stringify({ configured: false, error: message }), {
        encoding: 'utf8', mode: 0o600,
      }).catch(() => undefined)
    }
    process.stderr.write(`Model settings provisioning failed: ${message}\n`)
    process.exit(1)
  })
