// @vitest-environment node
import { createCipheriv, createDecipheriv, createHash, randomBytes } from 'node:crypto'
import { existsSync, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from 'node:fs'
import { mkdir, readFile, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
const secure=vi.hoisted(()=>({available:true,decryptable:true}))
const storageKey=Buffer.alloc(32,31)
vi.mock('electron',()=>({app:{getPath:()=>'/unused-public-test'},safeStorage:{
 isEncryptionAvailable:()=>secure.available,
 encryptString:(value:string)=>{const iv=randomBytes(12),cipher=createCipheriv('aes-256-gcm',storageKey,iv),body=Buffer.concat([cipher.update(value,'utf8'),cipher.final()]);return Buffer.concat([iv,cipher.getAuthTag(),body])},
 decryptString:(value:Buffer)=>{if(!secure.decryptable)throw Error('PRIVATE_SAFE_STORAGE_FAILURE');const decipher=createDecipheriv('aes-256-gcm',storageKey,value.subarray(0,12));decipher.setAuthTag(value.subarray(12,28));return Buffer.concat([decipher.update(value.subarray(28)),decipher.final()]).toString('utf8')},
}}))
import { createHouseholdProvisioningStores, HouseholdProvisioningError, importHouseholdProvisioning, sealHouseholdBootstrap, suppressHouseholdProvisioningSlots, validateHouseholdBootstrap, type HouseholdBootstrap, type HouseholdProvisioningStores, type ProvisioningOptions, type ProvisioningSlot, type SmtpBootstrapDefaults } from './household-provisioning'
const key=Buffer.alloc(32,19),id='15d42f90-800b-4dc6-8cd7-4043ec718f49'
const sample=():HouseholdBootstrap=>({version:1,bootstrapId:id,settings:{
 model:{provider:'qwen',apiKey:'sk-synthetic-1ce22cc01640b42bf375',baseUrl:'https://ws-public.cn-beijing.maas.aliyuncs.com/compatible-mode/v1',model:'deepseek-v4-pro',voiceIntentModel:'deepseek-v4-pro',fallback:{provider:'qwen',apiKey:'sk-synthetic-802a8efebdd80dbeea9b',baseUrl:'https://ws-public.cn-beijing.maas.aliyuncs.com/compatible-mode/v1',model:'qwen3.7-plus'}},
 asr:{provider:'dashscope',apiKey:'sk-synthetic-487fa8225e387b785b1a',baseUrl:'https://dashscope.aliyuncs.com/compatible-mode/v1',model:'qwen-audio-3.0-asr-flash'},
 qwenRealtime:{apiKey:'sk-synthetic-ed2960ed7dc38133915a',endpoint:'https://ws-public.cn-beijing.maas.aliyuncs.com',model:'qwen3.8-omni-flash-realtime',voice:'Tina',enabled:true},
 reportDeliverySecrets:{smtpPassword:'syntheticqqtoken',wecomWebhook:'https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=00000000-0000-4000-8000-000000000000'},
 smtpDefaults:{host:'smtp.qq.com',port:465,security:'ssl',username:'public-synthetic@qq.com',fromAddress:'public-synthetic@qq.com',recipient:'public-recipient@example.org'},
}})
const names:ProvisioningSlot[]=['model','asr','qwenRealtime','smtpPassword','wecomWebhook','smtpDefaults']
it('accepts an independently encrypted intent credential only for the authorized Aliyun endpoint',async()=>{
 const value=sample();value.settings.model!.voiceIntentModel='qwen3.8-flash';value.settings.model!.voiceIntentApiKey='synthetic-intent-only-key'
 await packagePayload(value)
 const stores=createHouseholdProvisioningStores(userData,defaultsPort())
 await importHouseholdProvisioning({resourcesPath:resources,userDataPath:userData,key,smtpPort:defaultsPort()})
 expect(await stores.model.inspect()).toEqual({state:'configured',value:value.settings.model})
 const files=readdirSync(userData).map(name=>readFileSync(join(userData,name),'utf8')).join('\n')
 expect(files).not.toContain(value.settings.model!.voiceIntentApiKey)
 const invalid=sample();invalid.settings.model={provider:'deepseek',apiKey:'synthetic-deepseek-only',baseUrl:'https://api.deepseek.com/v1',model:'deepseek-v4-pro',voiceIntentModel:'qwen3.8-flash',voiceIntentApiKey:'synthetic-intent-only-key'}
 expect(()=>sealHouseholdBootstrap(invalid,key)).toThrow('HOUSEHOLD_BOOTSTRAP_INVALID')
})
let root:string,resources:string,userData:string
beforeEach(async()=>{root=mkdtempSync(join(tmpdir(),'hsc-public-provisioning-'));resources=join(root,'resources');userData=join(root,'userData');await mkdir(resources);await mkdir(userData);secure.available=true;secure.decryptable=true})
afterEach(()=>{vi.restoreAllMocks();rmSync(root,{recursive:true,force:true})})
const receipt=()=>JSON.parse(readFileSync(join(userData,'household-provisioning-receipt.json'),'utf8'))
const payloadPath=()=>join(resources,'household-bootstrap.bin')
async function packagePayload(value:unknown=sample()){await writeFile(payloadPath(),sealHouseholdBootstrap(value,key))}
function ports(){const values:Partial<Record<ProvisioningSlot,unknown>>={},bad=new Set<ProvisioningSlot>(),saves:Record<string,any>={},stores={}as HouseholdProvisioningStores
 for(const slot of names){const save=vi.fn(async(value:unknown)=>{values[slot]=structuredClone(value)});saves[slot]=save;(stores as any)[slot]={inspect:async()=>bad.has(slot)?{state:'unreadable'}:values[slot]===undefined?{state:'not_configured'}:{state:'configured',value:structuredClone(values[slot])},save}}
 return {values,bad,saves,stores,options:()=>({resourcesPath:resources,userDataPath:userData,key,smtpPort:stores.smtpDefaults,stores})}
}
function defaultsPort(){const path=join(userData,'smtp-public-defaults.json');return {inspect:async()=>{try{return{state:'configured'as const,value:JSON.parse(await readFile(path,'utf8')) as SmtpBootstrapDefaults}}catch(error){return{state:(error as NodeJS.ErrnoException).code==='ENOENT'?'not_configured'as const:'unreadable'as const}}},save:async(value:SmtpBootstrapDefaults)=>writeFile(path,JSON.stringify(value))}}
function encryptedRaw(value:string):Buffer{const iv=randomBytes(12),cipher=createCipheriv('aes-256-gcm',key,iv);cipher.setAAD(Buffer.from('HSCBOOT1'));const body=Buffer.concat([cipher.update(value,'utf8'),cipher.final()]);return Buffer.concat([Buffer.from('HSCBOOT1'),iv,cipher.getAuthTag(),body])}
const expectNoSaves=(p:ReturnType<typeof ports>)=>{for(const save of Object.values(p.saves))expect(save).not.toHaveBeenCalled()}

it('imports through the actual existing safeStorage stores and persists only ciphertext plus secret-free receipt',async()=>{
 await packagePayload();const smtpPort=defaultsPort(),options:ProvisioningOptions={resourcesPath:resources,userDataPath:userData,key,smtpPort}
 expect((await importHouseholdProvisioning(options)).status).toBe('complete');expect(existsSync(payloadPath())).toBe(false)
 const stores=createHouseholdProvisioningStores(userData,smtpPort),s=sample().settings
 expect(await stores.model.inspect()).toEqual({state:'configured',value:s.model});expect(await stores.asr.inspect()).toEqual({state:'configured',value:s.asr})
 expect(await stores.qwenRealtime.inspect()).toEqual({state:'configured',value:s.qwenRealtime});expect(await stores.smtpPassword.inspect()).toEqual({state:'configured',value:s.reportDeliverySecrets!.smtpPassword})
 expect(await stores.smtpDefaults.inspect()).toEqual({state:'configured',value:s.smtpDefaults})
 const files=readdirSync(userData).map(name=>readFileSync(join(userData,name),'utf8')).join('\n')
 for(const secret of [s.model!.apiKey,s.model!.fallback!.apiKey,s.asr!.apiKey,s.qwenRealtime!.apiKey,s.reportDeliverySecrets!.smtpPassword!,s.reportDeliverySecrets!.wecomWebhook!])expect(files).not.toContain(secret)
 expect(receipt()).toEqual({version:1,bootstrapId:id,payloadSha256:expect.stringMatching(/^[a-f0-9]{64}$/),complete:true,slots:Object.fromEntries(names.map(name=>[name,'imported']))})
 expect(files).not.toContain('parentPin');expect(files).not.toContain('privacyGrant')
})
it('never overwrites configured preferences, including disabled realtime and individual report secret fields',async()=>{
 const p=ports();p.values.model={existing:'preference'};p.values.qwenRealtime={enabled:false};p.values.smtpPassword='different-user-synthetic-secret';p.values.smtpDefaults={recipient:'other@example.org'}
 await packagePayload();const result=await importHouseholdProvisioning(p.options())
 expect(result.status).toBe('complete');for(const slot of ['model','qwenRealtime','smtpPassword','smtpDefaults'])expect(p.saves[slot]).not.toHaveBeenCalled()
 expect(p.saves.wecomWebhook).toHaveBeenCalledOnce();expect(receipt().slots.model).toBe('preserved')
})
it.each(names)('any existing unreadable %s blocks all writes and preserves the payload',async slot=>{
 const p=ports();p.bad.add(slot);await packagePayload()
 await expect(importHouseholdProvisioning(p.options())).rejects.toMatchObject({code:'HOUSEHOLD_BOOTSTRAP_CONFIG_UNREADABLE'})
 expectNoSaves(p);expect(existsSync(payloadPath())).toBe(true);expect(existsSync(join(userData,'household-provisioning-receipt.json'))).toBe(false)
})
it('missing and unreadable are distinguished by actual store files, including OS decryption failure',async()=>{
 const port=defaultsPort(),stores=createHouseholdProvisioningStores(userData,port)
 expect(await stores.asr.inspect()).toEqual({state:'not_configured'})
 await writeFile(join(userData,'asr-settings.json'),'not-valid-public-json')
 expect(await stores.asr.inspect()).toEqual({state:'unreadable'})
 await packagePayload();await expect(importHouseholdProvisioning({resourcesPath:resources,userDataPath:userData,key,smtpPort:port})).rejects.toMatchObject({code:'HOUSEHOLD_BOOTSTRAP_CONFIG_UNREADABLE'})
 expect(existsSync(join(userData,'model-settings.json'))).toBe(false)
})
it('unavailable safeStorage refuses the entire import before receipt or any save',async()=>{
 const p=ports();await packagePayload();secure.available=false
 await expect(importHouseholdProvisioning(p.options())).rejects.toMatchObject({code:'HOUSEHOLD_BOOTSTRAP_STORAGE_UNAVAILABLE'});expectNoSaves(p)
 expect(existsSync(join(userData,'household-provisioning-receipt.json'))).toBe(false)
})
it('an already saved actual secret becoming undecryptable is preserved as unreadable, never replaced',async()=>{
 const smtpPort=defaultsPort(),stores=createHouseholdProvisioningStores(userData,smtpPort)
 await stores.asr.save(sample().settings.asr!);const old=readFileSync(join(userData,'asr-settings.json'));secure.decryptable=false;await packagePayload()
 await expect(importHouseholdProvisioning({resourcesPath:resources,userDataPath:userData,key,smtpPort})).rejects.toMatchObject({code:'HOUSEHOLD_BOOTSTRAP_CONFIG_UNREADABLE'})
 expect(readFileSync(join(userData,'asr-settings.json'))).toEqual(old);expect(existsSync(join(userData,'model-settings.json'))).toBe(false)
})
it('a known partial failure retries only unresolved slots and never refills a completed slot cleared later',async()=>{
 const p=ports();p.saves.asr.mockRejectedValueOnce(Error('DO_NOT_EXPOSE_SYNTHETIC_SECRET'));await packagePayload()
 expect((await importHouseholdProvisioning(p.options())).status).toBe('partial');expect(receipt().slots.model).toBe('imported');expect(receipt().slots.asr).toBe('pending')
 delete p.values.model
 expect((await importHouseholdProvisioning(p.options())).status).toBe('complete');expect(p.saves.model).toHaveBeenCalledOnce();expect(p.values.model).toBeUndefined();expect(p.saves.asr).toHaveBeenCalledTimes(2)
})
it('completed receipt prevents backfill after clear, resource replacement, and key loss',async()=>{
 const p=ports();await packagePayload();await importHouseholdProvisioning(p.options());for(const name of names)delete p.values[name]
 const other=sample();other.bootstrapId='25d42f90-800b-4dc6-8cd7-4043ec718f49';await packagePayload(other)
 expect((await importHouseholdProvisioning({...p.options(),key:Buffer.alloc(1)})).status).toBe('already_processed')
 for(const save of Object.values(p.saves))expect(save).toHaveBeenCalledOnce();expect(existsSync(payloadPath())).toBe(false)
})
it('clear tombstones work before first import and across a partial retry without granting other capabilities',async()=>{
 const p=ports();await suppressHouseholdProvisioningSlots(userData,['model','smtpPassword']);await packagePayload()
 expect((await importHouseholdProvisioning(p.options())).status).toBe('complete');expect(p.saves.model).not.toHaveBeenCalled();expect(p.saves.smtpPassword).not.toHaveBeenCalled()
 expect(receipt().slots.model).toBe('suppressed');expect(Object.keys(receipt()).sort()).toEqual(['bootstrapId','complete','payloadSha256','slots','version'])
})
it('ambiguous crashed inflight with missing target blocks automatic replay; present target is preserved',async()=>{
 const p=ports();await packagePayload();writeFileSync(join(userData,'household-provisioning-receipt.json'),JSON.stringify({version:1,bootstrapId:id,payloadSha256:createHash('sha256').update(readFileSync(payloadPath())).digest('hex'),complete:false,slots:{model:'inflight'}}))
 await expect(importHouseholdProvisioning(p.options())).rejects.toMatchObject({code:'HOUSEHOLD_BOOTSTRAP_RECONCILIATION_REQUIRED'});expectNoSaves(p)
 p.values.model={existing:'committed-before-crash'}
 expect((await importHouseholdProvisioning(p.options())).status).toBe('complete');expect(p.saves.model).not.toHaveBeenCalled();expect(receipt().slots.model).toBe('preserved')
})
it('save throwing after committing is reconciled without reapplying or exporting the exception body',async()=>{
 const p=ports();p.saves.model.mockImplementationOnce(async(value:unknown)=>{p.values.model=value;throw Error('PRIVATE_SYNTHETIC_PROVIDER_BODY')});await packagePayload()
 expect(await importHouseholdProvisioning(p.options())).toMatchObject({status:'partial',slots:{model:'preserved'}})
 await importHouseholdProvisioning(p.options());expect(p.saves.model).toHaveBeenCalledOnce();expect(readFileSync(join(userData,'household-provisioning-receipt.json'),'utf8')).not.toContain('PRIVATE_SYNTHETIC_PROVIDER_BODY')
})
it('concurrent imports serialize so safeStorage slots are saved exactly once',async()=>{
 const p=ports();await packagePayload();const result=await Promise.all([importHouseholdProvisioning(p.options()),importHouseholdProvisioning(p.options())])
 expect(result.map(r=>r.status)).toEqual(['complete','already_processed']);for(const save of Object.values(p.saves))expect(save).toHaveBeenCalledOnce()
})
it.each(['truncated','wrong-key','tampered','oversize','duplicate-json','unknown-json'])('rejects %s without writes or raw body in the error',async mode=>{
 const p=ports();let bytes=sealHouseholdBootstrap(sample(),key),candidateKey=key
 if(mode==='truncated')bytes=bytes.subarray(0,20)
 if(mode==='wrong-key')candidateKey=Buffer.alloc(32,20)
 if(mode==='tampered')bytes[bytes.length-1]^=1
 if(mode==='oversize')bytes=Buffer.alloc(65537)
 if(mode==='duplicate-json')bytes=encryptedRaw('{"version":1,"version":1,"bootstrapId":"'+id+'","settings":{"unknown":"PRIVATE_SYNTHETIC_BODY"}}')
 if(mode==='unknown-json')bytes=encryptedRaw(JSON.stringify({...sample(),secretUnexpected:'PRIVATE_SYNTHETIC_BODY'}))
 await writeFile(payloadPath(),bytes)
 const error=await importHouseholdProvisioning({...p.options(),key:candidateKey}).catch(error=>error)
 expect(error).toBeInstanceOf(HouseholdProvisioningError);expect(String(error)).not.toContain('PRIVATE_SYNTHETIC_BODY');expectNoSaves(p);expect(existsSync(payloadPath())).toBe(true)
})
it.each([
 (p:any)=>{p.parentPin='forbidden'},(p:any)=>{p.settings.privacyGrant=true},(p:any)=>{p.settings.model.baseUrl='https://malicious.invalid/steal'},
 (p:any)=>{p.settings.model.baseUrl='https://ws-public.cn-beijing.maas.aliyuncs.com.evil.invalid/compatible-mode/v1'},
 (p:any)=>{p.settings.qwenRealtime.endpoint+='?token=forbidden'},(p:any)=>{p.settings.smtpDefaults.fromAddress='different@example.test'},
 (p:any)=>{p.settings.smtpDefaults.port=25},(p:any)=>{p.settings.reportDeliverySecrets.smtpPassword='synthetic-invalid\nsecret'},
])('strict payload schema rejects an unsupported field or endpoint #%#',mutate=>{
 const p:any=sample();mutate(p);expect(()=>sealHouseholdBootstrap(p,key)).toThrow('HOUSEHOLD_BOOTSTRAP_INVALID')
})
it('optional ASR and all optional capabilities can be omitted without creating fake configuration',async()=>{
 const p=ports(),body=sample();delete body.settings.asr;delete body.settings.qwenRealtime;delete body.settings.reportDeliverySecrets;delete body.settings.smtpDefaults
 await packagePayload(body);expect((await importHouseholdProvisioning(p.options())).status).toBe('complete')
 expect(p.saves.model).toHaveBeenCalledOnce();for(const name of names.filter(name=>name!=='model'))expect(p.saves[name]).not.toHaveBeenCalled()
})
it.each(['broken-json','unknown-receipt-field','invalid-completion','changed-package'])('fails closed for %s receipt state',async mode=>{
 const p=ports();await packagePayload();const path=join(userData,'household-provisioning-receipt.json')
 const existing:any={version:1,bootstrapId:id,payloadSha256:createHash('sha256').update(readFileSync(payloadPath())).digest('hex'),complete:false,slots:{model:'pending'}}
 if(mode==='unknown-receipt-field')existing.secret='synthetic-12851229aeeabf77597bb1fa'
 if(mode==='invalid-completion')existing.complete=true
 if(mode==='changed-package')existing.bootstrapId='25d42f90-800b-4dc6-8cd7-4043ec718f49'
 writeFileSync(path,mode==='broken-json'?'PRIVATE_CORRUPT_BODY':JSON.stringify(existing))
 await expect(importHouseholdProvisioning(p.options())).rejects.toBeInstanceOf(HouseholdProvisioningError);expectNoSaves(p)
})
it('absent installed resource is a no-op and does not create configuration or a receipt',async()=>{
 const p=ports();expect(await importHouseholdProvisioning(p.options())).toEqual({status:'not_present',slots:{}});expectNoSaves(p);expect(readdirSync(userData)).toEqual([])
})

it('partial import is bound to the exact installed ciphertext, even if a replacement reuses the bootstrap id',async()=>{
 const p=ports();p.saves.asr.mockRejectedValueOnce(Error('PUBLIC_TRANSIENT_FAILURE'));await packagePayload()
 expect((await importHouseholdProvisioning(p.options())).status).toBe('partial')
 await packagePayload();await expect(importHouseholdProvisioning(p.options())).rejects.toMatchObject({code:'HOUSEHOLD_BOOTSTRAP_PACKAGE_CHANGED'})
 expect(p.saves.model).toHaveBeenCalledOnce();expect(p.saves.asr).toHaveBeenCalledOnce()
})

it.each(['[]','{"unsupportedSecret":"do-not-read-as-empty"}','{"encryptedSmtpPassword":""}','{"encryptedSmtpPassword":"not-base64!"}'])('corrupt existing report secret shape is unreadable and never treated as missing: #%#',async raw=>{
 const smtpPort=defaultsPort();await writeFile(join(userData,'report-delivery-secrets.json'),raw);await packagePayload()
 await expect(importHouseholdProvisioning({resourcesPath:resources,userDataPath:userData,key,smtpPort})).rejects.toMatchObject({code:'HOUSEHOLD_BOOTSTRAP_CONFIG_UNREADABLE'})
 expect(readFileSync(join(userData,'report-delivery-secrets.json'),'utf8')).toBe(raw)
 expect(existsSync(join(userData,'model-settings.json'))).toBe(false)
})

it('provider describes the API vendor: qwen workspace can route primary DeepSeek and fallback Qwen',()=>{
 const parsed=validateHouseholdBootstrap(sample())
 expect(parsed.settings.model).toMatchObject({provider:'qwen',model:'deepseek-v4-pro',fallback:{provider:'qwen',model:'qwen3.7-plus'}})
 const official=sample();official.settings.model={provider:'deepseek',apiKey:'sk-synthetic-206ae86c10dbebd657a9',baseUrl:'https://api.deepseek.com/v1',model:'deepseek-v4-pro',voiceIntentModel:'deepseek-v4-pro'}
 expect(validateHouseholdBootstrap(official).settings.model!.provider).toBe('deepseek')
})

it.each([['imported'],4,{state:'imported'}])('receipt slot states cannot coerce arrays/numbers/objects to a terminal enum #%#',async state=>{
 const p=ports();await packagePayload();writeFileSync(join(userData,'household-provisioning-receipt.json'),JSON.stringify({version:1,bootstrapId:id,payloadSha256:createHash('sha256').update(readFileSync(payloadPath())).digest('hex'),complete:true,slots:{model:state}}))
 await expect(importHouseholdProvisioning(p.options())).rejects.toMatchObject({code:'HOUSEHOLD_BOOTSTRAP_RECEIPT_UNREADABLE'});expectNoSaves(p);expect(existsSync(payloadPath())).toBe(true)
})
it('undefined-only package settings and secret objects are rejected before sealing',()=>{
 expect(()=>sealHouseholdBootstrap({version:1,bootstrapId:id,settings:{model:undefined}},key)).toThrow('HOUSEHOLD_BOOTSTRAP_INVALID')
 expect(()=>sealHouseholdBootstrap({version:1,bootstrapId:id,settings:{reportDeliverySecrets:{smtpPassword:undefined}}},key)).toThrow('HOUSEHOLD_BOOTSTRAP_INVALID')
})

it.each(['cn-beijing','ap-southeast-1'])('ASR accepts the same strict workspace compatible endpoint in %s',async region=>{
 const p=ports(),body=sample();body.settings.asr!.baseUrl=`https://ws-public.${region}.maas.aliyuncs.com/compatible-mode/v1`;body.settings.asr!.model='qwen3-asr-flash'
 await packagePayload(body);expect((await importHouseholdProvisioning(p.options())).status).toBe('complete')
 expect(p.values.asr).toEqual(body.settings.asr)
})
it.each([
 'https://ws-public.cn-beijing.maas.aliyuncs.com/other-path',
 'https://user:password@ws-public.cn-beijing.maas.aliyuncs.com/compatible-mode/v1',
 'https://ws-public.cn-beijing.maas.aliyuncs.com.foreign.invalid/compatible-mode/v1',
 'https://foreign.invalid/compatible-mode/v1',
 'https://ws-public.cn-beijing.maas.aliyuncs.com/compatible-mode/v1?api_key=forbidden',
 'https://ws-public.cn-beijing.maas.aliyuncs.com/compatible-mode/v1#fragment',
])('ASR still refuses unrelated, credential-bearing or non-exact workspace URL #%#',baseUrl=>{
 const body=sample();body.settings.asr!.baseUrl=baseUrl
 expect(()=>sealHouseholdBootstrap(body,key)).toThrow('HOUSEHOLD_BOOTSTRAP_INVALID')
})
