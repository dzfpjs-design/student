/* Local device acceptance harness. Captured frames stay on the test machine. */
import { app, BrowserWindow, session } from 'electron'
import fs from 'node:fs'
import path from 'node:path'

const output = process.argv[process.argv.indexOf('--output') + 1]
if (!output) throw new Error('--output is required')
const reportIndex = process.argv.indexOf('--report')
const report = reportIndex >= 0 ? process.argv[reportIndex + 1] : `${output}.json`
const patternIndex = process.argv.indexOf('--device-pattern')
const devicePattern = patternIndex >= 0 ? process.argv[patternIndex + 1] : 'L523|Fic760|document|scanner|高拍'

async function capture() {
  await app.whenReady()
  session.defaultSession.setPermissionRequestHandler((_contents, permission, callback) => callback(permission === 'media'))
  const window = new BrowserWindow({ show: false, webPreferences: { contextIsolation: false, nodeIntegration: true } })
  const harnessHtml = path.join(path.dirname(report), '.camera-capture-harness.html')
  try {
    fs.mkdirSync(path.dirname(harnessHtml), { recursive: true })
    fs.writeFileSync(harnessHtml, '<meta charset="utf-8"><video autoplay muted playsinline></video>', { mode: 0o600 })
    await window.loadFile(harnessHtml)
    const script = window.webContents.executeJavaScript(`(async () => {
      const permissionStream = await navigator.mediaDevices.getUserMedia({ audio: false, video: true })
      permissionStream.getTracks().forEach((track) => track.stop())
      const devices = await navigator.mediaDevices.enumerateDevices()
      const matcher = new RegExp(${JSON.stringify(devicePattern)}, 'i')
      const device = devices.find((item) => item.kind === 'videoinput' && matcher.test(item.label))
      if (!device) {
        const labels = devices.filter((item) => item.kind === 'videoinput').map((item) => item.label || 'unlabeled')
        throw new Error('document_camera_not_found:' + labels.join('|'))
      }
      const stream = await Promise.race([
        navigator.mediaDevices.getUserMedia({ audio: false, video: { deviceId: { exact: device.deviceId }, width: { ideal: 3840 }, height: { ideal: 2160 } } }),
        new Promise((_, reject) => setTimeout(() => reject(new Error('document_camera_open_timeout')), 12000)),
      ])
      const track = stream.getVideoTracks()[0]
      const video = document.querySelector('video')
      video.srcObject = stream
      await video.play()
      await new Promise((resolve) => setTimeout(resolve, 1200))
      const canvas = document.createElement('canvas')
      canvas.width = video.videoWidth; canvas.height = video.videoHeight
      canvas.getContext('2d').drawImage(video, 0, 0)
      const dataUrl = canvas.toDataURL('image/jpeg', 0.94)
      const settings = track.getSettings()
      const identityBytes = new TextEncoder().encode(device.deviceId)
      const identityDigest = await crypto.subtle.digest('SHA-256', identityBytes)
      const systemIdHash = Array.from(new Uint8Array(identityDigest), (byte) => byte.toString(16).padStart(2, '0')).join('')
      track.stop(); stream.getTracks().forEach((item) => item.stop())
      return { dataUrl, width: canvas.width, height: canvas.height, deviceLabel: device.label, systemIdHash, settings }
    })()`, true)
    const result = await Promise.race([script, new Promise((_, reject) => setTimeout(() => reject(new Error('renderer_capture_timeout')), 18000))])
    const encoded = result.dataUrl.split(',', 2)[1]
    fs.mkdirSync(path.dirname(output), { recursive: true })
    fs.writeFileSync(output, Buffer.from(encoded, 'base64'), { mode: 0o600 })
    const { deviceId: _deviceId, groupId: _groupId, ...safeSettings } = result.settings
    const summary = { status: 'captured', width: result.width, height: result.height, deviceLabel: result.deviceLabel, systemIdHash: result.systemIdHash, settings: safeSettings }
    fs.writeFileSync(report, JSON.stringify(summary), { mode: 0o600 })
    process.stdout.write(JSON.stringify(summary) + '\n')
  } finally {
    window.destroy()
    fs.rmSync(harnessHtml, { force: true })
  }
}

Promise.race([
  capture(),
  new Promise((_, reject) => setTimeout(() => reject(new Error('capture_process_timeout')), 30000)),
])
  .then(() => process.exit(0))
  .catch((error) => {
    const summary = { status: 'failed', error: error.message || String(error) }
    fs.mkdirSync(path.dirname(report), { recursive: true })
    fs.writeFileSync(report, JSON.stringify(summary), { mode: 0o600 })
    process.stderr.write(`${error.name || 'Error'}:${summary.error}\n`)
    process.exit(1)
  })
