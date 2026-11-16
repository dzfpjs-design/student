import { realpathSync, mkdtempSync, readFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { PetAudioSettingsCommand } from '../../shared/petAudioSettings'
const bridge = vi.hoisted(() => ({ api: null as unknown, invoke: vi.fn() }))
vi.mock('electron', () => ({ app: { getPath: () => '' }, contextBridge: { exposeInMainWorld: (_: string, api: unknown) => { bridge.api = api } },
  ipcRenderer: { invoke: (...args: unknown[]) => bridge.invoke(...args) },
  safeStorage: { isEncryptionAvailable: () => true,
    encryptString: (raw: string) => Buffer.from(Buffer.from(raw).toString('base64')),
    decryptString: (raw: Buffer) => Buffer.from(raw.toString(), 'base64').toString() }
}))
import '../../preload/index'
import { PetAudioSettingsStore, createPetAudioSettingsHandler } from '../../main/pet-audio-settings'
import { localApi, resetRuntimeCache } from './api'
import { ParentPetAudioTeaching } from './ParentPetAudioTeaching'
import { PetAudioCatalogStore } from '../../main/pet-audio-catalog'
import { canonicalPetCatalog } from '../../shared/petAudioCatalog'
let directory: string, store: PetAudioSettingsStore, loss: boolean, saves: number

it('imports a maintainer-signed directory through the real settings component, API, preload and original parent handler; only explicit Save changes configuration', async () => {
  const { createPrivateKey, createPublicKey, sign } = await import('node:crypto')
  const privateKey = createPrivateKey({ key: Buffer.concat([Buffer.from('302e020100300506032b657004220420','hex'),Buffer.alloc(32,17)]),format:'der',type:'pkcs8' })
  const trusted={ 'synthetic-only-key': { issuer:'Synthetic test maintainer',publicKeyPem:createPublicKey(privateKey).export({format:'pem',type:'spki'}).toString() } }
  const now=new Date(Date.now()), start=new Date(now.getTime()-3600000), end=new Date(start.getTime()+29*86400000)
  const stamp=(d:Date)=>d.toISOString().replace(/\.\d{3}Z$/,'Z')
  const payload={schema:'pet_audio_catalog_v1',revision:2,id:'synthetic-component-catalog',verified_at:stamp(start),review_before:stamp(end),
    region:'cn-beijing',currency:'CNY',endpoint_family:'aliyun-compatible-cn-beijing-v1',protocol:'qwen_omni_audio_json_sse_v1',model:'qwen3.5-omni-plus-2026-03-15',
    text_input_cny_per_million:'7',audio_input_cny_per_million:'53',text_output_with_audio_cny_per_million:'40',audio_tokens_per_second:7,
    source_urls:['https://help.aliyun.com/zh/model-studio/qwen3-5-omni-plus'],evidence_sha256:'a'.repeat(64)}
  const raw=Buffer.from(canonicalPetCatalog(payload)),bytes=Buffer.from(canonicalPetCatalog({schema:'pet_audio_catalog_signed_v1',key_id:'synthetic-only-key',payload_base64:raw.toString('base64'),signature_base64:sign(null,raw,privateKey).toString('base64')}))
  store=new PetAudioSettingsStore(directory,new PetAudioCatalogStore(directory,trusted))
  const chooser=vi.fn(async()=>bytes),apply=vi.fn(async()=>{})
  const handler=createPetAudioSettingsHandler(store,async(sender,child,token)=>{
    if(sender!==7||child!=='primary'||token!=='synthetic-parent')throw Error('Denied')
  },{apply,applied:()=>true,importCatalog:chooser})
  bridge.invoke.mockImplementation(async(channel:string,child:string,token:string,command:PetAudioSettingsCommand)=>{
    if(channel==='runtime:get-config')return {status:'ready',localApiUrl:'http://127.0.0.1:8765',sessionToken:'synthetic-session'}
    if(channel!=='pet-audio:settings')throw Error('Unexpected IPC')
    if(command.kind==='save')saves++
    return handler({sender:{id:7}},child,token,command)
  })
  await open()
  fireEvent.click(screen.getByRole('button',{name:'导入已核型号与费率目录'}))
  await screen.findByRole('region',{name:'待选用目录'})
  expect(chooser).toHaveBeenCalledTimes(1);expect(store.execute({kind:'read'}).revision).toBe(0);expect(saves).toBe(0);expect(apply).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button',{name:'选用此目录'}))
  expect(store.execute({kind:'read'}).revision).toBe(0);expect(saves).toBe(0)
  fireEvent.click(screen.getByRole('checkbox',{name:'启用已核验原音配置'}))
  fireEvent.click(screen.getByRole('button',{name:'保存原音配置'}))
  await screen.findByText('原音配置已保存。请核对服务应用状态，再单独确认当前孩子的用途许可与预算。')
  expect(saves).toBe(1);expect(apply).toHaveBeenCalledTimes(1)
  expect(store.execute({kind:'read'})).toMatchObject({revision:1,enabled:true,catalog:{revision:2,tariff:{id:payload.id}}})
  store=new PetAudioSettingsStore(directory,new PetAudioCatalogStore(directory,trusted))
  expect(store.execute({kind:'read'})).toMatchObject({revision:1,enabled:true,catalog:{revision:2}})
  expect(screen.queryByRole('region',{name:'待选用目录'})).not.toBeInTheDocument()
})
const stateRequest = async <T,>(path: string): Promise<T> => (path.endsWith('authorization')
  ? { child_id: 'primary', configured: false, current: false, grant: null } : { child_id: 'primary', items: [] }) as T
beforeEach(async () => {
  directory = mkdtempSync(join(realpathSync(tmpdir()), 'hsc-pet-ipc-test-')); store = new PetAudioSettingsStore(directory)
  loss = false; saves = 0; localStorage.clear(); resetRuntimeCache()
  window.desktop = bridge.api as NonNullable<typeof window.desktop>
  bridge.invoke.mockReset().mockImplementation(async (channel: string, child: string, token: string, command: PetAudioSettingsCommand) => {
    if (channel === 'runtime:get-config') return { status: 'ready', localApiUrl: 'http://127.0.0.1:8765', sessionToken: 'synthetic-session' }
    if (channel !== 'pet-audio:settings') throw Error('Unexpected IPC')
    const handler = createPetAudioSettingsHandler(store, async (sender, actualChild, actualToken) => {
      if (sender !== 7 || actualChild !== 'primary' || actualToken !== 'synthetic-parent') throw Error('Denied')
    })
    const result = await handler({ sender: { id: 7 } }, child, token, command)
    if (command.kind !== 'read') { saves++; if (loss) throw Error('Synthetic lost IPC response after durable write') }
    return result
  })
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    if (!url.endsWith('/v1/parent/unlock')) throw Error('No provider requests allowed')
    return new Response(JSON.stringify({ parent_token: 'synthetic-parent' }), { status: 200, headers: { 'Content-Type': 'application/json' } })
  }))
  await localApi.parentUnlock('synthetic-pin', false)
})
afterEach(() => { cleanup(); resetRuntimeCache(); vi.unstubAllGlobals(); rmSync(directory, { recursive: true, force: true }); localStorage.clear() })
async function open(): Promise<void> {
  render(<ParentPetAudioTeaching childId="primary" request={stateRequest} />)
  fireEvent.click(screen.getByRole('button', { name: '配置原音模型' }))
  await waitFor(() => expect(bridge.invoke).toHaveBeenCalledWith('pet-audio:settings', 'primary', 'synthetic-parent', { kind: 'read' }))
  fireEvent.change(screen.getByLabelText('原音模型名称'), { target: { value: 'synthetic-original' } })
  await waitFor(() => expect(screen.getByRole('button', { name: '保存原音配置' })).toBeEnabled())
  fireEvent.change(screen.getByLabelText('原音专属密钥'), { target: { value: 'synthetic-only-secret' } })
}
it('runs real component/API/preload/main-handler/file save; loss + store reopen recovers original action without another save', async () => {
  await open(); loss = true
  fireEvent.click(screen.getByRole('button', { name: '保存原音配置' }))
  await screen.findByText(/保存未确认或配置被拒绝/)
  const original = store.execute({ kind: 'read' })
  expect(original).toMatchObject({ revision: 1, enabled: false, configured: true, state: 'awaiting_verified_tariff' })
  expect(saves).toBe(1)
  expect(screen.getByLabelText('原音专属密钥')).toHaveValue('')
  expect(JSON.stringify(localStorage)).not.toContain('synthetic-only-secret')
  expect(readFileSync(join(directory, 'pet-audio-settings.bin')).toString()).not.toContain('synthetic-only-secret')
  cleanup(); store = new PetAudioSettingsStore(directory); loss = false
  render(<ParentPetAudioTeaching childId="primary" request={stateRequest} />)
  fireEvent.click(screen.getByRole('button', { name: '配置原音模型' }))
  await screen.findByText('原配置操作已找到，没有重新发送。')
  expect(store.execute({ kind: 'read' })).toEqual(original)
  expect(saves).toBe(1)
  expect(screen.getByRole('checkbox', { name: '允许原音练习建议' })).toBeDisabled()
  expect(fetch).toHaveBeenCalledTimes(1)
  fireEvent.click(screen.getByRole('button', { name: '清除原音密钥' }))
  await screen.findByText('原音密钥已清除，请核对服务应用状态；孩子原有用途记录保留。')
  expect(store.execute({ kind: 'read' })).toMatchObject({ revision: 2, configured: false, enabled: false })
})
it('blocks silent journal drops before IPC and never puts the key in its recovery journal', async () => {
  await open()
  const set = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => undefined)
  fireEvent.click(screen.getByRole('button', { name: '保存原音配置' }))
  await screen.findByText(/保存未确认或配置被拒绝/)
  expect(saves).toBe(0); expect(store.execute({ kind: 'read' }).revision).toBe(0)
  set.mockRestore()
})
it('privacy invalidation clears the secret and disables submission while a late read is ignored', async () => {
  await open()
  await act(async () => window.dispatchEvent(new Event('hsc-privacy-pause')))
  expect(screen.getByLabelText('原音专属密钥')).toHaveValue('')
  expect(screen.getByRole('button', { name: '保存原音配置' })).toBeDisabled()
  expect(saves).toBe(0)
})
it('does not remove a concurrent new journal when an earlier successful save returns late', async () => {
  await open()
  const real = bridge.invoke.getMockImplementation()!
  let release!: () => void
  const wait = new Promise<void>(resolve => { release = resolve })
  bridge.invoke.mockImplementation(async (...args: unknown[]) => {
    const result = await real(...args)
    if ((args[3] as PetAudioSettingsCommand)?.kind === 'save') await wait
    return result
  })
  fireEvent.click(screen.getByRole('button', { name: '保存原音配置' }))
  await waitFor(() => expect(saves).toBe(1))
  const key = Object.keys(localStorage).find(value => value.endsWith('original-audio-settings'))!
  const newer = JSON.stringify({ actionId: 'another-parent-action', expectedRevision: 1 })
  localStorage.setItem(key, newer)
  await act(async () => release())
  expect(localStorage.getItem(key)).toBe(newer)
  expect(saves).toBe(1)
})
it('ignores a late successful response after runtime invalidation and keeps the original recovery key', async () => {
  await open()
  const real = bridge.invoke.getMockImplementation()!
  let release!: () => void
  const wait = new Promise<void>(resolve => { release = resolve })
  bridge.invoke.mockImplementation(async (...args: unknown[]) => {
    const result = await real(...args)
    if ((args[3] as PetAudioSettingsCommand)?.kind === 'save') await wait
    return result
  })
  fireEvent.click(screen.getByRole('button', { name: '保存原音配置' }))
  await waitFor(() => expect(saves).toBe(1))
  const before = JSON.stringify(localStorage)
  await act(async () => { window.dispatchEvent(new Event('hsc-runtime-restarted')); release() })
  expect(JSON.stringify(localStorage)).toBe(before)
  expect(screen.getByRole('button', { name: '保存原音配置' })).toBeDisabled()
  expect(screen.queryByText(/配置已加密保存，等待/)).not.toBeInTheDocument()
})
