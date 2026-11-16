// @vitest-environment node
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { mkdtemp, mkdir, readFile, readdir, rm, writeFile } from 'node:fs/promises'
import { readFileSync } from 'node:fs'
import { createHash, randomBytes, randomUUID } from 'node:crypto'
import { join, resolve } from 'node:path'
import { pathToFileURL } from 'node:url'
import { tmpdir } from 'node:os'
import { transformSync } from 'esbuild'
import { parse } from '@babel/parser'
const state = vi.hoisted(() => ({ cipher: 0, available: true }))
vi.mock('electron', () => ({ app: { isPackaged: true, getPath: () => '/tmp/public-not-opened' }, safeStorage: {
  isEncryptionAvailable: () => state.available,
  encryptString: (value: string) => Buffer.from(JSON.stringify([++state.cipher, value])),
  decryptString: (value: Buffer) => JSON.parse(value.toString())[1],
} }))
import { ReportDeliverySecretStore } from './report-delivery-secrets'
import { createHouseholdSetupDefaultsPort, getHouseholdSetupDefaults, importInstalledHouseholdSetup } from './household-setup-defaults'
import { HouseholdProvisioningError, sealHouseholdBootstrap, suppressHouseholdProvisioningSlots } from './household-provisioning'
import { ModelSettingsStore, ensureDashScopeTextFallback } from './model-settings'
import { CloudAsrSettingsStore } from './asr-settings'
import { QwenRealtimeSettingsStore } from './qwen-realtime'
import { VoiceSettingsStore } from './voice-settings'
import { QwenRotationError, sealQwenRotation, recoverQwenKeyRotation, rotateInstalledQwenKey } from './qwen-key-rotation'
import { QwenRotationRecovery } from './qwen-rotation-recovery'
const bootstrapId = '11111111-1111-4111-8111-111111111111'
const smtp = { host: 'smtp.qq.com' as const, port: 465 as const, security: 'ssl' as const,
  username: 'public-synthetic@qq.com', fromAddress: 'public-synthetic@qq.com', recipient: 'public-parent@example.test' }
const envelope = { version: 1, bootstrapId, settings: { reportDeliverySecrets: { smtpPassword: 'PUBLIC_SYNTHETIC_ONLY_PASSWORD' }, smtpDefaults: smtp } }
const key = Buffer.alloc(32, 9)
const sha = (bytes: Buffer) => createHash('sha256').update(bytes).digest('hex')
let root: string, resources: string, user: string
beforeEach(async () => { root = await mkdtemp(join(tmpdir(), 'public-household-adapter-')); resources = join(root, 'resources'); user = join(root, 'user'); await mkdir(resources); await mkdir(user); state.available = true })
afterEach(async () => { vi.restoreAllMocks(); await rm(root, { recursive: true, force: true }) })
const port = () => createHouseholdSetupDefaultsPort(user)
const report = () => new ReportDeliverySecretStore(user)
async function packageFiles() {
  const bytes = sealHouseholdBootstrap(envelope, key)
  await writeFile(join(resources, 'household-bootstrap.bin'), bytes); await writeFile(join(resources, 'household-bootstrap.key'), key)
  return bytes
}
async function install() { await packageFiles(); return importInstalledHouseholdSetup({ resourcesPath: resources, userDataPath: user, smtpPort: port() }) }
async function receipt() { return JSON.parse(await readFile(join(user, 'household-provisioning-receipt.json'), 'utf8')) }

