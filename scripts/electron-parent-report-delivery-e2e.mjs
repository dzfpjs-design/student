#!/usr/bin/env node
/** Isolated visible acceptance for Electron safeStorage and loopback SMTP delivery. */
import { spawn } from 'node:child_process'
import { parentReportDeliveryEnvironment } from './electron-parent-report-delivery-env.mjs'
import { captureOwnedFailure, redactHarnessDiagnostic, runtimeDiagnostic } from './electron-parent-report-delivery-diagnostics.mjs'
import { awaitCdpResponse, CDP_COMMAND_TIMEOUT_MS, ELECTRON_RENDERER_STARTUP_TIMEOUT_MS, releaseChildProcessHandles, stopWindowsProcessTree, windowsRemainingPids } from './electron-owned-process.mjs'
import { randomUUID } from 'node:crypto'
import { createServer } from 'node:net'
import { existsSync } from 'node:fs'
import { mkdir, mkdtemp, readFile, readdir, realpath, rm, symlink, writeFile } from 'node:fs/promises'
import { dirname, join, resolve } from 'node:path'
import { tmpdir } from 'node:os'
import { fileURLToPath } from 'node:url'

const root = resolve(fileURLToPath(new URL('..', import.meta.url)))
const args = parseArgs(process.argv.slice(2))
const electronBin = resolve(args.get('--electron-bin'))
const pythonBin = resolve(args.get('--python-bin'))
const outputDir = resolve(args.get('--output-dir'))
const syntheticPassword = `hsc-smtp-${randomUUID()}`
const smtpUsername = 'synthetic-parent'
const smtpRecipient = 'parent@local.test'
const setupRecipient = 'setup-parent@local.test'
const owned = new Map()
const diagnosticSecrets = new Set([syntheticPassword])
let venvLinkCreated = false

function parseArgs(values) {
  const result = new Map()
  for (let index = 0; index < values.length; index += 2) result.set(values[index], values[index + 1])
  for (const key of ['--electron-bin', '--python-bin', '--output-dir']) if (!result.get(key)) throw new Error(`${key} is required`)
  return result
}
const sleep = (ms) => new Promise((resolvePromise) => setTimeout(resolvePromise, ms))
async function eventually(read, label, timeout = 25_000) {
  const deadline = Date.now() + timeout; let last
  while (Date.now() < deadline) {
    try { const value = await read(); if (value) return value } catch (error) { last = error }
    await sleep(160)
  }
  throw new Error(`${label} timed out${last ? `: ${last.message}` : ''}`)
}
async function freePort() {
  const server = createServer()
  await new Promise((resolvePromise, reject) => server.once('error', reject).listen(0, '127.0.0.1', resolvePromise))
  const port = server.address().port
  await new Promise((resolvePromise, reject) => server.close((error) => error ? reject(error) : resolvePromise()))
  return port
}
async function run(command, commandArgs, options = {}) {
  return await new Promise((resolvePromise, reject) => {
    const child = spawn(command, commandArgs, { stdio: ['ignore', 'pipe', 'pipe'], ...options })
    let stdout = '', stderr = ''
    child.stdout?.on('data', (chunk) => { stdout += chunk })
    child.stderr?.on('data', (chunk) => { stderr += chunk })
    child.once('error', reject)
    child.once('exit', (code) => code === 0 ? resolvePromise({ stdout, stderr }) : reject(new Error(`${command} exited ${code}: ${stderr.slice(-1200)}`)))
  })
}
async function seedDatabase(databasePath) {
  const receiptPath = join(outputDir, 'seed-result.json')
  const result = await run(pythonBin, [join(root, 'scripts/seed-parent-report-delivery.py'), '--database-path', databasePath, '--receipt-path', receiptPath], { cwd: root,
    env: parentReportDeliveryEnvironment({ root, pythonBin, dataDir: dirname(databasePath), databasePath }) })
  await writeFile(join(outputDir, 'seed.log'), redactHarnessDiagnostic(result.stdout + result.stderr, [...diagnosticSecrets]), { mode: 0o600 })
  const summary = JSON.parse(await readFile(receiptPath, 'utf8'))
  if (summary.reflection_count !== 1 || summary.completed_learning_tasks !== 0 || summary.report_job_status !== 'completed') throw new Error('Synthetic seed did not persist the expected no-learning reflection/report')
}

