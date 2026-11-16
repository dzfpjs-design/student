/* Local microphone acceptance harness. No audio samples are persisted. */
import { app, BrowserWindow, session } from 'electron'
import fs from 'node:fs'
import path from 'node:path'

const reportIndex = process.argv.indexOf('--report')
const report = reportIndex >= 0 ? process.argv[reportIndex + 1] : null
if (!report) throw new Error('--report is required')
const patternIndex = process.argv.indexOf('--device-pattern')
const devicePattern = patternIndex >= 0 ? process.argv[patternIndex + 1] : ''
const durationIndex = process.argv.indexOf('--duration-ms')
const durationMs = durationIndex >= 0 ? Number(process.argv[durationIndex + 1]) : 6_000

async function probe() {
  await app.whenReady()
  session.defaultSession.setPermissionRequestHandler((_contents, permission, callback) => callback(permission === 'media'))
  const window = new BrowserWindow({ show: false, webPreferences: { contextIsolation: false, nodeIntegration: true } })
  const harnessHtml = path.join(path.dirname(report), '.microphone-probe-harness.html')
  try {
    fs.mkdirSync(path.dirname(harnessHtml), { recursive: true })
    fs.writeFileSync(harnessHtml, '<meta charset="utf-8"><title>microphone probe</title>', { mode: 0o600 })
    await window.loadFile(harnessHtml)
    const result = await window.webContents.executeJavaScript(`(async () => {
      try {
      const initial = await navigator.mediaDevices.getUserMedia({ audio: true, video: false })
      const initialTrack = initial.getAudioTracks()[0]
      const devices = await navigator.mediaDevices.enumerateDevices()
      let selected = null
      if (${JSON.stringify(devicePattern)}) {
        const matcher = new RegExp(${JSON.stringify(devicePattern)}, 'i')
        selected = devices.find((item) => item.kind === 'audioinput' && matcher.test(item.label)) || null
      }
      initial.getTracks().forEach((track) => track.stop())
      const constraints = selected
        ? { audio: { deviceId: { exact: selected.deviceId }, channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true }, video: false }
        : { audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true }, video: false }
      const stream = await navigator.mediaDevices.getUserMedia(constraints)
      const track = stream.getAudioTracks()[0]
      const context = new AudioContext()
      const source = context.createMediaStreamSource(stream)
      const processor = context.createScriptProcessor(4096, 1, 1)
      let squaredSum = 0
      let sampleCount = 0
      let peak = 0
      let chunks = 0
      let speechChunks = 0
      processor.onaudioprocess = (event) => {
        const samples = event.inputBuffer.getChannelData(0)
        let chunkEnergy = 0
        let chunkPeak = 0
        for (const sample of samples) {
          const magnitude = Math.abs(sample)
          squaredSum += sample * sample
          chunkEnergy += sample * sample
          sampleCount += 1
          if (magnitude > peak) peak = magnitude
          if (magnitude > chunkPeak) chunkPeak = magnitude
        }
        chunks += 1
        if (Math.sqrt(chunkEnergy / Math.max(samples.length, 1)) >= 0.012) speechChunks += 1
      }
      source.connect(processor)
      processor.connect(context.destination)
      await new Promise((resolve) => setTimeout(resolve, ${Math.max(1_000, Math.min(durationMs, 20_000))}))
      processor.disconnect(); source.disconnect(); stream.getTracks().forEach((item) => item.stop()); await context.close()
      const identityBytes = new TextEncoder().encode(track.getSettings().deviceId || selected?.deviceId || '')
      const identityDigest = await crypto.subtle.digest('SHA-256', identityBytes)
      const systemIdHash = Array.from(new Uint8Array(identityDigest), (byte) => byte.toString(16).padStart(2, '0')).join('')
      const settings = track.getSettings()
      const labels = devices.filter((item) => item.kind === 'audioinput').map((item) => item.label || 'unlabeled')
      return {
        selectedLabel: track.label,
        requestedPattern: ${JSON.stringify(devicePattern)},
        systemIdHash,
        settings: { channelCount: settings.channelCount, sampleRate: settings.sampleRate, sampleSize: settings.sampleSize,
          echoCancellation: settings.echoCancellation, noiseSuppression: settings.noiseSuppression, autoGainControl: settings.autoGainControl },
        metrics: { rms: sampleCount ? Math.sqrt(squaredSum / sampleCount) : 0, peak, chunks, speechChunks },
        availableLabels: labels,
      }
      } catch (error) {
        return { probeError: {
          name: typeof error?.name === 'string' ? error.name : '',
          message: typeof error?.message === 'string' ? error.message : String(error),
          code: typeof error?.code === 'number' || typeof error?.code === 'string' ? error.code : null,
          constraint: typeof error?.constraint === 'string' ? error.constraint : null,
        } }
      }
    })()`, true)
    if (result?.probeError) {
      const failure = new Error(result.probeError.message || 'microphone_probe_failed')
      failure.name = result.probeError.name || 'MediaDeviceError'
      failure.code = result.probeError.code
      failure.constraint = result.probeError.constraint
      throw failure
    }
    const summary = { status: 'completed', ...result }
    fs.writeFileSync(report, JSON.stringify(summary), { mode: 0o600 })
  } finally {
    window.destroy()
    fs.rmSync(harnessHtml, { force: true })
  }
}

Promise.race([
  probe(),
  new Promise((_, reject) => setTimeout(() => reject(new Error('microphone_probe_timeout')), 30_000)),
]).then(() => process.exit(0)).catch((error) => {
  const summary = {
    status: 'failed',
    error: error?.message || String(error),
    error_name: error?.name || null,
    error_code: typeof error?.code === 'number' || typeof error?.code === 'string' ? error.code : null,
    error_keys: error && typeof error === 'object' ? Object.keys(error).slice(0, 12) : [],
  }
  fs.mkdirSync(path.dirname(report), { recursive: true })
  fs.writeFileSync(report, JSON.stringify(summary), { mode: 0o600 })
  process.exit(1)
})