it('fresh actual safeStorage import binds the same ciphertext and returns only Wizard defaults after completion', async () => {
  expect(await install()).toMatchObject({ status: 'complete', slots: { smtpPassword: 'imported', smtpDefaults: 'imported' } })
  expect(await getHouseholdSetupDefaults(user)).toEqual({ smtp: { host: smtp.host, port: 465, username: smtp.username,
    from_address: smtp.fromAddress, use_ssl: true, starttls: false, allow_external_delivery: true }, recipient: smtp.recipient })
  expect(await readdir(resources)).toEqual([])
  const metadata = await readFile(join(user, 'household-setup-defaults.json'), 'utf8')
  const stored = JSON.parse(metadata)
  expect(stored.binding).toEqual({ bootstrapId, payloadSha256: (await receipt()).payloadSha256,
    reportCipherSha256: sha(await readFile(join(user, 'report-delivery-secrets.json'))) })
  expect(metadata).not.toContain('PUBLIC_SYNTHETIC_ONLY_PASSWORD')
  expect(await readFile(join(user, 'household-provisioning-receipt.json'), 'utf8')).not.toContain('@')
  expect((await readdir(user)).sort()).toEqual(['household-provisioning-receipt.json', 'household-setup-defaults.json', 'report-delivery-secrets.json'])
})

it('a preserved legacy secret cannot authorize newly imported sender defaults', async () => {
  await report().save({ smtpPassword: 'synthetic-bbcc5549c6e4fd500d0e' })
  expect(await install()).toMatchObject({ slots: { smtpPassword: 'preserved', smtpDefaults: 'imported' } })
  expect(await getHouseholdSetupDefaults(user)).toBeNull()
  expect(await port().inspect()).toEqual({ state: 'configured', value: smtp })
  expect((await report().inspect()).secrets.smtpPassword).toBe('synthetic-bbcc5549c6e4fd500d0e')
})

it.each(['rewrite', 'clear', 'corrupt'] as const)('invalidates reuse after a report secret %s but preserves readable defaults preferences', async mode => {
  await install()
  if (mode === 'rewrite') await report().save({ smtpPassword: 'synthetic-cb2b73f4784d004b7527' })
  else if (mode === 'clear') await report().clear(['smtpPassword'])
  else await writeFile(join(user, 'report-delivery-secrets.json'), '{"encryptedSmtpPassword":"synthetic-f2769e636fbd329138e80a76"}')
  expect(await getHouseholdSetupDefaults(user)).toBeNull()
  expect(await port().inspect()).toEqual({ state: 'configured', value: smtp })
})

it('detects a secret changing while its protected status is inspected', async () => {
  await install(); const store = report(), original = store.inspect.bind(store)
  vi.spyOn(store, 'inspect').mockImplementationOnce(async () => { const old = await original(); await report().save({ smtpPassword: 'synthetic-c4d85f1c73691437f182' }); return old })
  expect(await getHouseholdSetupDefaults(user, store)).toBeNull()
})

it.each(['smtpPassword', 'smtpDefaults'] as const)('a suppressed %s receipt closes defaults reuse', async slot => {
  await install(); await suppressHouseholdProvisioningSlots(user, [slot])
  expect(await getHouseholdSetupDefaults(user)).toBeNull(); expect((await port().inspect()).state).toBe('configured')
})

it('a mismatched bootstrap or payload origin cannot authorize an otherwise readable sender', async () => {
  await install(); const path = join(user, 'household-setup-defaults.json'), original = JSON.parse(await readFile(path, 'utf8'))
  for (const field of ['bootstrapId', 'payloadSha256']) {
    const altered = structuredClone(original); altered.binding[field] = field === 'bootstrapId' ? '22222222-2222-4222-8222-222222222222' : 'a'.repeat(64)
    await writeFile(path, JSON.stringify(altered)); expect(await getHouseholdSetupDefaults(user)).toBeNull()
    expect((await port().inspect()).state).toBe('configured')
  }
})

it('a malformed binding is not permission to replace existing readable preferences', async () => {
  await install(); const path = join(user, 'household-setup-defaults.json'), original = JSON.parse(await readFile(path, 'utf8'))
  original.binding = { unexpected: true }; await writeFile(path, JSON.stringify(original))
  expect((await port().inspect()).state).toBe('configured'); expect(await getHouseholdSetupDefaults(user)).toBeNull()
})

it('old installation without either resource remains a no-op without a receipt or credentials', async () => {
  expect(await importInstalledHouseholdSetup({ resourcesPath: resources, userDataPath: user, smtpPort: port() })).toEqual({ status: 'not_present', slots: {} })
  expect(await readdir(user)).toEqual([])
})