function createSmtpSink() {
  const messages = []; const commands = []; let authSuccesses = 0, connections = 0
  const server = createServer((socket) => {
    connections += 1
    socket.setEncoding('utf8'); socket.write('220 hsc-loopback ESMTP\r\n')
    let buffer = '', data = false, body = '', authState = '', authUser = ''
    const acceptCredentials = (user, password) => {
      if (user === smtpUsername && password === syntheticPassword) { authSuccesses += 1; socket.write('235 2.7.0 authenticated\r\n') }
      else socket.write('535 5.7.8 invalid credentials\r\n')
    }
    socket.on('data', (chunk) => {
      buffer += chunk
      while (buffer.includes('\n')) {
        const index = buffer.indexOf('\n'); const line = buffer.slice(0, index + 1).replace(/\r?\n$/, ''); buffer = buffer.slice(index + 1)
        if (data) {
          if (line === '.') { messages.push(body); body = ''; data = false; socket.write('250 2.0.0 accepted\r\n') }
          else body += `${line}\n`
          continue
        }
        if (authState === 'plain') { const decoded = Buffer.from(line, 'base64').toString(); const parts = decoded.split('\0'); authState = ''; acceptCredentials(parts.at(-2) ?? '', parts.at(-1) ?? ''); continue }
        if (authState === 'login-user') { authUser = Buffer.from(line, 'base64').toString(); authState = 'login-pass'; socket.write('334 UGFzc3dvcmQ6\r\n'); continue }
        if (authState === 'login-pass') { authState = ''; acceptCredentials(authUser, Buffer.from(line, 'base64').toString()); continue }
        const [command, ...rest] = line.split(' '); const upper = command.toUpperCase()
        commands.push(upper)
        if (upper === 'EHLO' || upper === 'HELO') socket.write('250-hsc-loopback\r\n250 AUTH PLAIN LOGIN\r\n')
        else if (upper === 'AUTH' && rest[0]?.toUpperCase() === 'PLAIN' && rest[1]) { const decoded = Buffer.from(rest[1], 'base64').toString(); const parts = decoded.split('\0'); acceptCredentials(parts.at(-2) ?? '', parts.at(-1) ?? '') }
        else if (upper === 'AUTH' && rest[0]?.toUpperCase() === 'PLAIN') { authState = 'plain'; socket.write('334 \r\n') }
        else if (upper === 'AUTH' && rest[0]?.toUpperCase() === 'LOGIN') { authState = 'login-user'; socket.write('334 VXNlcm5hbWU6\r\n') }
        else if (upper === 'MAIL' || upper === 'RCPT' || upper === 'RSET' || upper === 'NOOP') socket.write('250 2.0.0 ok\r\n')
        else if (upper === 'DATA') { data = true; socket.write('354 end with <CRLF>.<CRLF>\r\n') }
        else if (upper === 'QUIT') { socket.write('221 2.0.0 bye\r\n'); socket.end() }
        else socket.write('250 2.0.0 ok\r\n')
      }
    })
  })
  return { server, messages, authSuccesses: () => authSuccesses, connections: () => connections, commands: () => [...commands] }
}

