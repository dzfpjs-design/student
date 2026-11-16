// Actual packaged renderer + preload + local backend; synthetic trial only.
import { writeFileSync, existsSync } from 'node:fs'
import { basename, dirname, join } from 'node:path'

const [portValue, output, phase] = process.argv.slice(2)
if (!/^\d+$/.test(portValue ?? '') || !output || existsSync(output) || !['setup', 'restart', 'key-loss'].includes(phase)) {
  throw new Error('Require owned loopback debug port, new output and setup/restart/key-loss phase')
}
const deadline = Date.now() + 100_000
const pause = () => new Promise(resolve => setTimeout(resolve, 300))
const report = { status: 'running', phase, runtime: 'packaged_electron', synthetic_trial: true,
  real_voice_or_camera_stream_verified: false, checks: {} }
let socket
const pending = new Map()
let sequence = 0
const call = (method, params = {}) => new Promise((resolve, reject) => {
  const id = ++sequence
  const timer = setTimeout(() => { pending.delete(id); reject(new Error(`CDP timeout: ${method}`)) }, 15_000)
  pending.set(id, { resolve, reject, timer })
  socket.send(JSON.stringify({ id, method, params }))
})
const evaluate = async expression => {
  const result = await call('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true })
  if (result.exceptionDetails) throw new Error('Owned renderer evaluation failed')
  return result.result.value
}
const until = async expression => {
  while (Date.now() < deadline) {
    if (await evaluate(expression)) return
    await pause()
  }
  throw new Error('Owned renderer workflow timed out')
}
const require = (value, message) => { if (!value) throw new Error(message) }
try {
  let target
  while (Date.now() < deadline) {
    const targets = await fetch(`http://127.0.0.1:${portValue}/json/list`, { signal: AbortSignal.timeout(2000) })
      .then(response => response.json()).catch(() => [])
    target = targets.find(value => value.type === 'page' && value.url.startsWith('file:') && /index\.html$/.test(value.url))
    if (target) break
    await pause()
  }
  require(target, 'Packaged owned renderer target missing')
  require(new URL(target.webSocketDebuggerUrl).hostname === '127.0.0.1', 'Non-loopback CDP target rejected')
  socket = new WebSocket(target.webSocketDebuggerUrl)
  await new Promise((resolve, reject) => {
    socket.addEventListener('open', resolve, { once: true })
    socket.addEventListener('error', reject, { once: true })
  })
  socket.addEventListener('message', event => {
    const result = JSON.parse(event.data)
    const request = pending.get(result.id)
    if (!request) return
    pending.delete(result.id); clearTimeout(request.timer)
    if (result.error) request.reject(new Error(`CDP command failed: ${result.error.code}`))
    else request.resolve(result.result)
  })
  if (phase === 'key-loss') {
    await until("(document.body?.textContent ?? '').includes('恢复原有受保护密钥')")
    const state = await evaluate(`(async () => {
      const runtime = await window.desktop.getRuntimeConfig()
      return { data_scope: runtime.dataScope, status: runtime.status, error: runtime.error,
        backend_not_started: !runtime.localApiUrl && !runtime.sessionToken, retry_scheduled: Boolean(runtime.nextRetryAt) }
    })()`)
    require(state.data_scope === 'trial' && state.status === 'error' && state.backend_not_started && !state.retry_scheduled,
      'Missing key did not fail before local service launch')
    require(state.error.includes('不会自动更换密钥') && state.error.includes('不会删除原资料'), 'Recovery guidance missing')
    const retryRejected = await evaluate(`window.desktop.restartLocalService().then(() => false).catch(() => true)`)
    require(retryRejected, 'Renderer restart bypassed protected startup')
    report.checks.missing_key_blocks_backend_and_retry = true
    await call('Page.reload', { ignoreCache: true })
    await until("(document.body?.textContent ?? '').includes('恢复原有受保护密钥')")
    const preserved = await evaluate(`window.desktop.getRuntimeConfig().then(value => value.error)`)
    require(preserved === state.error, 'Protected error was overwritten by automatic recovery')
    report.runtime_state = state
    report.checks.recovery_guidance_survives_reload = true
  } else {
  await until("Boolean(document.querySelector('[aria-label=\"安装初始化\"]'))")
  const config = await evaluate(`(async () => {
    const runtime = await window.desktop.getRuntimeConfig()
    if (runtime.dataScope !== 'trial' || runtime.status !== 'ready' || new URL(runtime.localApiUrl).hostname !== '127.0.0.1')
      throw new Error('Wrong runtime')
    const response = await fetch(runtime.localApiUrl + '/v1/bootstrap', { headers: {'X-HSC-Session': runtime.sessionToken} })
    if (!response.ok) throw new Error('Bootstrap rejected')
    const bootstrap = await response.json()
    return { data_scope: runtime.dataScope, service_status: runtime.status,
      child_ids: bootstrap.children.map(child => child.child_id), model_configured: bootstrap.model.configured,
      fallback_configured: bootstrap.model.fallback_configured, voice_configured: bootstrap.voice.configured }
  })()`)
  require(!config.model_configured && !config.fallback_configured && !config.voice_configured, 'Unexpected model credentials')
  require(config.child_ids.length === 2 && new Set(config.child_ids).size === 2, 'Two isolated child profiles missing')
  report.runtime_state = config
  report.checks.packaged_preload_backend_and_initialization = true
  const confirmPresent = await evaluate("Boolean(document.querySelector('[aria-label=\"再次输入家长PIN\"]'))")
  require(confirmPresent === (phase === 'setup'), 'PIN setup did not persist across application restart')
  await evaluate(`(() => {
    const set = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set
    for (const label of ['初始化家长PIN', '再次输入家长PIN']) {
      const input = document.querySelector('[aria-label="' + label + '"]')
      if (input) { set.call(input, '2468'); input.dispatchEvent(new Event('input', { bubbles: true })) }
    }
  })()`)
  await pause()
  await evaluate(`(() => {
    const button = [...document.querySelectorAll('button')].find(value => value.textContent === ${JSON.stringify(phase === 'setup' ? '保存 PIN' : '确认身份')})
    if (!button || button.disabled) throw new Error('PIN action unavailable')
    button.click()
  })()`)
  await until("Boolean(document.querySelector('[aria-label=\"初始化SMTP主机\"]'))")
  report.checks[phase === 'setup' ? 'real_ui_pin_saved' : 'real_ui_pin_restored_and_unlocked'] = true
  await call('Page.reload', { ignoreCache: true })
  await until("Boolean(document.querySelector('[aria-label=\"初始化家长PIN\"]')) && !document.querySelector('[aria-label=\"再次输入家长PIN\"]')")
  report.checks.refresh_restores_pin_without_authenticated_session = true
  }
  const layout = await evaluate(`({ width: innerWidth, height: innerHeight, scrollWidth: document.documentElement.scrollWidth,
    heading: document.querySelector('h1')?.textContent, visible_inputs: [...document.querySelectorAll('input')].length,
    alerts: [...document.querySelectorAll('[role="alert"]')].map(value => value.textContent) })`)
  require(layout.width >= 1024 && layout.height >= 600 && layout.scrollWidth <= layout.width
    && (phase === 'key-loss' || !layout.alerts.length), 'Visible startup layout or recovery failure')
  report.layout = layout
  const screenshot = await call('Page.captureScreenshot', { format: 'png' })
  const imagePath = join(dirname(output), `owned-window-${basename(output, '.json')}.png`)
  require(!existsSync(imagePath), 'Existing screenshot refused')
  writeFileSync(imagePath, Buffer.from(screenshot.data, 'base64'), { flag: 'wx' })
  report.screenshot = imagePath
  report.status = 'passed'
} catch (error) {
  report.status = 'failed'; report.error = error.message
  process.exitCode = 1
} finally {
  socket?.close()
  for (const request of pending.values()) clearTimeout(request.timer)
  writeFileSync(output, JSON.stringify(report, null, 2) + '\n', { flag: 'wx' })
  console.log(JSON.stringify({ status: report.status, phase, output }))
}