it.each(['missing_key', 'short_key', 'orphan_key'] as const)('incomplete or malformed installed pair %s fails before importing any setting', async mode => {
  if (mode !== 'orphan_key') await writeFile(join(resources, 'household-bootstrap.bin'), sealHouseholdBootstrap(envelope, key))
  if (mode !== 'missing_key') await writeFile(join(resources, 'household-bootstrap.key'), mode === 'short_key' ? Buffer.alloc(3) : key)
  await expect(importInstalledHouseholdSetup({ resourcesPath: resources, userDataPath: user, smtpPort: port() })).rejects.toMatchObject({ code: 'HOUSEHOLD_BOOTSTRAP_INVALID' })
  expect(await readdir(user)).toEqual([])
})

it('a completed receipt survives key loss and a replacement package without refilling a cleared credential', async () => {
  await install(); await report().clear(['smtpPassword'])
  await writeFile(join(resources, 'household-bootstrap.bin'), sealHouseholdBootstrap(envelope, key))
  expect(await importInstalledHouseholdSetup({ resourcesPath: resources, userDataPath: user, smtpPort: port() })).toMatchObject({ status: 'already_processed' })
  expect((await report().inspect()).summary.smtpPasswordConfigured).toBe(false)
  expect(await readdir(resources)).toEqual([])
})

it('a corrupt receipt remains a fixed public failure, never a source of automatic backfill', async () => {
  await packageFiles(); await writeFile(join(user, 'household-provisioning-receipt.json'), '{"slots":{"smtpPassword":["imported"]}}')
  await expect(importInstalledHouseholdSetup({ resourcesPath: resources, userDataPath: user, smtpPort: port() })).rejects.toMatchObject({ code: 'HOUSEHOLD_BOOTSTRAP_RECEIPT_UNREADABLE' })
  expect(await getHouseholdSetupDefaults(user)).toBeNull(); expect((await report().inspect()).summary.smtpPasswordConfigured).toBe(false)
})

const mainSource = () => readFileSync(resolve('src/main/index.ts'), 'utf8')
function registration(name: string): string {
  const source = mainSource(), tree = parse(source, { sourceType: 'module', plugins: ['typescript'] })
  let result: string | null = null
  const visit = (node: any): void => {
    if (!node || typeof node !== 'object') return
    const call = node.type === 'ExpressionStatement' ? node.expression : null
    if (call?.type === 'CallExpression' && call.callee?.type === 'MemberExpression'
      && call.callee.object?.name === 'ipcMain' && call.callee.property?.name === 'handle'
      && call.arguments[0]?.value === name) result = source.slice(node.start, node.end)
    for (const value of Object.values(node)) {
      if (Array.isArray(value)) value.forEach(visit)
      else if (value && typeof value === 'object') visit(value)
    }
  }
  visit(tree)
  if (!result) throw Error('PUBLIC_TEST_REGISTRATION_NOT_FOUND')
  return result
}
function evaluate(source: string, env: Record<string, unknown>) {
  const text = transformSync(source, { loader: 'ts', target: 'es2022' }).code
  return new Function(...Object.keys(env), text)(...Object.values(env))
}

it('actual Main defaults IPC rejects foreign renderers and exposes only the current nonsecret getter result', async () => {
  await install(); const handlers: Record<string, any> = {}
  evaluate(registration('household:get-setup-defaults'), { ipcMain: { handle: (name: string, callback: any) => { handlers[name] = callback } },
    primaryWindow: { isDestroyed: () => false, webContents: { id: 7 } }, getHouseholdSetupDefaults,
    app: { getPath: () => user }, reportDeliverySecrets: report() })
  expect(await handlers['household:get-setup-defaults']({ sender: { id: 8 } })).toBeNull()
  const value = await handlers['household:get-setup-defaults']({ sender: { id: 7 } })
  expect(value.smtp.username).toBe(smtp.username); expect(JSON.stringify(value)).not.toContain('PASSWORD')
})