class Cdp {
  constructor(url) { this.socket = new WebSocket(url); this.nextId = 0; this.pending = new Map(); this.events = [] }
  async open() {
    await new Promise((resolvePromise, reject) => { this.socket.addEventListener('open', resolvePromise, { once: true }); this.socket.addEventListener('error', reject, { once: true }) })
    this.socket.addEventListener('message', (event) => { const message = JSON.parse(String(event.data)); const item = this.pending.get(message.id); if (!item) { if (message.method) this.events.push(message); return } this.pending.delete(message.id); message.error ? item.reject(new Error(message.error.message)) : item.resolve(message.result) })
  }
  send(method, params = {}) { const id = ++this.nextId; const response = awaitCdpResponse(this.pending, id, method, CDP_COMMAND_TIMEOUT_MS); this.socket.send(JSON.stringify({ id, method, params })); return response }
  async evaluate(expression) { const result = await this.send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true }); if (result.exceptionDetails) throw new Error(result.exceptionDetails.exception?.description ?? result.exceptionDetails.text ?? 'renderer evaluation failed'); return result.result.value }
  close() { this.socket.close() }
}
async function stopOwned(app) {
  const child = app?.child
  if (!child?.pid) return
  app.cdp?.close()
  let trackedPids = [child.pid]
  if (process.platform === 'win32') {
    trackedPids = await stopWindowsProcessTree(child, eventually, run)
  } else if (child.exitCode === null) {
    try { process.kill(-child.pid, 'SIGTERM') } catch { child.kill('SIGTERM') }
  }
  if (process.platform !== 'win32') {
    const exited = await eventually(() => child.exitCode !== null, 'owned Electron exit', 7_000).catch(() => false)
    if (!exited) { try { process.kill(-child.pid, 'SIGKILL') } catch { child.kill('SIGKILL') } }
    if (!await eventually(() => child.exitCode !== null, 'owned Electron final exit', 3_000).catch(() => false)) throw new Error(`owned Electron ${child.pid} did not exit`)
  }
  await assertOwnedStopped(app, trackedPids)
  releaseChildProcessHandles(child)
  owned.delete(child)
}
async function startElectron(profile, dataDir, databasePath, { fakeMedia = false } = {}) {
  const cdpPort = await freePort(); let logs = ''
  const child = spawn(electronBin, [root, `--remote-debugging-port=${cdpPort}`,
    ...(fakeMedia ? ['--use-fake-device-for-media-stream'] : [])], {
    cwd: root, detached: true, stdio: ['ignore', 'pipe', 'pipe'],
    env: parentReportDeliveryEnvironment({ root, pythonBin, profile, dataDir, databasePath })
  })
  const ownedApp = { child, cdp: null, cdpPort, phase: 'cdp-page', logs: () => logs, runtime: null }
  child.once('error', (error) => { ownedApp.spawnError = error.message })
  owned.set(child, ownedApp); child.stdout.on('data', (chunk) => { logs += chunk }); child.stderr.on('data', (chunk) => { logs += chunk })
  const page = await eventually(async () => { const response = await fetch(`http://127.0.0.1:${cdpPort}/json/list`); const targets = await response.json(); return targets.find((item) => item.type === 'page' && item.webSocketDebuggerUrl) }, 'Electron CDP page', ELECTRON_RENDERER_STARTUP_TIMEOUT_MS)
  const cdp = new Cdp(page.webSocketDebuggerUrl); ownedApp.cdp = cdp; ownedApp.phase = 'cdp-connect'; await cdp.open(); await cdp.send('Page.enable'); await cdp.send('Runtime.enable'); await cdp.send('Log.enable')
  ownedApp.phase = 'runtime'
  await eventually(async () => {
    const runtime = await cdp.evaluate('window.desktop?.getRuntimeConfig()')
    if (runtime?.sessionToken) diagnosticSecrets.add(runtime.sessionToken)
    ownedApp.runtime = runtimeDiagnostic(runtime)
    return runtime?.status === 'ready' && runtime
  }, 'loopback Agent runtime')
  ownedApp.phase = 'ready'
  return ownedApp
}
async function assertOwnedStopped(app, trackedPids = [app.child.pid]) {
  if (process.platform === 'win32') {
    const survivors = await windowsRemainingPids(trackedPids, run)
    if (survivors.length) throw new Error(`owned Windows process tree survived cleanup: ${survivors.join(', ')}`)
  } else {
    if (app.child.exitCode === null) throw new Error(`owned Electron ${app.child.pid} is still running`)
    const groupGone = await eventually(() => {
      try { process.kill(-app.child.pid, 0); return false }
      catch (error) { if (error.code === 'ESRCH') return true; throw error }
    }, `owned Electron process group ${app.child.pid} closure`, 7_000).catch(() => false)
    if (!groupGone) throw new Error(`owned Electron process group ${app.child.pid} survived cleanup`)
  }
  await eventually(async () => {
    try { await fetch(`http://127.0.0.1:${app.cdpPort}/json/list`, { signal: AbortSignal.timeout(200) }); return false } catch { return true }
  }, `owned CDP port ${app.cdpPort} closure`, 3_000)
}
async function body(cdp) { return await cdp.evaluate('document.body.innerText') }
async function visible(cdp, text, timeout) {
  try { return await eventually(async () => (await body(cdp)).includes(text), `visible text: ${text}`, timeout) }
  catch (error) { throw new Error(`${error.message}; current UI: ${(await body(cdp)).slice(0, 2400)}`) }
}
async function click(cdp, text, selector = 'button') {
  await eventually(async () => await cdp.evaluate(`Array.from(document.querySelectorAll(${JSON.stringify(selector)})).some((item) => !item.disabled && item.textContent?.includes(${JSON.stringify(text)}))`), `clickable: ${text}`)
  await cdp.evaluate(`Array.from(document.querySelectorAll(${JSON.stringify(selector)})).find((item) => !item.disabled && item.textContent?.includes(${JSON.stringify(text)}))?.click()`)
}
async function setLabel(cdp, label, value) {
  await cdp.evaluate(`(() => { const node=document.querySelector('[aria-label=${JSON.stringify(label)}]') || Array.from(document.querySelectorAll('label')).find((item)=>item.textContent?.includes(${JSON.stringify(label)}))?.querySelector('input,select,textarea'); if (!node) throw new Error('missing field: ${label}'); const proto=node instanceof HTMLSelectElement?HTMLSelectElement.prototype:HTMLInputElement.prototype; Object.getOwnPropertyDescriptor(proto,'value').set.call(node,${JSON.stringify(value)}); node.dispatchEvent(new Event('input',{bubbles:true})); node.dispatchEvent(new Event('change',{bubbles:true})); })()`)
}
async function completeInstallationIfRequired(cdp, smtpPort, smtpSink) {
  const initialText = await eventually(async () => {
    const text = await body(cdp)
    return text.includes('先完成家长与日报配置') || text.includes('今天，我们从哪里开始？') ? text : null
  }, 'installation or child home surface')
  if (!initialText.includes('先完成家长与日报配置')) return false
  await setLabel(cdp, '初始化家长PIN', '2468')
  await setLabel(cdp, '再次输入家长PIN', '2468')
  await click(cdp, '保存 PIN')
  await visible(cdp, '配置日报 SMTP')
  await setLabel(cdp, '初始化SMTP主机', '127.0.0.1')
  await setLabel(cdp, '初始化SMTP端口', String(smtpPort))
  await setLabel(cdp, '初始化SMTP加密', 'none')
  await setLabel(cdp, '初始化SMTP用户名', smtpUsername)
  await setLabel(cdp, '初始化SMTP密码', syntheticPassword)
  await setLabel(cdp, '初始化SMTP发件地址', 'report@local.test')
  await click(cdp, '保存 SMTP')
  await visible(cdp, '设置每日报告收件人')
  await setLabel(cdp, '初始化收件邮箱', setupRecipient)
  await click(cdp, '保存收件人')
  await visible(cdp, '发送固定测试邮件')
  await click(cdp, '发送并验证')
  const verified = await eventually(() => smtpSink.messages.length === 2 && smtpSink.authSuccesses() === 2, 'installation SMTP verification for both children').catch(() => false)
  if (!verified) throw new Error(`installation SMTP verification failed: ${JSON.stringify({ connections: smtpSink.connections(), auth_successes: smtpSink.authSuccesses(), messages: smtpSink.messages.length, commands: smtpSink.commands() })}`)
  await visible(cdp, '今天，我们从哪里开始？', 70_000)
  return true
}
async function unlockParent(cdp, first, smtpPort, smtpSink) {
  const initialized = await completeInstallationIfRequired(cdp, smtpPort, smtpSink)
  await click(cdp, '家长中心'); await visible(cdp, '家长 PIN'); await setLabel(cdp, '家长 PIN', '2468'); await click(cdp, initialized || !first ? '解锁' : '设置并进入'); await visible(cdp, '报告审核与送达')
}

