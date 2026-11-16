// @vitest-environment node
import { EventEmitter } from 'node:events'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

const spawned = vi.hoisted(() => [] as Array<{ env: NodeJS.ProcessEnv; child: EventEmitter & { exitCode: number | null; stdout: EventEmitter; stderr: EventEmitter; kill: () => boolean } }>)
vi.mock('electron', () => ({ app: { getVersion: () => '0.1.4', getAppPath: () => process.cwd(), getPath: () => '/tmp/cloud-provider-synthetic-only', isPackaged: false } }))
vi.mock('node:net', () => ({ createServer: () => ({ once() {}, listen(_port: number, _host: string, ready: () => void) { ready() }, address: () => ({ port: 12345 }), close: (done?: () => void) => done?.() }) }))
vi.mock('node:child_process', () => ({ spawn: vi.fn((_exe, _args, options) => {
  const child = Object.assign(new EventEmitter(), { exitCode: null as number | null, stdout: new EventEmitter(), stderr: new EventEmitter(), kill: () => { child.exitCode = 0; child.emit('exit', 0, null);child.emit('close', 0, null); return true } })
  spawned.push({ env: options.env, child }); return child
}) }))
import { LocalServiceManager } from './local-service'

const nulls = { modelConfig: null, asrConfig: null, voiceConfig: null }
const saved = {
  modelConfig: { provider: 'deepseek' as const, apiKey: 'synthetic-model', baseUrl: 'http://127.0.0.1:1234/primary', model: 'public-model', voiceIntentModel: 'public-intent',
    fallback: { provider: 'qwen' as const, apiKey: 'synthetic-fallback', baseUrl: 'http://127.0.0.1:1234/fallback', model: 'public-fallback' } },
  asrConfig: { provider: 'dashscope' as const, apiKey: 'synthetic-asr', baseUrl: 'https://dashscope.aliyuncs.com/compatible-mode/v1', model: 'public-asr' },
  voiceConfig: { provider: 'aliyun_cosyvoice' as const, apiKey: 'synthetic-voice', relayUrl: '', voiceId: 'public-voice' },
}
const managers: LocalServiceManager[] = []
beforeEach(() => {
  spawned.length = 0; vi.stubEnv('HSC_LOCAL_AGENT_PYTHON', process.execPath); vi.stubEnv('HSC_LOCAL_API_URL', '')
  vi.stubGlobal('fetch', vi.fn(async (input: unknown) => {
    if (String(input).endsWith('/shutdown')) { const child = spawned.at(-1)?.child; if (child) { child.exitCode = 0; child.emit('exit', 0, null);child.emit('close', 0, null) } }
    return new Response('{"build_component":"home-study-local-agent","build_version":"0.1.4"}', { status: 200 })
  }))
})
afterEach(async () => { vi.mocked(fetch).mockImplementation(async input=>{if(String(input).endsWith('/shutdown'))finishSyntheticCloudChild();return new Response('{"build_component":"home-study-local-agent","build_version":"0.1.4"}',{status:200})}); for (const manager of managers.splice(0)) await manager.stop(); vi.useRealTimers(); vi.unstubAllGlobals(); vi.unstubAllEnvs() })
function manager() { const value = new LocalServiceManager(); value.setDataEncryptionKey('synthetic-data-key'); managers.push(value); return value }
it('actual mocked spawn consumes exact cloud snapshot and a subsequent all-null snapshot clears stale providers in a fresh child', async () => {
  const m = manager(); m.setReportDeliverySecrets({ smtpPassword: 'synthetic-delivery' })
  await m.start()
  const first = await m.restartCloudProviderSnapshot(saved, m.admitCloudProviderRefresh())
  expect(m.cloudProviderSnapshotApplied(first)).toBe(true)
  expect(spawned[1].env).toMatchObject({ HSC_MODEL_BASE_URL: saved.modelConfig.baseUrl, HSC_FALLBACK_MODEL_BASE_URL: saved.modelConfig.fallback.baseUrl,
    HSC_MODEL_API_KEY: 'synthetic-model', HSC_ASR_API_KEY: 'synthetic-asr', HSC_TTS_API_KEY: 'synthetic-voice', HSC_REPORT_DELIVERY_EMAIL_PASSWORD: 'synthetic-delivery', HSC_DATA_ENCRYPTION_KEY: 'synthetic-data-key' })
  const second = await m.restartCloudProviderSnapshot(nulls, m.admitCloudProviderRefresh())
  expect(second).not.toBe(first); expect(m.cloudProviderSnapshotApplied(first)).toBe(false); expect(m.cloudProviderSnapshotApplied(second)).toBe(true)
  for (const name of ['HSC_MODEL_API_KEY', 'HSC_FALLBACK_MODEL_API_KEY', 'HSC_ASR_API_KEY', 'HSC_TTS_API_KEY']) expect(spawned[2].env[name]).toBeUndefined()
  expect(spawned[2].env.HSC_REPORT_DELIVERY_EMAIL_PASSWORD).toBe('synthetic-delivery')
  expect(JSON.stringify(m.getRuntime())).not.toContain('synthetic-')
})
it.each(['restartPromise', 'startPromise', 'recoveryTimer', 'healthAbort', 'cloudProviderStops'])('rejects %s before committing any provider cache', async (field) => {
  const m = manager(); const admission = m.admitCloudProviderRefresh(); const state = m as any
  state[field] = field === 'cloudProviderStops' ? 1 : {}
  await expect(m.restartCloudProviderSnapshot(saved, admission)).rejects.toThrow('CLOUD_PROVIDER_REFRESH_BUSY')
  expect(m.getCloudProviderRevision()).toBe(0); expect(spawned).toHaveLength(0)
  state[field] = field === 'cloudProviderStops' ? 0 : null
})
it('rejects a changed child epoch or cache revision during readonly loads', async () => {
  const m = manager(); const admission = m.admitCloudProviderRefresh(); await m.start()
  await expect(m.restartCloudProviderSnapshot(saved, admission)).rejects.toThrow('CLOUD_PROVIDER_RUNTIME_CHANGED')
  const current = m.admitCloudProviderRefresh(); m.setModelConfig(saved.modelConfig)
  await expect(m.restartCloudProviderSnapshot(nulls, current)).rejects.toThrow('CLOUD_PROVIDER_RUNTIME_CHANGED')
  expect(spawned).toHaveLength(1)
})
it('never credits a ready child with a cache changed after its actual spawn', async () => {
  const m = manager(); let release!: (response: Response) => void
  vi.mocked(fetch).mockImplementationOnce(async () => new Promise<Response>(resolve => { release = resolve }))
  const started = m.start()
  while (!release) await Promise.resolve()
  expect(() => m.admitCloudProviderRefresh()).toThrow('CLOUD_PROVIDER_REFRESH_BUSY')
  m.setModelConfig(saved.modelConfig); const revision = m.getCloudProviderRevision()
  release(new Response('{"build_component":"home-study-local-agent","build_version":"0.1.4"}', { status: 200 })); await started
  expect(m.getRuntime().status).toBe('ready'); expect(spawned[0].env.HSC_MODEL_API_KEY).toBeUndefined()
  expect(m.cloudProviderRevisionApplied(revision)).toBe(false)
})
it('does not credit a new cache revision from an old coalesced restart', async () => {
  const m = manager(); await m.start()
  let release!: (response: Response) => void
  vi.mocked(fetch).mockImplementation(async input => {
    if (String(input).endsWith('/shutdown')) { const child = spawned.at(-1)!.child; child.exitCode = 0; child.emit('exit', 0, null);child.emit('close', 0, null); return new Response('{"build_component":"home-study-local-agent","build_version":"0.1.4"}', { status: 200 }) }
    return new Promise<Response>(resolve => { release = resolve })
  })
  const first = m.restart(); while (!release) await Promise.resolve()
  m.setVoiceConfig(saved.voiceConfig); const revision = m.getCloudProviderRevision(); const coalesced = m.restart()
  release(new Response('{"build_component":"home-study-local-agent","build_version":"0.1.4"}', { status: 200 })); await Promise.all([first, coalesced])
  expect(spawned).toHaveLength(2); expect(spawned[1].env.HSC_TTS_API_KEY).toBeUndefined(); expect(m.cloudProviderRevisionApplied(revision)).toBe(false)
})
it('invalidates applied credit on actual child exit and rejects its scheduled recovery', async () => {
  const m = manager(); const token = await m.restartCloudProviderSnapshot(saved, m.admitCloudProviderRefresh())
  const child = spawned.at(-1)!.child; child.exitCode = 1; child.emit('exit', 1, null);child.emit('close', 1, null)
  expect(m.cloudProviderSnapshotApplied(token)).toBe(false)
  expect(() => m.admitCloudProviderRefresh()).toThrow('CLOUD_PROVIDER_REFRESH_BUSY')
})
it('never refreshes externally managed or protected-failed runtime caches', async () => {
  const m = manager(); vi.stubEnv('HSC_LOCAL_API_URL', 'http://127.0.0.1:1234')
  expect(() => m.admitCloudProviderRefresh()).toThrow('CLOUD_PROVIDER_REFRESH_BUSY')
  vi.stubEnv('HSC_LOCAL_API_URL', ''); m.reportProtectedStartupFailure()
  expect(() => m.admitCloudProviderRefresh()).toThrow('CLOUD_PROVIDER_REFRESH_BUSY'); expect(m.getCloudProviderRevision()).toBe(0)
})