it('actual Main report clear writes suppression before the original clear, restarts once and never refills', async () => {
  await install(); const handlers: Record<string, any> = {}, store = report(), original = store.clear.bind(store), order: string[] = []
  vi.spyOn(store, 'clear').mockImplementation(async names => { expect((await receipt()).slots.smtpPassword).toBe('suppressed'); order.push('clear'); return original(names) })
  const restart = vi.fn(async () => { order.push('restart') })
  evaluate(registration('report-delivery:clear-secrets'), { ipcMain: { handle: (name: string, callback: any) => { handlers[name] = callback } },
    reportDeliverySecrets: store, suppressHouseholdProvisioningSlots, app: { getPath: () => user }, localService: { setReportDeliverySecrets: vi.fn(), restart } })
  await handlers['report-delivery:clear-secrets']({}, ['smtpPassword'])
  expect(order).toEqual(['clear', 'restart']); expect(await getHouseholdSetupDefaults(user)).toBeNull()
  await packageFiles(); await importInstalledHouseholdSetup({ resourcesPath: resources, userDataPath: user, smtpPort: port() })
  expect((await report().inspect()).summary.smtpPasswordConfigured).toBe(false)
  await handlers['report-delivery:clear-secrets']({}, ['smtpPassword']); expect(restart).toHaveBeenCalledOnce()
})

it('a suppression failure in the actual Main clear handler cannot clear the existing protected secret', async () => {
  await install(); const handlers: Record<string, any> = {}, store = report(), clear = vi.spyOn(store, 'clear')
  evaluate(registration('report-delivery:clear-secrets'), { ipcMain: { handle: (name: string, callback: any) => { handlers[name] = callback } },
    reportDeliverySecrets: store, suppressHouseholdProvisioningSlots: vi.fn(async () => { throw Error('PUBLIC_SUPPRESSION_FAILURE') }), app: { getPath: () => user }, localService: {} })
  await expect(handlers['report-delivery:clear-secrets']({}, ['smtpPassword'])).rejects.toThrow('PUBLIC_SUPPRESSION_FAILURE')
  expect(clear).not.toHaveBeenCalled(); expect((await store.inspect()).summary.smtpPasswordConfigured).toBe(true)
})
it.each(['model:test-settings','voice:preview-settings','voice:realtime-test-settings'] as const)('blocks direct %s cloud tests while PIN recovery is active',async name=>{
  const handlers:Record<string,any>={},fetch=vi.fn(),requireNormalCloudRuntime=()=>{throw Error('ROTATION_AUTH_ONLY')}
  evaluate(registration(name),{ipcMain:{handle:(key:string,callback:any)=>{handlers[key]=callback}},requireNormalCloudRuntime,fetch})
  await expect(handlers[name]({},{})).rejects.toThrow('ROTATION_AUTH_ONLY')
  expect(fetch).not.toHaveBeenCalled()
})

it.each(['model:clear-settings', 'voice:realtime-clear-settings'] as const)('actual Main %s tombstones the same slot before its existing clear and preserves deletion scope', async name => {
  await install(); const handlers: Record<string, any> = {}, order: string[] = [], slot = name === 'model:clear-settings' ? 'model' : 'qwenRealtime'
  const clear = vi.fn(async () => { expect((await receipt()).slots[slot]).toBe('suppressed'); order.push('clear') })
  const env = { ipcMain: { handle: (key: string, callback: any) => { handlers[key] = callback } }, primaryWindow: { isDestroyed: () => false, webContents: { id: 7 } },
    cloudProviders: { mutate: (work: () => any) => work() }, modelSettings: { clear }, qwenRealtimeSettings: { clear },
    voiceSettings: { loadEffective: vi.fn(async () => null) }, localService: { setVoiceConfig: vi.fn(), setModelConfig: vi.fn() },
    suppressHouseholdProvisioningSlots, requireNormalCloudRuntime:()=>{}, app: { getPath: () => user }, restartChangedCloudProviders: vi.fn(async () => { order.push('restart') }),
    summarizeQwenRealtime: () => ({ configured: false }) }
  evaluate(registration(name), env)
  await handlers[name]({ sender: { id: 7 } })
  expect(order).toEqual(['clear', 'restart']); expect(clear).toHaveBeenCalledOnce()
  expect((await receipt()).slots.asr).toBeUndefined()
})

