/*
 * Probes one document-camera mode in a fresh Electron process.
 * It never serializes or writes a camera frame; stdout contains settings only.
 */
import { app, BrowserWindow, session } from 'electron'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

function argument(name, fallback = '') {
  const index = process.argv.indexOf(name)
  return index >= 0 ? process.argv[index + 1] : fallback
}

const requestedWidth = Number.parseInt(argument('--width'), 10)
const requestedHeight = Number.parseInt(argument('--height'), 10)
const devicePattern = argument('--device-pattern', '^Usb HDCamera')
const openTimeoutMs = Number.parseInt(argument('--open-timeout-ms', '15000'), 10)

if (!Number.isFinite(requestedWidth) || !Number.isFinite(requestedHeight)) {
  throw new Error('--width and --height are required')
}

async function probe() {
  await app.whenReady()
  session.defaultSession.setPermissionRequestHandler((_contents, permission, callback) => {
    callback(permission === 'media')
  })
  const window = new BrowserWindow({
    show: false,
    webPreferences: { contextIsolation: false, nodeIntegration: false },
  })
  const harnessPath = path.join(os.tmpdir(), `hsc-camera-mode-${process.pid}.html`)
  try {
    fs.writeFileSync(harnessPath, '<meta charset="utf-8"><video autoplay muted playsinline></video>', { mode: 0o600 })
    await window.loadFile(harnessPath)
    return await window.webContents.executeJavaScript(`(async () => {
      const matcher = new RegExp(${JSON.stringify(devicePattern)}, 'i')
      const permission = await navigator.mediaDevices.getUserMedia({ audio: false, video: true })
      permission.getTracks().forEach((track) => track.stop())
      await new Promise((resolve) => setTimeout(resolve, 500))
      const devices = await navigator.mediaDevices.enumerateDevices()
      const device = devices.find((item) => item.kind === 'videoinput' && matcher.test(item.label))
      if (!device) throw new Error('DOCUMENT_CAMERA_NOT_FOUND')
      const stream = await Promise.race([
        navigator.mediaDevices.getUserMedia({
          audio: false,
          video: {
            deviceId: { exact: device.deviceId },
            width: { ideal: ${requestedWidth} },
            height: { ideal: ${requestedHeight} },
            resizeMode: { ideal: 'none' },
            frameRate: { ideal: 5, max: 10 },
          },
        }),
        new Promise((_, reject) => setTimeout(() => reject(new Error('OPEN_TIMEOUT')), ${openTimeoutMs})),
      ])
      try {
        const video = document.querySelector('video')
        video.srcObject = stream
        await video.play()
        await Promise.race([
          new Promise((resolve) => {
            if (video.readyState >= HTMLMediaElement.HAVE_CURRENT_DATA && video.videoWidth > 0) return resolve()
            const ready = () => {
              if (video.readyState >= HTMLMediaElement.HAVE_CURRENT_DATA && video.videoWidth > 0) resolve()
            }
            video.addEventListener('loadeddata', ready, { once: true })
            video.addEventListener('resize', ready, { once: true })
            if (video.requestVideoFrameCallback) video.requestVideoFrameCallback(() => ready())
          }),
          new Promise((_, reject) => setTimeout(() => reject(new Error('FIRST_FRAME_TIMEOUT')), 10000)),
        ])
        const track = stream.getVideoTracks()[0]
        const settings = track.getSettings()
        const capabilities = track.getCapabilities ? track.getCapabilities() : {}
        return {
          status: 'ready',
          requested: { width: ${requestedWidth}, height: ${requestedHeight} },
          delivered: { width: video.videoWidth, height: video.videoHeight },
          track: {
            width: settings.width ?? null,
            height: settings.height ?? null,
            frameRate: settings.frameRate ?? null,
            resizeMode: settings.resizeMode ?? null,
            focusMode: settings.focusMode ?? null,
          },
          advertised: {
            width: capabilities.width ? { min: capabilities.width.min, max: capabilities.width.max } : null,
            height: capabilities.height ? { min: capabilities.height.min, max: capabilities.height.max } : null,
            frameRate: capabilities.frameRate ? { min: capabilities.frameRate.min, max: capabilities.frameRate.max } : null,
          },
          deviceLabel: device.label,
        }
      } finally {
        stream.getTracks().forEach((track) => track.stop())
      }
    })()`)
  } finally {
    window.destroy()
    fs.rmSync(harnessPath, { force: true })
  }
}

Promise.race([
  probe(),
  new Promise((_, reject) => setTimeout(() => reject(new Error('PROCESS_TIMEOUT')), openTimeoutMs + 15000)),
])
  .then((result) => {
    process.stdout.write(`${JSON.stringify(result)}\n`)
    app.quit()
  })
  .catch((error) => {
    process.stderr.write(`${JSON.stringify({ status: 'failed', requested: { width: requestedWidth, height: requestedHeight }, error: error?.message ?? String(error) })}\n`)
    app.exit(1)
  })