it('allows explicit refresh after an old stop has actually settled, without borrowing its previous child', async () => {
  const m = manager(); await m.start(); await m.stop()
  const token = await m.restartCloudProviderSnapshot(nulls, m.admitCloudProviderRefresh())
  expect(spawned).toHaveLength(2); expect(m.cloudProviderSnapshotApplied(token)).toBe(true)
})

function finishSyntheticCloudChild():void { const child=spawned.at(-1)?.child;if(child){child.exitCode=0;child.emit('exit', 0, null);child.emit('close', 0, null)} }
it('actual pending stop forbids cache admission until that owned stop and child exit settle',async()=>{
 const m=manager(),old=await m.restartCloudProviderSnapshot(nulls,m.admitCloudProviderRefresh());let release!:()=>void
 vi.mocked(fetch).mockImplementation(async input=>String(input).endsWith('/shutdown')?new Promise<Response>(r=>{release=()=>{finishSyntheticCloudChild();r(new Response('{"build_component":"home-study-local-agent","build_version":"0.1.4"}',{status:200}))}}):new Response('{"build_component":"home-study-local-agent","build_version":"0.1.4"}',{status:200}))
 const stop=m.stop();while(!release)await Promise.resolve();expect(()=>m.admitCloudProviderRefresh()).toThrow('CLOUD_PROVIDER_REFRESH_BUSY');expect(m.cloudProviderSnapshotApplied(old)).toBe(false)
 release();await stop;const next=await m.restartCloudProviderSnapshot(nulls,m.admitCloudProviderRefresh());expect(next).not.toBe(old);expect(m.cloudProviderSnapshotApplied(next)).toBe(true);expect(spawned).toHaveLength(2)
})
it('PET setter coalesced after cloud child spawn cannot pretend its newer action was applied',async()=>{
 const m=manager();m.setPetAudioConfig({purpose:'pet_exam_audio',baseUrl:'',model:'',apiKey:'SYNTHETIC-PET',enabled:false,revision:1,actionId:'SYNTHETIC-old-PET'} as any);await m.start();let release!:(r:Response)=>void
 vi.mocked(fetch).mockImplementation(async input=>{if(String(input).endsWith('/shutdown')){finishSyntheticCloudChild();return new Response('{"build_component":"home-study-local-agent","build_version":"0.1.4"}',{status:200})}return new Promise<Response>(r=>{release=r})})
 const refreshing=m.restartCloudProviderSnapshot(nulls,m.admitCloudProviderRefresh());while(!release)await Promise.resolve()
 m.setPetAudioConfig({purpose:'pet_exam_audio',baseUrl:'',model:'',apiKey:'SYNTHETIC-PET',enabled:false,revision:2,actionId:'SYNTHETIC-new-PET'} as any);const coalesced=m.restart()
 release(new Response('{"build_component":"home-study-local-agent","build_version":"0.1.4"}',{status:200}));const token=await refreshing;await coalesced
 expect(m.cloudProviderSnapshotApplied(token)).toBe(true);expect(m.petAudioConfigApplied('SYNTHETIC-new-PET')).toBe(false);expect(m.petAudioConfigApplied('SYNTHETIC-old-PET')).toBe(true);expect(spawned).toHaveLength(2)
 vi.mocked(fetch).mockImplementation(async input=>{if(String(input).endsWith('/shutdown'))finishSyntheticCloudChild();return new Response('{"build_component":"home-study-local-agent","build_version":"0.1.4"}',{status:200})})
})
it('actual child exit during refreshed health wait invalidates token and rejects refresh',async()=>{
 const m=manager();await m.start();let health=false
 vi.mocked(fetch).mockImplementation(async input=>{if(String(input).endsWith('/shutdown')){finishSyntheticCloudChild();return new Response('{"build_component":"home-study-local-agent","build_version":"0.1.4"}',{status:200})}health=true;return new Promise<Response>(()=>{})})
 const refreshing=m.restartCloudProviderSnapshot(nulls,m.admitCloudProviderRefresh()),rejected=expect(refreshing).rejects.toThrow('本地学习服务启动已取消');while(!health)await Promise.resolve()
 spawned.at(-1)!.child.exitCode=1;spawned.at(-1)!.child.emit('exit', 1, null);spawned.at(-1)!.child.emit('close', 1, null);await rejected;expect(m.cloudProviderRevisionApplied(m.getCloudProviderRevision())).toBe(false);expect(()=>m.admitCloudProviderRefresh()).toThrow('CLOUD_PROVIDER_REFRESH_BUSY')
})