async function openSettings(cdp) {
  await eventually(() => cdp.evaluate(`Boolean(document.querySelector('.report-delivery-settings summary'))`), 'delivery settings summary')
  await cdp.evaluate(`(() => { const details=document.querySelector('.report-delivery-settings'); if (!details.open) details.querySelector('summary').click(); return details.open })()`)
  await eventually(() => cdp.evaluate(`Boolean(document.querySelector('input[aria-label="SMTP主机"]'))`), 'visible SMTP settings')
}
async function focusSmtpSettings(cdp) {
  await cdp.evaluate(`Array.from(document.querySelectorAll('legend')).find((item) => item.textContent?.includes('邮件 SMTP'))?.scrollIntoView({ block: 'center' })`)
  await sleep(250)
}
async function focusEmailTestStatus(cdp) {
  await cdp.evaluate(`Array.from(document.querySelectorAll('p')).find((item) => item.textContent?.includes('邮件发送测试：'))?.scrollIntoView({ block: 'center' })`)
  await sleep(250)
}
async function screenshot(cdp, path) { const shot = await cdp.send('Page.captureScreenshot', { format: 'png' }); await writeFile(path, Buffer.from(shot.data, 'base64'), { mode: 0o600 }) }
async function scanForSecret(paths) {
  const leaks = [], scanErrors = []
  async function walk(path) {
    let entries
    try { entries = await readdir(path, { withFileTypes: true }) } catch (error) { scanErrors.push(`${path}: ${error.code ?? error.message}`); return }
    for (const entry of entries) {
      const current = join(path, entry.name)
      if (entry.isDirectory()) await walk(current)
      else if (entry.isFile()) {
        try { if ((await readFile(current)).includes(Buffer.from(syntheticPassword))) leaks.push(current) }
        catch (error) { scanErrors.push(`${current}: ${error.code ?? error.message}`) }
      }
    }
  }
  for (const path of paths) await walk(path)
  return { leaks, scanErrors }
}

