import { app, safeStorage } from 'electron'
import { createHash } from 'node:crypto'
import { lstat, mkdir, readFile, rename, unlink, writeFile } from 'node:fs/promises'
import { dirname, join, resolve } from 'node:path'
import process from 'node:process'

const MAX_INPUT_BYTES = 16 * 1024

async function readBoundedStdin() {
  const chunks = []
  let size = 0
  for await (const chunk of process.stdin) {
    size += chunk.length
    if (size > MAX_INPUT_BYTES) throw new Error('ASR settings input exceeded the size limit.')
    chunks.push(chunk)
  }
  return Buffer.concat(chunks).toString('utf8')
}

async function emitResult(result) {
  const resultPath = process.env.HSC_ASR_PROVISION_RESULT_PATH?.trim()
  const serialized = JSON.stringify(result)
  if (resultPath) {
    await mkdir(dirname(resolve(resultPath)), { recursive: true })
    await writeFile(resolve(resultPath), serialized, { encoding: 'utf8', mode: 0o600 })
  }
  process.stdout.write(serialized)
}

function normalizeConfig(value) {
  const apiKey = String(value?.apiKey ?? '').trim()
  const baseUrl = String(value?.baseUrl ?? '').trim().replace(/\/+$/, '')
  const model = String(value?.model ?? '').trim()
  if (value?.provider !== 'dashscope') throw new Error('Only the DashScope ASR provider is supported by this provisioner.')
  if (apiKey.length < 8) throw new Error('The ASR API key is incomplete.')
  if (baseUrl !== 'https://dashscope.aliyuncs.com/compatible-mode/v1') throw new Error('The DashScope endpoint must use the approved HTTPS origin.')
  if (model !== 'qwen3-asr-flash') throw new Error('The approved ASR model is qwen3-asr-flash.')
  return { provider: 'dashscope', apiKey, baseUrl, model }
}

async function probeAsr(config, wavPath) {
  const wav = await readFile(resolve(wavPath))
  if (wav.length < 44 || wav.length > 10 * 1024 * 1024 || wav.subarray(0, 4).toString('ascii') !== 'RIFF') {
    throw new Error('The ASR probe fixture must be a bounded WAV file.')
  }
  const started = Date.now()
  const response = await fetch(`${config.baseUrl}/chat/completions`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${config.apiKey}`, 'Content-Type': 'application/json' },
    body: JSON.stringify({
      model: config.model,
      messages: [{ role: 'user', content: [{ type: 'input_audio', input_audio: { data: `data:audio/wav;base64,${wav.toString('base64')}` } }] }],
      stream: false,
      asr_options: { enable_itn: true },
    }),
    signal: AbortSignal.timeout(60_000),
  })
  if (!response.ok) throw new Error(`The ASR probe returned HTTP ${response.status}.`)
  const body = await response.json()
  const transcript = String(body?.choices?.[0]?.message?.content ?? '').trim()
  if (!transcript) throw new Error('The ASR probe returned no transcript.')
  return {
    status: response.status,
    latencyMs: Date.now() - started,
    transcriptChars: transcript.length,
    transcriptSha256: createHash('sha256').update(transcript).digest('hex'),
    audioSha256: createHash('sha256').update(wav).digest('hex'),
  }
}

async function main() {
  const targetUserData = process.env.HSC_TARGET_USER_DATA_DIR?.trim()
  if (!targetUserData) throw new Error('HSC_TARGET_USER_DATA_DIR is required.')
  const inheritedInput = process.env.HSC_PROVISION_ASR_SETTINGS_JSON
  delete process.env.HSC_PROVISION_ASR_SETTINGS_JSON
  app.setPath('userData', resolve(targetUserData))
  await app.whenReady()
  for (const name of ['qwen-key-rotation-required.bin','qwen-key-rotation']) {
    try { await lstat(join(app.getPath('userData'),name)); throw new Error('QWEN_ROTATION_MANAGED_SETTINGS_REQUIRED') }
    catch (error) { if (error?.code !== 'ENOENT') throw error }
  }
  if (!safeStorage.isEncryptionAvailable()) throw new Error('Electron safeStorage is unavailable for this Windows user.')

  const targetPath = join(app.getPath('userData'), 'asr-settings.json')
  if (process.env.HSC_VERIFY_ASR_SETTINGS === '1') {
    const raw = await readFile(targetPath, 'utf8')
    const stored = JSON.parse(raw)
    const config = normalizeConfig({
      ...stored,
      apiKey: safeStorage.decryptString(Buffer.from(stored.encryptedApiKey, 'base64')),
    })
    const probePath = process.env.HSC_ASR_PROBE_WAV_PATH?.trim()
    const probe = probePath ? await probeAsr(config, probePath) : undefined
    await emitResult({
      configured: true,
      decryptable: true,
      plaintextAbsent: !raw.includes(config.apiKey),
      provider: config.provider,
      baseUrl: config.baseUrl,
      model: config.model,
      ...(probe ? { probe } : {}),
    })
    return
  }

  const serialized = inheritedInput ?? await readBoundedStdin()
  if (Buffer.byteLength(serialized, 'utf8') > MAX_INPUT_BYTES) throw new Error('ASR settings input exceeded the size limit.')
  const config = normalizeConfig(JSON.parse(serialized))
  await mkdir(dirname(targetPath), { recursive: true })
  const stored = {
    provider: config.provider,
    baseUrl: config.baseUrl,
    model: config.model,
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
  if (raw.includes(config.apiKey)) throw new Error('ASR credential plaintext was detected after provisioning.')
  await emitResult({ configured: true, provider: config.provider, baseUrl: config.baseUrl, model: config.model, plaintextAbsent: true })
}

main()
  .then(() => process.exit(0))
  .catch(async (error) => {
    const message = error instanceof Error ? error.message : String(error)
    const resultPath = process.env.HSC_ASR_PROVISION_RESULT_PATH?.trim()
    if (resultPath) {
      await mkdir(dirname(resolve(resultPath)), { recursive: true }).catch(() => undefined)
      await writeFile(resolve(resultPath), JSON.stringify({ configured: false, error: message }), { encoding: 'utf8', mode: 0o600 }).catch(() => undefined)
    }
    process.stderr.write(`ASR settings provisioning failed: ${message}\n`)
    process.exit(1)
  })
