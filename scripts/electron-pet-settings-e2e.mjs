#!/usr/bin/env node
import { mkdir, mkdtemp, readFile, writeFile } from 'node:fs/promises'
import { join } from 'node:path'
import { createHash, randomUUID } from 'node:crypto'
import { spawnSync } from 'node:child_process'
import { startElectron, stopOwned, unlockParent, screenshot, root, outputDir, owned, diagnosticSecrets } from './electron-parent-report-delivery-e2e.mjs'

// Dedicated disposable profile; a synthetic key is saved but no child grant is created.
await mkdir(outputDir, { recursive: true })
const inspectionIndex = process.argv.indexOf('--inspect-result')
const inspection = inspectionIndex >= 0 ? JSON.parse(await readFile(process.argv[inspectionIndex + 1], 'utf8')) : null
if (inspection && (!inspection.passed || !/^\/private\/tmp\/hsc-pet-settings-native-[A-Za-z0-9]+$/.test(inspection.fixture))) throw Error('Only this harness disposable fixture can be inspected')
const fixture = inspection ? inspection.fixture : await mkdtemp('/private/tmp/hsc-pet-settings-native-')
const profile = join(fixture, 'profile'), data = join(fixture, 'data'), db = join(data, 'home-study.db')
await mkdir(profile, { recursive: true }); await mkdir(data, { recursive: true })
const key = 'synthetic-never-a-provider-key-' + randomUUID()
diagnosticSecrets.add(key)
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms))
async function until(read, label, timeout = 45000) {
  const deadline = Date.now() + timeout
  while (Date.now() < deadline) {
    try { if (await read()) return } catch (error) {
      if (!/context|navigation|document|innerText/i.test(String(error))) throw error
    }
    await sleep(180)
  }
  throw Error(label)
}
let app
const evalUi = source => app.cdp.evaluate(source)
const has = text => evalUi(`Boolean(document.body?.innerText.includes(${JSON.stringify(text)}))`)
async function click(text) {
  const source = `Array.from(document.querySelectorAll('button')).find(x=>!x.disabled&&x.textContent.includes(${JSON.stringify(text)}))`
  await until(() => evalUi(`Boolean(${source})`), 'Button unavailable: ' + text)
  await evalUi(`${source}.click()`)
}
async function field(label, value) {
  await evalUi(`(()=>{const x=document.querySelector('input[aria-label=${JSON.stringify(label)}]');if(!x||x.disabled)throw Error('Field unavailable');Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(x,${JSON.stringify(value)});x.dispatchEvent(new Event('input',{bubbles:true}));x.dispatchEvent(new Event('change',{bubbles:true}));})()`)
}
async function open(first = false) {
  if (!await has('报告审核与送达')) await unlockParent(app.cdp, first)
  if (!await evalUi(`Boolean(document.querySelector('section[aria-label="原音模型设置"]'))`)) await click('配置原音模型')
  await until(() => evalUi(`Boolean(document.querySelector('input[aria-label="原音模型名称"]:not(:disabled)'))`), 'Settings not editable')
}
async function snapshot(name) {
  await evalUi(`document.querySelector('section[aria-label="原音模型设置"]')?.scrollIntoView({block:'center'})`)
  await screenshot(app.cdp, join(outputDir, name + '.png'))
}
async function returnThroughRuntimeGate() {
  await until(async () => !await has('报告审核与送达') && await has('家长中心'), 'Runtime change did not close stale parent identity')
  await open(false)
}
async function summary() {
  // Reuse the UI's actual authenticated read, without serializing the PIN/session token.
  return await evalUi(`(()=>{const x=document.querySelector('section[aria-label="原音模型设置"]');return x?x.innerText:null})()`)
}
function assertNoDispatch() {
  const result = spawnSync('/usr/bin/sqlite3', ['-readonly', '-cmd', '.timeout 5000', db,
    'SELECT count(*) FROM pet_audio_dispatch; SELECT count(*) FROM pet_audio_grant;'], { encoding: 'utf8' })
  if (result.status !== 0 || result.stdout.trim() !== '0\n0') throw Error('Unexpected grant/dispatch or fixture read failed')
}
const html = await readFile(join(root, 'out/renderer/index.html'), 'utf8')
const asset = html.match(/\.\/assets\/index-[^" ]+\.js/)?.[0]
if (!asset) throw Error('Renderer build missing')
const files = ['src/main/pet-audio-settings.ts', 'src/main/local-service.ts', 'src/main/index.ts',
  'src/preload/index.ts', 'src/renderer/src/ParentPetAudioSettings.tsx', 'src/renderer/src/api.ts',
  'src/shared/petAudioTariffs.ts', 'services/local_agent/src/home_study_agent/config.py',
  'services/local_agent/src/home_study_agent/pet_audio_gateway.py', 'services/local_agent/src/home_study_agent/pet_audio_catalog.py',
  'out/main/index.js', 'out/preload/index.js', 'out/renderer/' + asset.slice(2)]
const hashes = async () => Object.fromEntries(await Promise.all(files.map(async file => [file, createHash('sha256').update(await readFile(join(root, file))).digest('hex')])))
const before = await hashes(), receipts = []
async function stopFixture(value) {
  try { await stopOwned(value) } catch (error) {
    if (!String(error).includes(`owned Electron process group ${value.child.pid} survived cleanup`)) throw error
    try { process.kill(-value.child.pid, 'SIGKILL') } catch (cause) { if (cause.code !== 'ESRCH') throw cause }
    await sleep(500)
    await stopOwned(value)
  }
}
try {
  app = await startElectron(profile, data, db)
  await app.cdp.send('Page.addScriptToEvaluateOnNewDocument', { source: `Object.defineProperty(navigator.mediaDevices,'getUserMedia',{value:async()=>{throw new DOMException('Synthetic device denial','NotAllowedError')}})` })
  await open(!inspection)
  if (inspection) {
    for (const width of [1280, 640]) {
      await app.cdp.send('Emulation.setDeviceMetricsOverride', { width, height: 900, deviceScaleFactor: 1, mobile: false })
      await sleep(250)
      const dimensions = await evalUi(`(()=>{const s=document.querySelector('section[aria-label="原音模型设置"]'),l=s.querySelector('.pet-config-consent'),i=l.querySelector('input').getBoundingClientRect(),t=l.querySelector('span').getBoundingClientRect();return{inputWidth:i.width,inputHeight:i.height,labelSeparated:i.right<=t.left,overflow:s.scrollWidth>s.clientWidth+1}})()`)
      if (dimensions.inputWidth !== 18 || dimensions.inputHeight !== 18 || !dimensions.labelSeparated || dimensions.overflow) throw Error('Settings layout invalid: ' + JSON.stringify(dimensions))
      receipts.push({ phase: 'layout', width, dimensions }); await snapshot('layout-' + width)
    }
    assertNoDispatch()
    await stopFixture(app); app = null
  } else {
  await field('原音专属密钥', key)
  await evalUi(`document.querySelector('input[aria-label="启用已核验原音配置"]').click()`)
  const oldRuntime = await evalUi(`window.desktop.getRuntimeConfig().then(r=>r.sessionToken)`)
  await click('保存原音配置')
  await until(async () => {
    try { return (await readFile(join(profile, 'pet-audio-settings.bin'))).length > 0 } catch { return false }
  }, 'Protected configuration missing')
  await until(() => evalUi(`window.desktop.getRuntimeConfig().then(r=>r.status==='ready'&&r.sessionToken!==${JSON.stringify(oldRuntime)})`), 'Managed backend failed to restart')
  await returnThroughRuntimeGate()
  await until(() => has('已核验原音模型配置已应用'), 'Saved configuration not applied/readable')
  if ((await readFile(join(profile, 'pet-audio-settings.bin'))).includes(Buffer.from(key))) throw Error('Plaintext credential on disk')
  assertNoDispatch(); receipts.push({ phase: 'saved', text: await summary() }); await snapshot('01-saved')
  await stopFixture(app); app = null
  app = await startElectron(profile, data, db)
  await open(false)
  await until(() => has('已核验原音模型配置已应用'), 'Full restart lost protected configuration')
  assertNoDispatch(); receipts.push({ phase: 'restart', text: await summary() }); await snapshot('02-reopened')
  const clearRuntime = await evalUi(`window.desktop.getRuntimeConfig().then(r=>r.sessionToken)`)
  await click('清除原音密钥')
  await until(() => evalUi(`window.desktop.getRuntimeConfig().then(r=>r.status==='ready'&&r.sessionToken!==${JSON.stringify(clearRuntime)})`), 'Clear restart not ready')
  await returnThroughRuntimeGate()
  await until(() => has('尚未保存原音专属模型配置'), 'Cleared configuration still enabled')
  assertNoDispatch(); receipts.push({ phase: 'cleared', text: await summary() }); await snapshot('03-cleared')
  await stopFixture(app); app = null
  app = await startElectron(profile, data, db)
  await open(false)
  await until(() => has('尚未保存原音专属模型配置'), 'Full restart revived deleted configuration')
  assertNoDispatch(); receipts.push({ phase: 'clear_restart', text: await summary() })
  await stopFixture(app); app = null
  }
  const after = await hashes()
  if (JSON.stringify(before) !== JSON.stringify(after)) throw Error('Verification inputs changed')
  await writeFile(join(outputDir, 'result.json'), JSON.stringify({ passed: true, fixture, before, after, receipts,
    owned_processes: owned.size, grant_count: 0, dispatch_count: 0,
    boundary: 'Actual macOS Electron, OS-encrypted disposable configuration and managed Python restarts. Synthetic non-provider key, empty disposable family database, no child purpose grant, no audio/camera capture, no paid model/Windows/child learning outcome claim.' }, null, 2))
} catch (error) {
  if (app?.cdp) await snapshot('failure').catch(() => {})
  await writeFile(join(outputDir, 'failure.txt'), String(error.stack).split(key).join('[REDACTED]'))
  process.exitCode = 1
} finally {
  if (app) await stopFixture(app)
  for (const value of [...owned.values()]) await stopFixture(value)
}
