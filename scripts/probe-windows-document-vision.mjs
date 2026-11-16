import { app, safeStorage } from 'electron'
import { readFile } from 'node:fs/promises'
import { join, resolve } from 'node:path'
import { spawn } from 'node:child_process'

// Read the authorized Windows user's encrypted speech credential only into the
// isolated probe child environment. Never print or persist plaintext secrets.
async function main() {
app.setPath('userData', join(process.env.APPDATA, 'home-study-companion'))
await app.whenReady()
let stage = 'load_encrypted_config'
try {
  const profile = join(process.env.APPDATA, 'home-study-companion')
  const learning = process.argv.includes('--learning')
  const config = JSON.parse(await readFile(join(profile, learning ? 'model-settings.json' : 'asr-settings.json'), 'utf8'))
  stage = 'check_safe_storage'
  if (!safeStorage.isEncryptionAvailable() || config.provider !== (learning ? 'deepseek' : 'dashscope')) throw new Error('credential_unavailable')
  if (config.baseUrl !== (learning ? 'https://api.deepseek.com' : 'https://dashscope.aliyuncs.com/compatible-mode/v1')) throw new Error('endpoint_not_approved')
  stage = 'decrypt_authorized_key'
  const apiKey = safeStorage.decryptString(Buffer.from(config.encryptedApiKey, 'base64'))
  stage = 'launch_isolated_probe'
  const child = spawn(resolve('.venv/Scripts/python.exe'), [`services/local_agent/scripts/probe_document_${learning ? 'learning' : 'vision'}.py`], {
    cwd: process.cwd(), windowsHide: true,
    env: { ...process.env, PYTHONPATH: resolve('services/local_agent/src'),
      ...(learning ? { HSC_MODEL_PROVIDER: 'deepseek', HSC_MODEL_BASE_URL: config.baseUrl,
        HSC_MODEL_API_KEY: apiKey, HSC_MODEL_NAME: config.model, HSC_MODEL_TIMEOUT_SECONDS: '45' } : {
      HSC_ASR_PROVIDER: 'dashscope', HSC_ASR_BASE_URL: config.baseUrl,
      HSC_ASR_API_KEY: apiKey,
      HSC_DOCUMENT_VISION_ENABLED: '1', HSC_DOCUMENT_VISION_BASE_URL: config.baseUrl,
      HSC_DOCUMENT_VISION_CREDENTIAL_SOURCE: 'asr_same_origin' }) },
    stdio: ['ignore', 'pipe', 'ignore']
  })
  let output = ''
  child.stdout.on('data', chunk => { if (output.length < 16000) output += chunk.toString() })
  child.on('error', () => { console.log(JSON.stringify({ status: 'blocked', reason: 'probe_launch_failed' })); app.exit(1) })
  child.on('exit', code => {
    for (const line of output.split(/\r?\n/)) {
      try {
        const result = JSON.parse(line)
        const safe = Object.fromEntries(['status', 'source', 'reason', 'model', 'total_tokens', 'latency_ms', 'token', 'latency', 'error_code', 'schema_summary', 'gate_passed', 'block_count', 'observations', 'hypotheses', 'specific_focus_count', 'memory_count', 'mastery_updated']
          .filter(key => key in result).map(key => [key, result[key]]))
        if (Object.keys(safe).length) console.log(JSON.stringify(safe))
      } catch { /* Provider and diagnostic text are never echoed. */ }
    }
    app.exit(code ?? 1)
  })
} catch {
  console.log(JSON.stringify({ status: 'blocked', reason: stage }))
  app.exit(1)
}
}
void main()