function startupSource(): string {
  const source = mainSource(), start = source.indexOf('    const configureAndStart = (skipRotation: boolean,'), end = source.indexOf('\n    app.on(\'activate\'', start)
  if (start < 0 || end < 0) throw Error('PUBLIC_STARTUP_SOURCE_NOT_FOUND')
  return source.slice(start, end).replace('    void cloudProviders.mutate', '    return cloudProviders.mutate')
}
it.each(['qwen','mixed'] as const)('main startup imports %s bootstrap then applies independent rotation and a later generation',async mode=>{
  const old='synthetic-bootstrap-key-only', first='synthetic-rotation-one-only', second='synthetic-rotation-two-only'
  const origin='https://synthetic.cn-beijing.maas.aliyuncs.com',baseUrl=origin+'/compatible-mode/v1'
  const config={version:1,bootstrapId,settings:{
    model:{provider:'qwen',apiKey:old,baseUrl,model:mode==='mixed'?'deepseek-v4-pro':'qwen3.7-plus',voiceIntentModel:'qwen3.8-flash',...(mode==='mixed'?{voiceIntentApiKey:first}:{})},
    asr:{provider:'dashscope',apiKey:old,baseUrl,model:'qwen3-asr-flash'},
    qwenRealtime:{apiKey:old,endpoint:origin,model:'qwen3.8-omni-flash-realtime',voice:'Tina',enabled:true},
  }}
  const modelSettings=new ModelSettingsStore(user),asrSettings=new CloudAsrSettingsStore(user)
  const qwenRealtimeSettings=new QwenRealtimeSettingsStore(user),voiceSettings=new VoiceSettingsStore(user),diagnostics=vi.fn()
  const stateApplied:any={};let backendStarts=0
  const env:any={cloudProviders:{mutate:(work:()=>any)=>work()},dataEncryptionKeyStore:{loadOrCreate:async()=> 'synthetic-data-key'},
    app:{isPackaged:true,getPath:()=>user},runtimeProfile:{mode:'formal'},process:{platform:'win32',resourcesPath:resources,env:{}},
    recoverQwenKeyRotation,importInstalledHouseholdSetup,rotateInstalledQwenKey,householdSetupDefaultsPort:port(),HouseholdProvisioningError:class extends Error{},
    modelSettings,asrSettings,qwenRealtimeSettings,voiceSettings,reportDeliverySecrets:report(),ensureDashScopeTextFallback,
    petAudioSettings:{runtimeConfig:()=>null},diagnosticRecorder:{record:diagnostics},
    localService:{setModelConfig:(v:any)=>{stateApplied.model=v},setAsrConfig:(v:any)=>{stateApplied.asr=v},setVoiceConfig:(v:any)=>{stateApplied.voice=v},
      setPetAudioConfig:vi.fn(),setDataEncryptionKey:vi.fn(),setReportDeliverySecrets:vi.fn(),async start(){backendStarts++},reportProtectedStartupFailure:vi.fn()},
    pilotTestEmail:{start:vi.fn()},QwenRotationError,QwenRotationRecovery,qwenRotationRecoveryBinding:async()=> 'synthetic-binding',ipcMain:{handle:vi.fn()},MediaKeyRecoveryRequiredError:class extends Error{},console:{error:vi.fn(),warn:vi.fn()},createWindow:vi.fn()}
  let rotationOrder=100
  const bootstrapCiphertexts:Buffer[]=[]
  const writeInstall=async(secret:string)=>{
    const wrapping=randomBytes(32)
    const bootstrapBytes=sealHouseholdBootstrap(config,key);bootstrapCiphertexts.push(bootstrapBytes)
    await writeFile(join(resources,'household-bootstrap.bin'),bootstrapBytes);await writeFile(join(resources,'household-bootstrap.key'),key)
    await writeFile(join(resources,'qwen-rotation.bin'),sealQwenRotation(randomUUID(),secret,wrapping,++rotationOrder));await writeFile(join(resources,'qwen-rotation.key'),wrapping)
  }
  await writeInstall(first);await evaluate(startupSource(),env)
  expect(backendStarts).toBe(1);expect(stateApplied.model.apiKey).toBe(mode==='mixed'?old:first);expect(stateApplied.asr.apiKey).toBe(first)
  if(mode==='mixed') expect(stateApplied.model.voiceIntentApiKey).toBe(first)
  expect((await qwenRealtimeSettings.load())?.apiKey).toBe(first);expect(stateApplied.voice.apiKey).toBe(old)
  await writeInstall(second);await evaluate(startupSource(),env)
  expect(bootstrapCiphertexts[0].equals(bootstrapCiphertexts[1])).toBe(false)
  expect(backendStarts).toBe(2);expect(stateApplied.model.apiKey).toBe(mode==='mixed'?old:second);expect(stateApplied.asr.apiKey).toBe(second)
  if(mode==='mixed') expect(stateApplied.model.voiceIntentApiKey).toBe(second)
  expect((await qwenRealtimeSettings.load())?.apiKey).toBe(second);expect(stateApplied.voice.apiKey).toBe(old)
  expect(JSON.stringify(diagnostics.mock.calls)).not.toContain(first);expect(JSON.stringify(diagnostics.mock.calls)).not.toContain(second)
  expect(await readdir(resources)).toEqual([])
})
it('actual startup retains the new package and blocks rotation when an incomplete bootstrap receipt has different ciphertext',async()=>{
  const prior=await packageFiles()
  await writeFile(join(user,'household-provisioning-receipt.json'),JSON.stringify({version:1,bootstrapId,payloadSha256:sha(prior),complete:false,slots:{}}))
  const replacement=await packageFiles();expect(replacement.equals(prior)).toBe(false)
  const rotation=vi.fn(), localService={reportProtectedStartupFailure:vi.fn()}
  const env:any={cloudProviders:{mutate:(work:()=>any)=>work()},dataEncryptionKeyStore:{loadOrCreate:async()=> 'synthetic-data-key'},
    app:{isPackaged:true,getPath:()=>user},runtimeProfile:{mode:'formal'},process:{platform:'win32',resourcesPath:resources,env:{}},
    recoverQwenKeyRotation,importInstalledHouseholdSetup,rotateInstalledQwenKey:rotation,householdSetupDefaultsPort:port(),HouseholdProvisioningError,
    localService,QwenRotationError,QwenRotationRecovery,qwenRotationRecoveryBinding:async()=> 'synthetic-binding',ipcMain:{handle:vi.fn()},
    MediaKeyRecoveryRequiredError:class extends Error{},console:{error:vi.fn(),warn:vi.fn()},createWindow:vi.fn()}
  await evaluate(startupSource(),env)
  expect(rotation).not.toHaveBeenCalled();expect(localService.reportProtectedStartupFailure).toHaveBeenCalledWith('storage')
  expect(await readFile(join(resources,'household-bootstrap.bin'))).toEqual(replacement)
  expect((await receipt()).complete).toBe(false)
})
it.each(['normal', 'data_key_failure', 'recovery_failure', 'import_failure', 'partial_import', 'rotation_failure', 'rotation_unknown'] as const)('actual Main startup %s imports after protected data key and before any settings load/backend start', async mode => {
  const order: string[] = [], settings = { load: async () => { order.push('settings'); return null }, loadOrImportDeepSeekFromMacOSKeychain: async () => { order.push('settings'); return null } }
  const env: any = {
    cloudProviders: { done: null, mutate(work: () => any) { this.done = work(); return this.done } },
    dataEncryptionKeyStore: { async loadOrCreate() { order.push('data_key'); if (mode === 'data_key_failure') throw Error('PUBLIC_DATA_KEY_FAILURE'); return 'PUBLIC_PROTECTED_DATA_KEY_RESULT' } },
    app: { isPackaged: true, getPath: () => user }, runtimeProfile: { mode: 'formal' }, process: { platform: 'win32', resourcesPath: resources, env: {} },
    async recoverQwenKeyRotation() { order.push('recovery'); if (mode === 'recovery_failure') throw Error('PUBLIC_RECOVERY_FAILURE') },
    async importInstalledHouseholdSetup() { expect(order).toEqual(['data_key','recovery']); order.push('import'); if (mode === 'import_failure') throw Error('PUBLIC_IMPORT_FAILURE'); return { status: mode === 'partial_import' ? 'partial' : 'complete' } },
    async rotateInstalledQwenKey() { order.push('rotation'); if (mode === 'rotation_failure') throw Error('PUBLIC_ROTATION_FAILURE'); if (mode === 'rotation_unknown') throw new QwenRotationError('ROTATION_MODEL_UNCLASSIFIED') },
    householdSetupDefaultsPort: port(), HouseholdProvisioningError: class extends Error {}, modelSettings: settings, asrSettings: settings,
    qwenRealtimeSettings: { async loadOrImport() { order.push('settings'); return null } }, reportDeliverySecrets: { async load() { order.push('settings'); return {} } },
    ensureDashScopeTextFallback: (value: unknown) => value, petAudioSettings: { runtimeConfig: () => null },
    voiceSettings: { loadEffective: async () => null }, diagnosticRecorder: { record: vi.fn() },
    localService: { setModelConfig: vi.fn(), setPetAudioConfig: vi.fn(), setAsrConfig: vi.fn(), setVoiceConfig: vi.fn(), setDataEncryptionKey: vi.fn(), setReportDeliverySecrets: vi.fn(),
      setRotationRecoveryGuard:vi.fn(),async start() { order.push('backend_start') }, async startRotationAuthOnly() { order.push('parent_auth_only') }, reportProtectedStartupFailure: vi.fn(() => { order.push('protected_error') }) },
    pilotTestEmail: { start: vi.fn() }, QwenRotationError,QwenRotationRecovery,qwenRotationRecoveryBinding:async()=> 'synthetic-binding',ipcMain:{handle:vi.fn()}, MediaKeyRecoveryRequiredError: class extends Error {}, console: { error: vi.fn(), warn: vi.fn() }, createWindow: vi.fn(),
  }
  await evaluate(startupSource(), env)
  expect(env.createWindow).toHaveBeenCalledOnce()
  if (mode === 'normal') {
    expect(order.slice(0, 4)).toEqual(['data_key', 'recovery', 'import', 'rotation']); expect(order.filter(item => item === 'settings')).toHaveLength(4); expect(order.at(-1)).toBe('backend_start')
  } else {
    expect(order).not.toContain('settings'); expect(order).not.toContain('backend_start'); expect(order.at(-1)).toBe(mode==='rotation_unknown'?'parent_auth_only':'protected_error')
  }
})

