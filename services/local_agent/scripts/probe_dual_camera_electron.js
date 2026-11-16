/* Interactive Windows acceptance probe. It stores metadata only, never frames. */
import { app, BrowserWindow, session } from 'electron'
import fs from 'node:fs'
import path from 'node:path'

const argument = (name, fallback) => {
  const index = process.argv.indexOf(name)
  return index >= 0 ? process.argv[index + 1] : fallback
}

const reportPath = argument('--report', '')
const cameraPattern = argument('--camera-pattern', 'Integrated Camera')
const documentPattern = argument('--document-pattern', 'Usb HDCamera|Fic760|L523|document|scanner|高拍')
const documentWidth = Number.parseInt(argument('--document-width', '1280'), 10)
const documentHeight = Number.parseInt(argument('--document-height', '960'), 10)
if (!reportPath) throw new Error('--report is required')

async function probe() {
  await app.whenReady()
  session.defaultSession.setPermissionRequestHandler((_contents, permission, callback) => callback(permission === 'media'))
  const window = new BrowserWindow({ show: false, webPreferences: { contextIsolation: false, nodeIntegration: true } })
  const harnessPath = path.join(path.dirname(reportPath), '.dual-camera-probe.html')
  try {
    fs.mkdirSync(path.dirname(reportPath), { recursive: true })
    fs.writeFileSync(harnessPath, '<meta charset="utf-8"><video id="camera" autoplay muted playsinline></video><video id="document" autoplay muted playsinline></video>', { mode: 0o600 })
    await window.loadFile(harnessPath)
    const result = await window.webContents.executeJavaScript(`(async () => {
      const permission = await navigator.mediaDevices.getUserMedia({ audio: false, video: true })
      permission.getTracks().forEach((track) => track.stop())
      const devices = (await navigator.mediaDevices.enumerateDevices()).filter((item) => item.kind === 'videoinput')
      const select = (pattern) => devices.find((item) => new RegExp(pattern, 'i').test(item.label))
      const camera = select(${JSON.stringify(cameraPattern)})
      const documentCamera = select(${JSON.stringify(documentPattern)})
      if (!camera || !documentCamera || camera.deviceId === documentCamera.deviceId) {
        throw new Error('dual_device_selection_failed:' + devices.map((item) => item.label || 'unlabeled').join('|'))
      }
      const opened = []
      const open = async (device, elementId, width, height, frameRate) => {
        const stream = await Promise.race([
          navigator.mediaDevices.getUserMedia({ audio: false, video: {
            deviceId: { exact: device.deviceId }, width: { ideal: width }, height: { ideal: height },
            frameRate: { ideal: frameRate, max: 15 }, resizeMode: { ideal: 'none' },
          } }),
          new Promise((_, reject) => setTimeout(() => reject(new Error(elementId + '_open_timeout')), 15000)),
        ])
        opened.push(stream)
        const video = document.getElementById(elementId)
        video.srcObject = stream
        await video.play()
        await new Promise((resolve, reject) => {
          const deadline = setTimeout(() => reject(new Error(elementId + '_first_frame_timeout')), 15000)
          const confirm = () => {
            if (video.readyState >= HTMLMediaElement.HAVE_CURRENT_DATA && video.videoWidth > 0 && video.videoHeight > 0) {
              clearTimeout(deadline); resolve()
            }
          }
          video.addEventListener('loadeddata', confirm, { once: true })
          video.addEventListener('resize', confirm, { once: true })
          video.addEventListener('timeupdate', confirm, { once: true })
          const poll = setInterval(() => {
            confirm()
            if (video.videoWidth > 0 && video.videoHeight > 0) clearInterval(poll)
          }, 100)
          confirm()
        })
        return { stream, video, track: stream.getVideoTracks()[0] }
      }
      try {
        const first = await open(camera, 'camera', 1280, 720, 10)
        const second = await open(documentCamera, 'document', ${documentWidth}, ${documentHeight}, 5)
        const before = { camera: first.video.currentTime, document: second.video.currentTime }
        await new Promise((resolve) => setTimeout(resolve, 2500))
        const safe = (device, value) => {
          const settings = value.track.getSettings()
          return {
            label: device.label,
            width: value.video.videoWidth,
            height: value.video.videoHeight,
            ready_state: value.video.readyState,
            media_time: value.video.currentTime,
            media_time_advanced: value.video.currentTime > before[value.video.id] + 0.01,
            track_state: value.track.readyState,
            settings: { width: settings.width ?? null, height: settings.height ?? null, frame_rate: settings.frameRate ?? null },
          }
        }
        return { status: 'passed', requested_document_mode: { width: ${documentWidth}, height: ${documentHeight} }, camera: safe(camera, first), document_camera: safe(documentCamera, second), enumerated_labels: devices.map((item) => item.label || 'unlabeled') }
      } finally {
        opened.forEach((stream) => stream.getTracks().forEach((track) => track.stop()))
      }
    })()`, true)
    fs.writeFileSync(reportPath, JSON.stringify(result), { mode: 0o600 })
    process.stdout.write(JSON.stringify(result) + '\n')
  } finally {
    window.destroy()
    fs.rmSync(harnessPath, { force: true })
  }
}

Promise.race([
  probe(),
  new Promise((_, reject) => setTimeout(() => reject(new Error('dual_probe_process_timeout')), 45000)),
]).then(() => process.exit(0)).catch((error) => {
  const result = { status: 'failed', error: error.message || String(error) }
  fs.mkdirSync(path.dirname(reportPath), { recursive: true })
  fs.writeFileSync(reportPath, JSON.stringify(result), { mode: 0o600 })
  process.stderr.write(`${error.name || 'Error'}:${result.error}\n`)
  process.exit(1)
})
