/* Windows target probe: 4K frame stays in memory and only derived OCR is persisted. */
import { spawnSync } from 'node:child_process'
import { app, BrowserWindow, session } from 'electron'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const argument = (name, fallback = '') => {
  const index = process.argv.indexOf(name)
  return index >= 0 ? process.argv[index + 1] : fallback
}

const reportPath = argument('--report')
const requestedWidth = Number.parseInt(argument('--width', '4160'), 10)
const requestedHeight = Number.parseInt(argument('--height', '3120'), 10)
if (!reportPath) throw new Error('--report is required')

async function probe() {
  await app.whenReady()
  session.defaultSession.setPermissionRequestHandler((_contents, permission, callback) => callback(permission === 'media'))
  const window = new BrowserWindow({ show: false, webPreferences: { contextIsolation: false } })
  const harnessPath = path.join(path.dirname(reportPath), '.document-camera-ocr-probe.html')
  try {
    fs.mkdirSync(path.dirname(reportPath), { recursive: true })
    fs.writeFileSync(harnessPath, '<meta charset="utf-8"><video autoplay muted playsinline></video>', { mode: 0o600 })
    await window.loadFile(harnessPath)
    const capture = await window.webContents.executeJavaScript(`(async () => {
      const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms))
      const permission = await navigator.mediaDevices.getUserMedia({ audio: false, video: true })
      permission.getTracks().forEach((track) => track.stop())
      await wait(500)
      const devices = await navigator.mediaDevices.enumerateDevices()
      const device = devices.find((item) => item.kind === 'videoinput' && /^Usb HDCamera(?: |$)/i.test(item.label))
      if (!device) throw new Error('DOCUMENT_CAMERA_NOT_FOUND')
      const stream = await navigator.mediaDevices.getUserMedia({ audio: false, video: {
        deviceId: { exact: device.deviceId }, width: { exact: ${requestedWidth} }, height: { exact: ${requestedHeight} },
        resizeMode: { ideal: 'none' }, frameRate: { ideal: 5, max: 10 },
      } })
      try {
        const track = stream.getVideoTracks()[0]
        const capabilities = track.getCapabilities ? track.getCapabilities() : {}
        if (Array.isArray(capabilities.focusMode) && capabilities.focusMode.includes('continuous')) {
          try { await track.applyConstraints({ advanced: [{ focusMode: 'continuous' }] }) } catch {}
        }
        const video = document.querySelector('video')
        video.srcObject = stream
        await video.play()
        for (let attempt = 0; attempt < 100 && (!video.videoWidth || video.readyState < 2); attempt += 1) await wait(100)
        if (!video.videoWidth) throw new Error('FIRST_FRAME_TIMEOUT')
        await wait(1600)
        const canvas = document.createElement('canvas')
        canvas.width = video.videoWidth
        canvas.height = video.videoHeight
        canvas.getContext('2d').drawImage(video, 0, 0)
        const settings = track.getSettings()
        return {
          deviceLabel: device.label,
          width: canvas.width,
          height: canvas.height,
          settings: { width: settings.width ?? null, height: settings.height ?? null, frameRate: settings.frameRate ?? null, focusMode: settings.focusMode ?? null },
          imageBase64: canvas.toDataURL('image/jpeg', 0.94).split(',', 2)[1],
        }
      } finally {
        stream.getTracks().forEach((track) => track.stop())
      }
    })()`)

    const repository = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..', '..')
    const python = path.join(repository, '.venv', 'Scripts', 'python.exe')
    const helper = path.join(repository, 'scripts', 'probe_document_ocr_frame.py')
    const child = spawnSync(python, [helper], {
      input: capture.imageBase64,
      encoding: 'utf8',
      env: { ...process.env, PYTHONUTF8: '1' },
      maxBuffer: 8 * 1024 * 1024,
      timeout: 180_000,
    })
    if (child.status !== 0) throw new Error(`OCR_FAILED:${(child.stderr || child.stdout || '').slice(-1200)}`)
    const report = {
      status: 'passed',
      capture: { device_label: capture.deviceLabel, width: capture.width, height: capture.height, settings: capture.settings },
      ocr: JSON.parse(child.stdout.trim()),
      raw_media_saved: false,
      media_uploaded: false,
    }
    fs.writeFileSync(reportPath, JSON.stringify(report), { mode: 0o600 })
    process.stdout.write(`${JSON.stringify(report)}\n`)
  } finally {
    window.destroy()
    fs.rmSync(harnessPath, { force: true })
  }
}

Promise.race([
  probe(),
  new Promise((_, reject) => setTimeout(() => reject(new Error('PROBE_TIMEOUT')), 210_000)),
]).then(() => process.exit(0)).catch((error) => {
  const report = { status: 'failed', error: error?.message ?? String(error), raw_media_saved: false, media_uploaded: false }
  fs.mkdirSync(path.dirname(reportPath), { recursive: true })
  fs.writeFileSync(reportPath, JSON.stringify(report), { mode: 0o600 })
  process.stderr.write(`${JSON.stringify(report)}\n`)
  process.exit(1)
})