async function main() {
  if (!existsSync(electronBin) || !existsSync(pythonBin)) throw new Error('Required local executables are missing.')
  if (!existsSync(join(root, 'out/main/index.js'))) throw new Error('Run npm run build before this acceptance.')
  await mkdir(outputDir, { recursive: true })
  const temporaryRoot = await realpath(await mkdtemp(join(tmpdir(), 'hsc-parent-delivery-e2e-')))
  const profile = join(temporaryRoot, 'profile'), dataDir = join(temporaryRoot, 'data'), databasePath = join(dataDir, 'home-study.db')
  const localVenv = join(root, '.venv')
  const logs = []; let app; let smtp; let failure
  try {
    await mkdir(profile); await mkdir(dataDir)
    if (!existsSync(localVenv)) { await symlink(resolve(dirname(pythonBin), '..'), localVenv); venvLinkCreated = true }
    await seedDatabase(databasePath)
    smtp = createSmtpSink()
    await new Promise((resolvePromise, reject) => smtp.server.once('error', reject).listen(0, '127.0.0.1', resolvePromise))
    const smtpPort = smtp.server.address().port
    app = await startElectron(profile, dataDir, databasePath); await unlockParent(app.cdp, true, smtpPort, smtp)
    const installationMessageCount = smtp.messages.length, installationAuthCount = smtp.authSuccesses()
    await eventually(() => app.cdp.evaluate(`Boolean(document.querySelector('input[aria-label="确认收件人授权"]'))`), 'report delivery snapshot and recipient form')
    await app.cdp.evaluate(`(() => { const fields=Array.from(document.querySelectorAll('.recipient-add input')).filter((node)=>node.type!=='checkbox'); if(fields.length<2) throw new Error('recipient fields missing'); const set=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set; set.call(fields[0],'本机家长'); fields[0].dispatchEvent(new Event('input',{bubbles:true})); set.call(fields[1],${JSON.stringify(smtpRecipient)}); fields[1].dispatchEvent(new Event('input',{bubbles:true})); })()`)
    await app.cdp.evaluate(`document.querySelector('input[aria-label="确认收件人授权"]')?.click()`); await click(app.cdp, '确认授权并添加'); await visible(app.cdp, '本机家长 · 邮件')
    await openSettings(app.cdp); await setLabel(app.cdp, 'SMTP主机', '127.0.0.1'); await setLabel(app.cdp, 'SMTP端口', String(smtpPort)); await setLabel(app.cdp, 'SMTP用户名', smtpUsername); await setLabel(app.cdp, 'SMTP密码', syntheticPassword); await setLabel(app.cdp, 'SMTP发件地址', 'report@local.test')
    await setLabel(app.cdp, 'SMTP加密方式', 'none')
    await click(app.cdp, '保存配置并允许邮件发送'); await visible(app.cdp, '邮件 SMTP · 已配置'); await click(app.cdp, installationMessageCount > 0 ? '重新发送一封测试邮件' : '发送不含孩子数据的测试邮件')
    await eventually(() => smtp.messages.length === installationMessageCount + 1 && smtp.authSuccesses() === installationAuthCount + 1, 'first authenticated local SMTP delivery')
    const firstMessageCount = smtp.messages.length - installationMessageCount, firstAuthCount = smtp.authSuccesses() - installationAuthCount
    await focusEmailTestStatus(app.cdp); await screenshot(app.cdp, join(outputDir, '01-saved-and-sent.png')); logs.push(app.logs()); await stopOwned(app)

    app = await startElectron(profile, dataDir, databasePath); await unlockParent(app.cdp, false, smtpPort, smtp); await openSettings(app.cdp); await visible(app.cdp, '邮件 SMTP · 已配置'); await click(app.cdp, '重新发送一封测试邮件')
    const beforeRestartMessages = smtp.messages.length, beforeRestartAuth = smtp.authSuccesses()
    await eventually(() => smtp.messages.length === beforeRestartMessages + 1 && smtp.authSuccesses() === beforeRestartAuth + 1, 'safeStorage recovery authenticated delivery')
    const restartMessageCount = smtp.messages.length - beforeRestartMessages, restartAuthCount = smtp.authSuccesses() - beforeRestartAuth
    const forbiddenChildData = ['计划20分钟', '完成 1 项学习计划', '判别式', 'child-junior-3']
    const childDataLeaks = forbiddenChildData.filter((value) => smtp.messages.some((message) => message.includes(value)))
    if (childDataLeaks.length) throw new Error(`fixed SMTP test message exposed seeded child data: ${childDataLeaks.join(', ')}`)
    await click(app.cdp, '删除邮件配置与密码')
    await visible(app.cdp, '先完成家长与日报配置', 70_000); await screenshot(app.cdp, join(outputDir, '02-cleared.png')); logs.push(app.logs()); await stopOwned(app)

    app = await startElectron(profile, dataDir, databasePath); await visible(app.cdp, '先完成家长与日报配置')
    const status = await app.cdp.evaluate('window.desktop.getReportDeliverySecretStatus()')
    if (status.smtpPasswordConfigured || status.state !== 'empty') throw new Error(`secret status survived clear: ${JSON.stringify(status)}`)
    const before = smtp.messages.length; await sleep(800)
    if (smtp.messages.length !== before) throw new Error('delivery occurred after visible credential clear and restart')
    await screenshot(app.cdp, join(outputDir, '03-restart-blocked.png')); logs.push(app.logs()); await stopOwned(app); app = null
    await writeFile(join(outputDir, 'electron.log'), logs.join('\n--- restart ---\n'), { mode: 0o600 })
    const { leaks, scanErrors } = await scanForSecret([profile, dataDir, outputDir, join(root, 'out')])
    if (scanErrors.length) throw new Error(`secret scan was incomplete: ${scanErrors.join(', ')}`)
    if (leaks.length) throw new Error(`plaintext synthetic secret found on disk: ${leaks.join(', ')}`)
    const report = { status: 'passed', synthetic_only: true, loopback_only: true, installation_setup_completed_in_ui: true, installation_test_messages: installationMessageCount, installation_auth_successes: installationAuthCount, visible_ui_save: true, first_authenticated_sink_messages: firstMessageCount, first_auth_successes: firstAuthCount, restart_recovered_authenticated_sink_messages: restartMessageCount, restart_auth_successes: restartAuthCount, fixed_test_child_data_leaks: childDataLeaks.length, plaintext_secret_files: leaks.length, secret_scan_errors: scanErrors.length, visible_ui_clear: true, post_clear_restart_blocked_by_installation_gate: true, final_sink_messages: smtp.messages.length, external_delivery: false, screenshots: ['01-saved-and-sent.png', '02-cleared.png', '03-restart-blocked.png'] }
    await writeFile(join(outputDir, 'result.json'), JSON.stringify(report, null, 2) + '\n', { mode: 0o600 }); process.stdout.write(`${JSON.stringify(report)}\n`)
  } catch (error) {
    failure = error
    const captureErrors = await captureOwnedFailure({ outputDir, temporaryRoot, apps: [...owned.values()],
      error, secrets: [...diagnosticSecrets], smtpMessages: smtp?.messages.length ?? 0 })
    if (captureErrors.length) console.error(`Failure capture incomplete: ${JSON.stringify(captureErrors)}`)
    const latest = [...owned.values()].at(-1)
    if (latest?.cdp) await screenshot(latest.cdp, join(outputDir, 'failure.png')).catch(() => undefined)
    throw error
  } finally {
    const cleanupErrors = []
    const collect = (results) => cleanupErrors.push(...results.filter((item) => item.status === 'rejected').map((item) => item.reason))
    if (app) collect(await Promise.allSettled([stopOwned(app)]))
    collect(await Promise.allSettled([...owned.values()].map(stopOwned)))
    if (smtp) collect(await Promise.allSettled([
      new Promise((resolvePromise, reject) => smtp.server.close((error) => error ? reject(error) : resolvePromise()))
    ]))
    if (venvLinkCreated) collect(await Promise.allSettled([rm(localVenv, { force: true })]))
    if (!failure && !cleanupErrors.length) collect(await Promise.allSettled([rm(temporaryRoot, { recursive: true, force: true })]))
    if (failure || cleanupErrors.length) {
      await writeFile(join(outputDir, 'failure-cleanup.json'), JSON.stringify({ retained_fixture: temporaryRoot,
        owned_processes_remaining: owned.size, smtp_listening: smtp?.server.listening ?? false,
        errors: cleanupErrors.map((error) => redactHarnessDiagnostic(error?.message ?? error, [...diagnosticSecrets])) }, null, 2), { mode: 0o600 })
        .catch((error) => console.error(`Cannot persist cleanup status: ${error.code}`))
      console.error(`Synthetic failure fixture retained: ${temporaryRoot}`)
    }
    if (cleanupErrors.length && !failure) throw new Error(`owned process cleanup failed: ${cleanupErrors.map((error) => error?.message ?? error).join(', ')}`)
  }
}

export { startElectron, stopOwned, unlockParent, visible, screenshot, root, outputDir, pythonBin,
  owned, diagnosticSecrets, run, captureOwnedFailure, redactHarnessDiagnostic }

if (resolve(process.argv[1]) === fileURLToPath(import.meta.url)) main().catch(async (error) => { await mkdir(outputDir, { recursive: true }).catch(() => undefined); const safeError = redactHarnessDiagnostic(error.stack ?? error, [...diagnosticSecrets]); await writeFile(join(outputDir, 'failure.txt'), `${safeError}\n`, { mode: 0o600 }).catch(() => undefined); console.error(safeError); process.exit(1) })
