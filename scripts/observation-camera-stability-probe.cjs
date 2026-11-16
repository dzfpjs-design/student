// Real Windows L523 + current mounted LocalObservation + isolated actual API.
// No getUserMedia/API mocks, cloud credentials, saved pixels or family data.
const { app, BrowserWindow, powerMonitor } = require('electron')
const { spawn, execFileSync } = require('node:child_process')
const fs = require('node:fs')
const http = require('node:http')
const path = require('node:path')
const root = path.resolve(process.argv[2] || '')
if (path.dirname(root) !== 'C:\\HSC\\diagnostics' || !path.basename(root).startsWith('hsc-camera-stability-')) process.exit(2)
const configuration = fs.existsSync(path.join(root, 'probe-config.json')) ? JSON.parse(fs.readFileSync(path.join(root, 'probe-config.json'))) : {}
const soakSeconds = configuration.soak_seconds ?? 180
const dualSeconds = configuration.dual_seconds ?? 0
if (!Number.isInteger(soakSeconds) || soakSeconds < 180 || soakSeconds > 1800 || !Number.isInteger(dualSeconds) || dualSeconds < 0 || dualSeconds > 120) process.exit(2)
const captureBackend = configuration.capture_backend ?? 'default'
if (!['default', 'directshow'].includes(captureBackend)) process.exit(2)
if (captureBackend === 'directshow') app.commandLine.appendSwitch('disable-features', 'MediaFoundationVideoCapture')
const hardwareAcceleration = configuration.hardware_acceleration ?? true
if (typeof hardwareAcceleration !== 'boolean') process.exit(2)
if (!hardwareAcceleration) app.disableHardwareAcceleration()
app.setPath('userData', path.join(root, 'profile'))
app.on('window-all-closed', () => {})
const python = 'C:\\HSC\\candidates\\refactor-v83-key-loss-20261003-r5\\.venv\\Scripts\\python.exe'
const runtimeScript = path.join(root, 'observation-camera-stability-runtime.py')
let win, backend, server, finished = false, runtime
const state = { status: 'running', stage: 'starting', physical_camera: 'L523', cloud_calls: 0, pixels_saved: false, samples: [], recoveries: [], spontaneous_recoveries: [], power_events: [] }
state.capture_backend_diagnostic = captureBackend
state.hardware_acceleration_diagnostic = hardwareAcceleration
const save = () => fs.writeFileSync(path.join(root, 'result.json'), JSON.stringify(state, null, 2))
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms))
async function finish(code) {
  if (finished) return
  finished = true
  if (win && !win.isDestroyed()) {
    await win.webContents.executeJavaScript('window.__cameraProbe?.close()').catch(() => {})
    await sleep(500)
    win.destroy()
  }
  if (server) await new Promise(resolve => server.close(resolve))
  if (backend && backend.exitCode === null) {
    backend.kill()
    await Promise.race([new Promise(resolve => backend.once('exit', resolve)), sleep(3000)])
  }
  state.backend_closed = !backend || backend.exitCode !== null || backend.signalCode !== null
  try { state.persisted = JSON.parse(execFileSync(python, [runtimeScript, root, '--inspect'], { timeout: 15000 }).toString()) }
  catch { state.persistence_error = 'INSPECTION_FAILED'; code = 2 }
  state.status = code ? 'failed' : 'passed'
  state.owned_window_closed = !win || win.isDestroyed()
  save()
  app.exit(code)
}
setTimeout(() => { state.failure = 'PROBE_DEADLINE'; void finish(2) }, (soakSeconds + dualSeconds + 240) * 1000).unref()
app.whenReady().then(async () => {
  state.gpu_features = app.getGPUFeatureStatus()
  for (const event of ['lock-screen', 'unlock-screen', 'suspend', 'resume']) powerMonitor.on(event, () => { state.power_events.push({ event, at: new Date().toISOString() }); save() })
  backend = spawn(python, [runtimeScript, root], { cwd: root,
    env: { ...process.env, PYTHONPATH: path.join(root, 'services', 'local_agent', 'src') }, stdio: ['ignore', 'pipe', 'ignore'] })
  runtime = await new Promise((resolve, reject) => {
    let output = ''
    const timer = setTimeout(() => reject(Error('RUNTIME_START_TIMEOUT')), 60000)
    backend.stdout.on('data', bytes => {
      output += bytes.toString()
      const line = output.split('\n').find(row => row.startsWith('{'))
      if (!line) return
      try { const ready = JSON.parse(line); clearTimeout(timer); ready.status === 'ready' ? resolve(ready) : reject(Error('RUNTIME_START_FAILED')) } catch {}
    })
    backend.once('exit', () => { clearTimeout(timer); reject(Error('RUNTIME_EXITED')) })
  })
  server = http.createServer((request, response) => {
    if (request.url === '/component.js') { response.setHeader('Content-Type', 'text/javascript'); response.end(fs.readFileSync(path.join(root, 'component.js'))) }
    else response.end('<html><body><h2>观察摄像头持续运行验证（隔离测试）</h2><div id="root"></div><script src="/component.js"></script></body></html>')
  })
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
  const origin = 'http://127.0.0.1:' + server.address().port
  win = new BrowserWindow({ show: true, title: '家庭学习伙伴：观察摄像头持续验证', webPreferences: { contextIsolation: true, nodeIntegration: false, backgroundThrottling: false } })
  const session = win.webContents.session
  session.webRequest.onBeforeRequest((details, callback) => callback({ cancel: !details.url.startsWith(origin + '/') && !details.url.startsWith(runtime.localApiUrl + '/') && !details.url.startsWith('data:') }))
  session.setPermissionRequestHandler((contents, permission, callback, details) => callback(contents === win.webContents && permission === 'media' && details.requestingUrl.startsWith(origin + '/')))
  await win.loadURL(origin)
  state.stage = 'mounting_current_component'; save()
  state.bound = await win.webContents.executeJavaScript(`window.__cameraProbe.start(${JSON.stringify(runtime)})`)
  const read = () => win.webContents.executeJavaScript('window.__cameraProbe.read()')
  async function waitFrame(timeout = 45000) {
    const deadline = Date.now() + timeout
    while (Date.now() < deadline) {
      const frame = await read(); state.starting_frame = frame
      if (frame.status.frame_confirmed && frame.frames > 0) return frame
      save(); await sleep(500)
    }
    throw Error('NO_CURRENT_COMPONENT_FRAME')
  }
  state.first = await waitFrame()
  state.stage = 'continuous_' + soakSeconds + '_seconds'; save()
  let originalStream = state.first.stream_id, naturalStall = null
  for (let tick = 0; tick < soakSeconds; tick++) {
    await sleep(1000)
    const frame = await read()
    state.samples.push({ tick, ...frame, window_visible: win.isVisible(), window_minimized: win.isMinimized(), idle_seconds: powerMonitor.getSystemIdleTime() })
    if (!frame.status.frame_confirmed) {
      if (!naturalStall) naturalStall = { tick, at: Date.now(), before: frame }
      if (Date.now() - naturalStall.at > 45000) throw Error('NATURAL_STALL_NOT_RECOVERED')
    } else if (naturalStall || frame.stream_id !== originalStream) {
      state.spontaneous_recoveries.push({ before: naturalStall?.before ?? state.samples[state.samples.length - 2], after: frame,
        elapsed_ms: naturalStall ? Date.now() - naturalStall.at : null, stream_replaced: frame.stream_id !== originalStream })
      originalStream = frame.stream_id
      naturalStall = null
    }
    if (tick % 10 === 0) save()
  }
  if (naturalStall) throw Error('NATURAL_STALL_UNCONFIRMED_AT_BOUNDARY')
  if (dualSeconds) {
    state.stage = 'dual_camera_' + dualSeconds + '_seconds'; save()
    state.document_camera = await win.webContents.executeJavaScript('window.__cameraProbe.openDocument()')
    state.dual_samples = []
    for (let tick = 0; tick < dualSeconds; tick++) {
      await sleep(1000)
      const frame = await read()
      const document = await win.webContents.executeJavaScript('window.__cameraProbe.readDocument()')
      state.dual_samples.push({ tick, observation: frame, document })
      if (!frame.status.frame_confirmed || frame.stream_id !== originalStream || !document.live) throw Error('DUAL_CAPTURE_LOST')
      if (tick > 5 && document.frames <= state.dual_samples[tick - 5].document.frames) throw Error('DOCUMENT_CAPTURE_STALLED')
      if (tick % 10 === 0) save()
    }
    state.document_released = await win.webContents.executeJavaScript('window.__cameraProbe.closeDocument()')
    if (!state.document_released) throw Error('DOCUMENT_RELEASE_UNCONFIRMED')
  }
  for (const fault of ['pause_video', 'stop_track']) {
    state.stage = 'recover_' + fault; save()
    const before = await read(), started = Date.now()
    await win.webContents.executeJavaScript(`window.__cameraProbe.fault(${JSON.stringify(fault)})`)
    let recovered
    while (Date.now() - started < 45000) {
      await sleep(500)
      const frame = await read()
      if (frame.stream_id !== before.stream_id && frame.status.frame_confirmed && frame.frames > 0) { recovered = frame; break }
    }
    if (!recovered) throw Error('AUTOMATIC_RECOVERY_FAILED')
    state.recoveries.push({ fault, elapsed_ms: Date.now() - started, before, after: recovered })
    await sleep(15000)
  }
  state.stage = 'privacy_pause'; save()
  await win.webContents.executeJavaScript('window.__cameraProbe.pause()')
  await sleep(30000)
  state.paused = await read()
  if (state.paused.status.track_live || state.paused.stream_id !== null) throw Error('PRIVACY_PAUSE_FAILED')
  await finish(0)
}).catch(async error => {
  state.failure = error.message.match(/(?:SETUP_HTTP_[0-9]+|L523_NOT_FOUND|[A-Z_]{5,})/)?.[0] || error.name
  if (win && !win.isDestroyed()) state.setup = await win.webContents.executeJavaScript('window.__cameraSetupResult || null').catch(() => null)
  void finish(2)
})
