import { createPrivateKey, createPublicKey, sign, createHash } from 'node:crypto'
import { mkdtempSync, readFileSync, realpathSync, readdirSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
vi.mock('electron', () => ({ app: { getPath: () => '' }, safeStorage: {
  isEncryptionAvailable: () => true,
  encryptString: (s: string) => Buffer.from('SYNTHETIC-DPAPI:' + Buffer.from(s).toString('base64')),
  decryptString: (b: Buffer) => { const s = b.toString(); if (!s.startsWith('SYNTHETIC-DPAPI:')) throw Error('unavailable'); return Buffer.from(s.slice(16), 'base64').toString() }
} }))
import { canonicalPetCatalog } from '../shared/petAudioCatalog'
import { PET_AUDIO_TARIFF } from '../shared/petAudioTariffs'
import { PET_AUDIO_PURPOSE } from '../shared/petAudioSettings'
import { PetAudioCatalogStore, verifyPetCatalog } from './pet-audio-catalog'
import { PetAudioSettingsStore, createPetAudioSettingsHandler } from './pet-audio-settings'
import { safeStorage } from 'electron'
const privateKey = createPrivateKey({ key: Buffer.concat([Buffer.from('302e020100300506032b657004220420','hex'), Buffer.alloc(32,17)]), format: 'der', type: 'pkcs8' })
const trusted = { 'synthetic-only-key': { issuer: 'Synthetic test maintainer', publicKeyPem: createPublicKey(privateKey).export({ format: 'pem', type: 'spki' }).toString() } }
const payload = (revision = 2) => ({ schema: 'pet_audio_catalog_v1', revision, id: `synthetic-catalog-${revision}`, verified_at: '2026-10-24T05:04:30Z', review_before: '2026-11-23T05:04:30Z',
  region: 'cn-beijing', currency: 'CNY', endpoint_family: 'aliyun-compatible-cn-beijing-v1', protocol: 'qwen_omni_audio_json_sse_v1', model: 'qwen3.5-omni-plus-2026-03-15',
  text_input_cny_per_million: '7', audio_input_cny_per_million: '53', text_output_with_audio_cny_per_million: '40', audio_tokens_per_second: 7,
  source_urls: ['https://help.aliyun.com/zh/model-studio/qwen3-5-omni-plus','https://help.aliyun.com/zh/model-studio/qwen-omni'], evidence_sha256: 'a'.repeat(64) })
const bundle = (p = payload()) => { const b = Buffer.from(canonicalPetCatalog(p)); return Buffer.from(canonicalPetCatalog({ schema: 'pet_audio_catalog_signed_v1', key_id: 'synthetic-only-key', payload_base64: b.toString('base64'), signature_base64: sign(null,b,privateKey).toString('base64') })) }
let directory: string
beforeEach(() => { directory = mkdtempSync(join(realpathSync(tmpdir()),'hsc-signed-catalog-')); vi.spyOn(Date,'now').mockReturnValue(Date.parse('2026-10-24T12:00:00Z')) })
afterEach(() => { vi.restoreAllMocks(); rmSync(directory,{recursive:true,force:true}) })
it('keeps the existing compiled catalogue active with empty new trust root; unsigned or altered imports never gain trust', () => {
  const store = new PetAudioSettingsStore(directory)
  const saved = store.execute({ kind:'save',expectedRevision:0,actionId:'existing-configuration',config:{purpose:PET_AUDIO_PURPOSE,baseUrl:'https://dashscope.aliyuncs.com/compatible-mode/v1',model:PET_AUDIO_TARIFF.model,apiKey:'synthetic-secret',enabled:true,tariffId:PET_AUDIO_TARIFF.id} })
  expect(saved.enabled).toBe(true)
  expect(() => store.catalogs.preview(bundle(),'parent')).toThrow('SIGNER_NOT_CONFIGURED')
  expect(store.execute({kind:'read'})).toEqual(saved)
  const changed=JSON.parse(bundle().toString());changed.signature_base64=Buffer.alloc(64).toString('base64')
  expect(() => verifyPetCatalog(Buffer.from(canonicalPetCatalog(changed)),Date.now(),trusted)).toThrow('SIGNATURE')
  for(const extra of [{region:'us-east-1'},{currency:'USD'},{protocol:'unapproved'},{review_before:'2026-11-24T05:04:30Z'},{text_input_cny_per_million:'0'},{text_input_cny_per_million:7},{model:'qwen-latest'}])
    expect(() => verifyPetCatalog(bundle({...payload(),...extra} as ReturnType<typeof payload>),Date.now(),trusted)).toThrow()
  const good=verifyPetCatalog(bundle(),Date.now(),trusted)
  const golden=readFileSync(join(process.cwd(),'src/test-fixtures/pet-reviewed-synthetic.hscatalog'))
  expect(verifyPetCatalog(golden,Date.now(),trusted).sha256).toBe(createHash('sha256').update(bundle()).digest('hex'))
  expect(()=>verifyPetCatalog(Buffer.from('{"schema":"untrusted",'+golden.toString().slice(1)),Date.now(),trusted)).toThrow('ENVELOPE_CANONICAL')
  expect([good.tariff.inputSecondMicros,good.tariff.inputTextTokenMicros,good.tariff.outputTokenMicros]).toEqual([371,7,40])
  expect(verifyPetCatalog(bundle({...payload(),text_input_cny_per_million:'7.000001'}),Date.now(),trusted).tariff.inputTextTokenMicros).toBe(8)
})
it('native-import preview is parent-owned and zero configuration writes; exact CAS selection persists and reopens without inheriting a grant', async () => {
  const catalogs=new PetAudioCatalogStore(directory,trusted),store=new PetAudioSettingsStore(directory,catalogs), chooser=vi.fn(async()=>bundle()),applied=vi.fn(async()=>{})
  const auth=vi.fn(async(sender:number,child:string,token:string)=>{if(sender!==7||child!=='primary'||token!=='parent')throw Error('denied')})
  const handler=createPetAudioSettingsHandler(store,auth,{apply:applied,applied:()=>true,importCatalog:chooser})
  await expect(handler({sender:{id:8}},'primary','parent',{kind:'previewCatalog'})).rejects.toThrow('denied');expect(chooser).not.toHaveBeenCalled()
  const preview=(await handler({sender:{id:7}},'primary','parent',{kind:'previewCatalog'})).catalogPreview!
  expect(auth).toHaveBeenCalledTimes(3);expect(store.execute({kind:'read'}).revision).toBe(0);expect(readdirSync(directory)).toEqual([]);expect(applied).not.toHaveBeenCalled()
  const command={kind:'save' as const,expectedRevision:0,actionId:'signed-selection-first',catalogToken:preview.token,config:{purpose:PET_AUDIO_PURPOSE,baseUrl:'https://dashscope.aliyuncs.com/compatible-mode/v1',model:preview.tariff.model,apiKey:'synthetic-secret',enabled:true,tariffId:preview.tariff.id}}
  await expect(handler({sender:{id:7}},'primary','wrong',command)).rejects.toThrow('denied')
  const result=await handler({sender:{id:7}},'primary','parent',command)
  expect(result).toMatchObject({enabled:true,revision:1,catalog:{revision:2,sha256:preview.sha256}});expect(applied).toHaveBeenCalledTimes(1)
  expect(new PetAudioSettingsStore(directory,new PetAudioCatalogStore(directory,trusted)).execute({kind:'read'})).toEqual(store.execute({kind:'read'}))
  expect(JSON.stringify(result)).not.toContain('synthetic-secret');expect(readFileSync(join(directory,'pet-audio-settings.bin')).toString()).not.toContain('synthetic-secret')
  await expect(handler({sender:{id:7}},'primary','parent',command)).rejects.toThrow('版本已变化');expect(applied).toHaveBeenCalledTimes(1)
  expect(store.runtimeConfig()?.catalog?.sha256).toBe(preview.sha256)
})
it('separate protected revision floor survives business/config restore and partial save, rejects rollback/conflicting same revision, and recovers by explicit CAS', () => {
  const catalogs=new PetAudioCatalogStore(directory,trusted),store=new PetAudioSettingsStore(directory,catalogs)
  store.execute({kind:'save',expectedRevision:0,actionId:'old-configuration',config:{purpose:PET_AUDIO_PURPOSE,baseUrl:'https://dashscope.aliyuncs.com/compatible-mode/v1',model:PET_AUDIO_TARIFF.model,apiKey:'synthetic-secret',enabled:true,tariffId:PET_AUDIO_TARIFF.id}})
  const old=readFileSync(join(directory,'pet-audio-settings.bin')),preview=catalogs.preview(bundle(),'owned-parent')
  catalogs.activate(preview.token,'owned-parent') // exact crash boundary: floor committed, original config replacement not reached.
  expect(store.execute({kind:'read'})).toMatchObject({enabled:false,state:'catalog_recovery_required',revision:1})
  writeFileSync(join(directory,'pet-audio-settings.bin'),old) // simulated business restore cannot replace the separate control record.
  expect(new PetAudioSettingsStore(directory,new PetAudioCatalogStore(directory,trusted)).execute({kind:'read'}).enabled).toBe(false)
  expect(()=>catalogs.preview(bundle(payload(1)),'owned-parent')).toThrow('ROLLBACK')
  expect(()=>catalogs.preview(bundle({...payload(),text_input_cny_per_million:'8'}),'owned-parent')).toThrow('ROLLBACK')
  expect(()=>catalogs.activate(preview.token,'another-child')).toThrow('STALE')
  const result=store.execute({kind:'save',expectedRevision:1,actionId:'recovered-configuration',catalogToken:preview.token,config:{purpose:PET_AUDIO_PURPOSE,baseUrl:'https://dashscope.aliyuncs.com/compatible-mode/v1',model:preview.tariff.model,apiKey:'',enabled:true,tariffId:preview.tariff.id}},'owned-parent')
  expect(result).toMatchObject({revision:2,enabled:true,catalog:{revision:2}})
  const raw=readFileSync(store.runtimeConfig()!.catalog!.file);expect(createHash('sha256').update(raw).digest('hex')).toBe(preview.sha256)
  vi.spyOn(Date,'now').mockReturnValue(Date.parse('2026-11-23T05:04:30Z'));expect(store.execute({kind:'read'}).enabled).toBe(false)
})
it('revocation during the native chooser and stale CAS refuse before protected revision advancement', async () => {
  const catalogs=new PetAudioCatalogStore(directory,trusted),store=new PetAudioSettingsStore(directory,catalogs)
  let allowed=true
  const handler=createPetAudioSettingsHandler(store,async()=>{if(!allowed)throw Error('revoked')},
    {apply:async()=>{},applied:()=>true,importCatalog:async()=>{allowed=false;return bundle()}})
  await expect(handler({sender:{id:7}},'primary','parent',{kind:'previewCatalog'})).rejects.toThrow('revoked')
  expect(readdirSync(directory)).toEqual([])
  const first=catalogs.preview(bundle(),'parent'),next=catalogs.preview(bundle(payload(3)),'parent')
  catalogs.activate(first.token,'parent')
  expect(()=>store.execute({kind:'save',expectedRevision:99,actionId:'stale-save-action',catalogToken:next.token,
    config:{purpose:PET_AUDIO_PURPOSE,baseUrl:'https://dashscope.aliyuncs.com/compatible-mode/v1',model:next.tariff.model,apiKey:'synthetic-secret',enabled:true,tariffId:next.tariff.id}},'parent')).toThrow('版本已变化')
  expect(catalogs.current(catalogs.activate(first.token,'parent')).payload.revision).toBe(2)
})
it('missing protected floor never reinitializes from an older signed import; expiry cannot fall back to a same-named built-in tariff', () => {
  const catalogs=new PetAudioCatalogStore(directory,trusted),store=new PetAudioSettingsStore(directory,catalogs)
  const p=catalogs.preview(bundle({...payload(),id:PET_AUDIO_TARIFF.id,review_before:'2026-10-24T13:00:00Z'}),'parent')
  const config={purpose:PET_AUDIO_PURPOSE,baseUrl:'https://dashscope.aliyuncs.com/compatible-mode/v1',model:p.tariff.model,apiKey:'synthetic-secret',enabled:true,tariffId:p.tariff.id}
  expect(store.execute({kind:'save',expectedRevision:0,actionId:'signed-collision-save',catalogToken:p.token,config},'parent').enabled).toBe(true)
  vi.spyOn(Date,'now').mockReturnValue(Date.parse('2026-10-24T13:00:00Z'))
  expect(store.execute({kind:'read'})).toMatchObject({enabled:false,state:'tariff_unavailable'})
  rmSync(join(directory,'pet-audio-catalog-control.bin'))
  const older=catalogs.preview(bundle(payload(1)),'parent')
  expect(()=>store.execute({kind:'save',expectedRevision:1,actionId:'must-not-reinitialize',catalogToken:older.token,
    config:{...config,tariffId:older.tariff.id}},'parent')).toThrow('CONTROL_REQUIRED')
  expect(store.execute({kind:'read'})).toMatchObject({enabled:false,revision:1,state:'catalog_recovery_required'})
})
it('rejects numeric catalog IDs without regex coercion in the shared signed-payload contract', () => {
  expect(() => verifyPetCatalog(bundle({ ...payload(), id: 12345678 } as unknown as ReturnType<typeof payload>), Date.now(), trusted)).toThrow('CATALOG_ID')
})
it('an actual post-floor settings encryption failure synchronously revokes future runtime config without restarting sent work; stale CAS does not revoke', async () => {
  const catalogs=new PetAudioCatalogStore(directory,trusted),store=new PetAudioSettingsStore(directory,catalogs)
  const config={purpose:PET_AUDIO_PURPOSE,baseUrl:'https://dashscope.aliyuncs.com/compatible-mode/v1',model:PET_AUDIO_TARIFF.model,apiKey:'synthetic-secret',enabled:true,tariffId:PET_AUDIO_TARIFF.id}
  store.execute({kind:'save',expectedRevision:0,actionId:'original-builtin-config',config})
  const runtime={apply:vi.fn(async()=>{}),applied:()=>false,revoke:vi.fn()}
  const handler=createPetAudioSettingsHandler(store,async()=>{},runtime)
  const preview=catalogs.preview(bundle(),'7:primary:parent')
  await expect(handler({sender:{id:7}},'primary','parent',{kind:'save',expectedRevision:99,actionId:'stale-before-fence',catalogToken:preview.token,config:{...config,tariffId:preview.tariff.id}})).rejects.toThrow('版本已变化')
  expect(runtime.revoke).not.toHaveBeenCalled()
  const encrypt=safeStorage.encryptString
  vi.spyOn(safeStorage,'encryptString').mockImplementation(text=>{if(text.includes('"config":')&&text.includes('"catalog":'))throw Error('SYNTHETIC_SETTINGS_ENCRYPT_FAILED');return encrypt(text)})
  await expect(handler({sender:{id:7}},'primary','parent',{kind:'save',expectedRevision:1,actionId:'post-fence-failure',catalogToken:preview.token,config:{...config,tariffId:preview.tariff.id}})).rejects.toThrow('SYNTHETIC_SETTINGS_ENCRYPT_FAILED')
  expect(runtime.revoke).toHaveBeenCalledTimes(1);expect(runtime.apply).not.toHaveBeenCalled()
  expect(store.execute({kind:'read'})).toMatchObject({revision:1,enabled:false,state:'catalog_recovery_required'})
  expect(store.runtimeConfig()?.control?.state).toMatch(/^[a-f0-9]{64}$/)
})
