// @vitest-environment node
import { it, expect, vi } from 'vitest'
import { readFileSync } from 'node:fs'
import { mkdtemp, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { runInNewContext } from 'node:vm'
import { parse } from '@babel/parser'
import { transformSync } from 'esbuild'

function callback() {
  const raw = readFileSync(resolve('src/main/index.ts'), 'utf8')
  const tree = parse(raw, { sourceType: 'module', plugins: ['typescript'] })
  let callbackText: string | undefined
  function walk(node: any) {
    if (!node || typeof node !== 'object') return
    if (node.type === 'CallExpression' && node.callee?.object?.name === 'ipcMain'
      && node.callee?.property?.name === 'handle' && node.arguments?.[0]?.value === 'runtime:restart') {
      const fn = node.arguments[1]; callbackText = raw.slice(fn.start, fn.end)
    }
    for (const value of Object.values(node)) {
      if (Array.isArray(value)) value.forEach(walk)
      else if (value && typeof value === 'object') walk(value)
    }
  }
  walk(tree); if (!callbackText) throw Error('Actual runtime handler missing')
  return transformSync('handler=' + callbackText, { loader: 'ts', target: 'es2022' }).code
}
async function fixture(saved: boolean) {
  const directory = await mkdtemp(join(tmpdir(), 'provider-refresh-public-fixture-'))
  if (saved) await writeFile(join(directory, 'model-settings.json'), '{"encryptedApiKey":"synthetic-ciphertext"}')
  const model = saved ? { provider: 'deepseek', apiKey: 'synthetic-key', baseUrl: 'http://127.0.0.1:1234/primary', model: 'public-model',
    fallback: { provider: 'qwen', apiKey: 'synthetic-fallback', baseUrl: 'http://127.0.0.1:1234/fallback', model: 'public-fallback' } } : null
  const asr = saved ? { provider: 'dashscope', apiKey: 'synthetic-asr', baseUrl: 'https://dashscope.aliyuncs.com/compatible-mode/v1', model: 'public-asr' } : null
  const voice = saved ? { provider: 'aliyun_cosyvoice', apiKey: 'synthetic-voice', relayUrl: '', voiceId: 'public-voice' } : null
  const tutorExplanationConfig = saved ? { apiKey: 'synthetic-tutor-key', baseUrl: 'https://tutor.example.test/v1', model: 'deepseek-test' } : null
  let cached: unknown = { modelConfig: { apiKey: 'synthetic-old' }, asrConfig: { apiKey: 'synthetic-old' }, voiceConfig: { apiKey: 'synthetic-old' } }
  let token: unknown
  const manager = {
    getRuntime: () => ({ status: 'ready', localApiUrl: 'http://127.0.0.1:1234', sessionToken: 'synthetic-session' }),
    restart: vi.fn(async () => {}),
    admitCloudProviderRefresh: vi.fn(() => ({ child: 'synthetic-original', epoch: 1 })),
    restartCloudProviderSnapshot: vi.fn(async snapshot => { cached = snapshot; token = { child: 'synthetic-fresh', epoch: 2, revision: 1 }; return token }),
    cloudProviderSnapshotApplied: vi.fn(value => value === token)
  }
  const modelSettings = { load: vi.fn(async () => model) }
  const asrSettings = { load: vi.fn(async () => asr) }
  const qwenRealtimeSettings = { load: vi.fn(async () => saved ? { enabled: true } : null) }
  const voiceSettings = { loadEffective: vi.fn(async () => voice) }
  const context: Record<string, unknown> = { app: { getPath: () => directory }, process: { env: {} },
    primaryWindow: { isDestroyed: () => false, webContents: { id: 1 } },
    localService: manager, modelSettings, asrSettings, qwenRealtimeSettings, voiceSettings, loadTutorExplanationConfig: async () => tutorExplanationConfig }
  const module = await import('./runtime-provider-refresh')
  context.cloudProviders = new module.RuntimeProviderRefresh()
  context.cloudProviderSettingsFingerprint = module.cloudProviderSettingsFingerprint
  runInNewContext(callback(), context)
  return { context, manager, modelSettings, asrSettings, voiceSettings, expected: { modelConfig: model, asrConfig: asr, voiceConfig: voice, tutorExplanationConfig }, cached: () => cached,
    close: () => rm(directory, { recursive: true, force: true }) }
}
it('actual existing runtime handler reloads saved primary/fallback, ASR and effective voice without saving', async () => {
  const f = await fixture(true)
  try {
    await (f.context.handler as Function)({ sender: { id: 1 } })
    expect(f.modelSettings.load).toHaveBeenCalledOnce()
    expect(f.asrSettings.load).toHaveBeenCalledOnce()
    expect(f.voiceSettings.loadEffective).toHaveBeenCalledWith({ enabled: true })
    expect(f.cached()).toEqual(f.expected)
    expect(f.manager.restartCloudProviderSnapshot).toHaveBeenCalledOnce()
  } finally { await f.close() }
})
it('actual existing runtime handler clears all stale cloud caches when saved records are absent', async () => {
  const f = await fixture(false)
  try {
    await (f.context.handler as Function)({ sender: { id: 1 } })
    expect(f.cached()).toEqual({ modelConfig: null, asrConfig: null, voiceConfig: null, tutorExplanationConfig: null })
  } finally { await f.close() }
})
it.each(['old restart', 'old start', 'scheduled recovery'])('actual runtime refuses %s admission instead of returning its old ready state', async () => {
  const f = await fixture(true)
  try {
    f.manager.admitCloudProviderRefresh.mockImplementation(() => { throw Error('CLOUD_PROVIDER_REFRESH_BUSY') })
    await expect((f.context.handler as Function)({ sender: { id: 1 } })).rejects.toThrow('CLOUD_PROVIDER_REFRESH_BUSY')
    expect(f.modelSettings.load).not.toHaveBeenCalled(); expect(f.manager.restartCloudProviderSnapshot).not.toHaveBeenCalled()
  } finally { await f.close() }
})
it('actual runtime rejects a foreign sender before decrypting settings', async () => {
  const f = await fixture(true)
  try {
    await expect((f.context.handler as Function)({ sender: { id: 2 } })).rejects.toThrow('CLOUD_PROVIDER_REFRESH_SCOPE_CHANGED')
    expect(f.modelSettings.load).not.toHaveBeenCalled(); expect(f.manager.restart).not.toHaveBeenCalled()
  } finally { await f.close() }
})
it('actual external-managed restart stays health-only and does not reload desktop providers', async () => {
  const f = await fixture(true)
  try {
    (f.context.process as { env: Record<string, string> }).env.HSC_LOCAL_API_URL = 'http://127.0.0.1:1234'
    await (f.context.handler as Function)({ sender: { id: 1 } })
    expect(f.manager.restart).toHaveBeenCalledOnce(); expect(f.modelSettings.load).not.toHaveBeenCalled()
    expect(f.manager.restartCloudProviderSnapshot).not.toHaveBeenCalled()
  } finally { await f.close() }
})

it('all seven actual cloud mutation callbacks structurally use the shared serialization coordinator',()=>{
 const source=readFileSync(resolve('src/main/index.ts'),'utf8'),tree=parse(source,{sourceType:'module',plugins:['typescript']}),names=['voice:realtime-save-settings','voice:realtime-clear-settings','voice:save-settings','voice:clear-settings','voice:enroll-reference','model:save-settings','model:clear-settings'],seen:string[]=[]
 const walk=(node:any)=>{if(!node||typeof node!=='object')return;if(node.type==='CallExpression'&&node.callee?.object?.name==='ipcMain'&&node.callee?.property?.name==='handle'&&names.includes(node.arguments?.[0]?.value)){const body=node.arguments[1].body;expect(body.type).toBe('CallExpression');expect(body.callee.object.name).toBe('cloudProviders');expect(body.callee.property.name).toBe('mutate');seen.push(node.arguments[0].value)}for(const v of Object.values(node)){if(Array.isArray(v))v.forEach(walk);else if(v&&typeof v==='object')walk(v)}}
 walk(tree);expect(seen.sort()).toEqual(names.sort())
})