it('actual Main offers one parent-authorized continuation after an unknown model without replaying or deleting the installer',async()=>{
  let currentUrl=pathToFileURL(join('/synthetic-main','../renderer/index.html')).href
  const handlers:Record<string,any>={},frame={},contents={mainFrame:frame,isDestroyed:()=>false,getURL:()=>currentUrl},window={webContents:contents,isDestroyed:()=>false}
  const runtime:any={status:'stopped',runtimeEpoch:1,localApiUrl:'http://127.0.0.1:1234',sessionToken:'synthetic-session'}
  const settings={load:async()=>null},resourcesBefore=['synthetic-payload','synthetic-key']
  const state={started:0,stopped:0,warning:false}
  const env:any={cloudProviders:{mutate:(work:()=>any)=>work()},dataEncryptionKeyStore:{loadOrCreate:async()=> 'synthetic-data-key'},
    app:{isPackaged:true,getPath:()=>user},runtimeProfile:{mode:'formal'},process:{platform:'win32',resourcesPath:resources,env:{}},
    recoverQwenKeyRotation:vi.fn(),importInstalledHouseholdSetup:async()=>({status:'complete'}),
    rotateInstalledQwenKey:vi.fn(async()=>{throw new QwenRotationError('ROTATION_MODEL_UNCLASSIFIED')}),
    householdSetupDefaultsPort:port(),HouseholdProvisioningError:class extends Error{},modelSettings:settings,asrSettings:settings,
    qwenRealtimeSettings:{loadOrImport:async()=>null},voiceSettings:{loadEffective:async()=>null},reportDeliverySecrets:{load:async()=>({})},
    ensureDashScopeTextFallback:(x:any)=>x,petAudioSettings:{runtimeConfig:()=>null},diagnosticRecorder:{record:vi.fn()},
    localService:{setModelConfig:vi.fn(),setPetAudioConfig:vi.fn(),setAsrConfig:vi.fn(),setVoiceConfig:vi.fn(),setDataEncryptionKey:vi.fn(),setReportDeliverySecrets:vi.fn(),
      getRuntime:()=>({...runtime}),async startRotationAuthOnly(){runtime.status='ready';runtime.rotationAuthOnly=true},
      async stopRotationAuthOnly(){state.stopped++;runtime.status='stopped';runtime.rotationAuthOnly=false;runtime.runtimeEpoch++},
      markRotationKeyUpdatePending(value=true){state.warning=value},setRotationRecoveryGuard:vi.fn(),async start(){state.started++;runtime.status='ready'},reportProtectedStartupFailure:vi.fn()},
    pilotTestEmail:{start:vi.fn()},QwenRotationError,QwenRotationRecovery,qwenRotationRecoveryBinding:async()=>resourcesBefore.join(':'),
    ipcMain:{handle:(name:string,handler:any)=>{handlers[name]=handler}},primaryWindow:window,
    fetch:async()=>new Response('{"authorized":true}',{status:200}),AbortSignal,URL,pathToFileURL,join,__dirname:'/synthetic-main',
    MediaKeyRecoveryRequiredError:class extends Error{},console:{error:vi.fn(),warn:vi.fn()},createWindow:vi.fn()}
  await evaluate(startupSource(),env)
  expect(runtime.rotationAuthOnly).toBe(true);expect(state.started).toBe(0)
  currentUrl='https://untrusted.invalid/renderer/index.html'
  await expect(Promise.resolve().then(()=>handlers['rotation-recovery:continue']({sender:contents,senderFrame:frame},'synthetic-parent-token'))).rejects.toThrow('ROTATION_RECOVERY_UNAVAILABLE')
  currentUrl=pathToFileURL(join('/synthetic-main','../renderer/index.html')).href
  expect(await handlers['rotation-recovery:continue']({sender:contents,senderFrame:frame},'synthetic-parent-token')).toEqual({status:'completed'})
  await handlers['rotation-recovery:cancel']({sender:contents,senderFrame:frame})
  expect(state).toEqual({started:1,stopped:1,warning:true})
  expect(env.rotateInstalledQwenKey).toHaveBeenCalledOnce()
  expect(resourcesBefore).toEqual(['synthetic-payload','synthetic-key'])
})
it('actual rotation IPC returns a fixed restart-required status without forwarding internal errors',async()=>{
  const handlers:Record<string,any>={}
  evaluate(registration('rotation-recovery:continue'),{ipcMain:{handle:(name:string,handler:any)=>{handlers[name]=handler}},
    primaryWindow:{},rotationOwnerCurrent:()=>true,cloudProviders:{mutate:(work:()=>any)=>work()},
    rotationRecovery:{continueWithOldKey:async()=>{throw Error('ROTATION_RECOVERY_RESTART_REQUIRED')}}})
  expect(await handlers['rotation-recovery:continue']({senderFrame:{}},'synthetic-token')).toEqual({status:'restart_required'})
})
