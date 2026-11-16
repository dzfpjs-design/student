import { link, mkdtemp, mkdir, readFile, readdir, realpath, rm, unlink, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { createHash, randomBytes, randomUUID } from 'node:crypto'
import { spawn } from 'node:child_process'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
const fixture = vi.hoisted(() => ({ available: true, corruptNewKey: false }))
vi.mock('electron', () => ({ app: { getPath: () => '' }, safeStorage: {
  isEncryptionAvailable: () => fixture.available,
  encryptString: (value: string) => Buffer.from('synthetic-sealed:' + value + (fixture.corruptNewKey && value === 'synthetic-new-key-never-real' ? '-corrupt' : ''), 'utf8'),
  decryptString: (value: Buffer) => {
    const text = value.toString('utf8')
    if (!text.startsWith('synthetic-sealed:')) throw Error('PRIVATE_STORAGE_FAILURE')
    return text.slice('synthetic-sealed:'.length)
  },
} }))
import { sealQwenRotation, rotateInstalledQwenKey, recoverQwenKeyRotation } from './qwen-key-rotation'
import { ModelSettingsStore } from './model-settings'
import { CloudAsrSettingsStore } from './asr-settings'
import { QwenRealtimeSettingsStore } from './qwen-realtime'
import { VoiceSettingsStore } from './voice-settings'
import { PetAudioSettingsStore } from './pet-audio-settings'
import { cloudProviderSettingsFingerprint } from './runtime-provider-refresh'
import { readQwenOverlay } from './qwen-key-overlay'

const oldKey = 'synthetic-old-key-never-real', newKey = 'synthetic-new-key-never-real', otherKey = 'synthetic-other-key-never-real'
const seal = (value: string): string => Buffer.from('synthetic-sealed:' + value).toString('base64')
const unseal = (value: string): string => Buffer.from(value, 'base64').toString().slice('synthetic-sealed:'.length)
const generation = () => randomUUID()
let root: string, resources: string, userData: string
beforeEach(async () => { fixture.available=true;fixture.corruptNewKey=false;root=await mkdtemp(join(await realpath(tmpdir()),'hsc-rotation-test-')); resources=join(root,'resources'); userData=join(root,'profile'); await mkdir(resources); await mkdir(userData) })
afterEach(async()=>{ await rm(root,{recursive:true,force:true}) })
const put = (name: string, value: unknown) => writeFile(join(userData,name),JSON.stringify(value))
const read = async (name: string): Promise<any> => JSON.parse(await readFile(join(userData,name),'utf8'))
async function makePayload(id = generation(), value = newKey) {
  const wrappingKey=randomBytes(32), ciphertext=sealQwenRotation(id,value,wrappingKey)
  await writeFile(join(resources,'qwen-rotation.bin'),ciphertext)
  await writeFile(join(resources,'qwen-rotation.key'),wrappingKey)
  return { id, wrappingKey, ciphertext }
}
const qwenUrl='https://synthetic.cn-beijing.maas.aliyuncs.com/compatible-mode/v1'
it.each(['primary','fallback'])('preserves a DeepSeek %s credential hosted in an Aliyun workspace',async(slot)=>{
  const qwen={provider:'qwen',baseUrl:qwenUrl,model:'qwen3.7-plus',encryptedApiKey:seal(oldKey)}
  const deepseek={provider:'deepseek',baseUrl:qwenUrl,model:'deepseek-v4-pro',encryptedApiKey:seal(otherKey)}
  await put('model-settings.json',slot==='primary'?{...deepseek,voiceIntentModel:'deepseek-v4-pro',fallback:qwen}:{...qwen,voiceIntentModel:'qwen3.7-plus',fallback:deepseek})
  await makePayload();await rotateInstalledQwenKey({userDataPath:userData,resourcesPath:resources})
  const current=await new ModelSettingsStore(userData).load()
  expect(slot==='primary'?current?.apiKey:current?.fallback?.apiKey).toBe(otherKey)
  expect(slot==='primary'?current?.fallback?.apiKey:current?.apiKey).toBe(newKey)
})
it.each(['primary','fallback','intent','asr'])('blocks an unclassified Aliyun model alias in %s before committing any rotation',async(slot)=>{
  const model={provider:'qwen',baseUrl:qwenUrl,model:'qwen3.7-plus',voiceIntentModel:'qwen3.8-flash',encryptedApiKey:seal(oldKey),fallback:{provider:'qwen',baseUrl:qwenUrl,model:'qwen3.5-plus',encryptedApiKey:seal(oldKey)}}
  if(slot==='primary') model.model='qwen-internal-alias'
  if(slot==='fallback') model.fallback.model='private-deployment-alias'
  if(slot==='intent') model.voiceIntentModel='private-deployment-alias'
  await put('model-settings.json',model)
  if(slot==='asr') await put('asr-settings.json',{provider:'dashscope',baseUrl:qwenUrl,model:'private-deployment-alias',encryptedApiKey:seal(oldKey)})
  await makePayload()
  await expect(rotateInstalledQwenKey({userDataPath:userData,resourcesPath:resources})).rejects.toThrow('ROTATION_MODEL_UNCLASSIFIED')
  expect(await read('model-settings.json')).toEqual(model)
  expect(await readdir(userData)).not.toContain('qwen-key-rotation-required.bin')
})
it('rejects a restored old pointer when a newer generation is still present',async()=>{
  await put('asr-settings.json',{provider:'dashscope',baseUrl:qwenUrl,model:'qwen3-asr-flash',encryptedApiKey:seal(oldKey)})
  const first=randomBytes(32);await writeFile(join(resources,'qwen-rotation.bin'),sealQwenRotation(generation(),newKey,first,100));await writeFile(join(resources,'qwen-rotation.key'),first)
  await rotateInstalledQwenKey({userDataPath:userData,resourcesPath:resources})
  const pointer=join(userData,'qwen-key-rotation','high-water.bin'),prior=await readFile(pointer)
  const second=randomBytes(32);await writeFile(join(resources,'qwen-rotation.bin'),sealQwenRotation(generation(),'synthetic-later-key-never-real',second,200));await writeFile(join(resources,'qwen-rotation.key'),second)
  await rotateInstalledQwenKey({userDataPath:userData,resourcesPath:resources})
  await writeFile(pointer,prior)
  await expect(new CloudAsrSettingsStore(userData).load()).rejects.toThrow('QWEN_ROTATION_SOURCE_CHANGED')
})
it.each([
  ['model-settings.json',{provider:'deepseek',baseUrl:'https://api.deepseek.com',model:'deepseek-v4-pro',voiceIntentModel:'deepseek-v4-pro',encryptedApiKey:seal(otherKey),fallback:{provider:'qwen',baseUrl:qwenUrl,model:'qwen3.7-plus',encryptedApiKey:seal(oldKey)}}],
  ['asr-settings.json',{provider:'dashscope',baseUrl:qwenUrl,model:'qwen3-asr-flash',encryptedApiKey:seal(oldKey)}],
  ['qwen-realtime-settings.json',{endpoint:'https://synthetic.cn-beijing.maas.aliyuncs.com',model:'qwen3.8-omni-flash-realtime',voice:'Tina',enabled:false,encryptedApiKey:seal(oldKey)}],
  ['voice-settings.json',{provider:'qwen_voice_clone',relayUrl:'',voiceId:'synthetic-voice',model:'qwen3-tts-vc-2026-01-22',region:'cn-beijing',encryptedApiKey:seal(oldKey)}],
])('accepts a valid synthetic %s slot',async(name,value)=>{
  await put(name,value);await makePayload()
  expect(await rotateInstalledQwenKey({userDataPath:userData,resourcesPath:resources})).toEqual({status:'completed',changedSlots:name==='qwen-realtime-settings.json'?2:1})
})

it('rotates all existing Qwen slots while preserving non-Qwen key, settings, and disabled states', async () => {
  await put('model-settings.json',{provider:'deepseek',baseUrl:'https://api.deepseek.com',model:'deepseek-v4-pro',voiceIntentModel:'deepseek-v4-pro',encryptedApiKey:seal(otherKey),
    fallback:{provider:'qwen',baseUrl:qwenUrl,model:'qwen3.7-plus',encryptedApiKey:seal(oldKey)}})
  await put('asr-settings.json',{provider:'dashscope',baseUrl:qwenUrl,model:'qwen3-asr-flash',encryptedApiKey:seal(oldKey)})
  await put('qwen-realtime-settings.json',{endpoint:'https://synthetic.cn-beijing.maas.aliyuncs.com',model:'qwen3.8-omni-flash-realtime',voice:'Tina',enabled:false,encryptedApiKey:seal(oldKey)})
  await put('voice-settings.json',{provider:'qwen_voice_clone',relayUrl:'',voiceId:'synthetic-voice',model:'qwen3-tts-vc-2026-01-22',region:'cn-beijing',encryptedApiKey:seal(oldKey)})
  await writeFile(join(userData,'voice-disabled.json'),'synthetic-disabled-marker')
  const pet={version:1,revision:3,actionId:'synthetic-action',config:{purpose:'pet_original_audio_formative_v1',baseUrl:qwenUrl,model:'qwen3-omni-flash-2025-12-01',apiKey:oldKey,enabled:false,tariffId:''}}
  await writeFile(join(userData,'pet-audio-settings.bin'),Buffer.from('synthetic-sealed:'+JSON.stringify(pet)))
  const payload=await makePayload()
  expect(await rotateInstalledQwenKey({userDataPath:userData,resourcesPath:resources})).toEqual({status:'completed',changedSlots:5})
  const model=await read('model-settings.json'), effectiveModel=await new ModelSettingsStore(userData).load()
  expect(unseal(model.encryptedApiKey)).toBe(otherKey);expect(unseal(model.fallback.encryptedApiKey)).toBe(oldKey)
  expect(effectiveModel?.apiKey).toBe(otherKey);expect(effectiveModel?.fallback?.apiKey).toBe(newKey)
  expect(unseal((await read('asr-settings.json')).encryptedApiKey)).toBe(oldKey)
  expect((await new CloudAsrSettingsStore(userData).load())?.apiKey).toBe(newKey)
  const realtime=await read('qwen-realtime-settings.json');expect(unseal(realtime.encryptedApiKey)).toBe(oldKey);expect(realtime.enabled).toBe(false)
  expect((await new QwenRealtimeSettingsStore(userData).load())?.apiKey).toBe(newKey)
  const voice=await read('voice-settings.json');expect(unseal(voice.encryptedApiKey)).toBe(oldKey);expect(voice.voiceId).toBe('synthetic-voice')
  expect((await new VoiceSettingsStore(userData).load())?.apiKey).toBe(newKey)
  expect(await readFile(join(userData,'voice-disabled.json'),'utf8')).toBe('synthetic-disabled-marker')
  const afterPet=JSON.parse((await readFile(join(userData,'pet-audio-settings.bin'),'utf8')).slice('synthetic-sealed:'.length))
  expect(afterPet.config.apiKey).toBe(oldKey);expect(afterPet.config.enabled).toBe(false);expect(afterPet.revision).toBe(3)
  expect(new PetAudioSettingsStore(userData).runtimeConfig()?.apiKey).toBe(newKey)
  // An older already-receipted installer can never overwrite a later edit.
  model.fallback.encryptedApiKey=seal('synthetic-later-key-never-real');await put('model-settings.json',model)
  await writeFile(join(resources,'qwen-rotation.bin'),payload.ciphertext);await writeFile(join(resources,'qwen-rotation.key'),payload.wrappingKey)
  expect(await rotateInstalledQwenKey({userDataPath:userData,resourcesPath:resources})).toEqual({status:'already_completed',changedSlots:0})
  expect(unseal((await read('model-settings.json')).fallback.encryptedApiKey)).toBe('synthetic-later-key-never-real')
  await expect(new ModelSettingsStore(userData).load()).rejects.toThrow('QWEN_ROTATION_SOURCE_CHANGED')
})

it('blocks a corrupt existing slot before changing any earlier slot', async () => {
  const realtime={endpoint:'https://synthetic.cn-beijing.maas.aliyuncs.com',model:'qwen3.8-omni-flash-realtime',voice:'Tina',enabled:true,encryptedApiKey:seal(oldKey)}
  await put('qwen-realtime-settings.json',realtime);await writeFile(join(userData,'voice-settings.json'),'PRIVATE_BAD_CONFIG')
  await makePayload()
  await expect(rotateInstalledQwenKey({userDataPath:userData,resourcesPath:resources})).rejects.toMatchObject({code:'ROTATION_CONFIG_UNREADABLE'})
  expect(await read('qwen-realtime-settings.json')).toEqual(realtime)
})

it('refuses to overwrite a changed file while recovering a partially applied generation', async () => {
  const id=generation(), key=randomBytes(32), payload=sealQwenRotation(id,newKey,key), hash=(b:Buffer)=>createHash('sha256').update(b).digest('hex')
  const before=Buffer.from('synthetic-before'), after=Buffer.from('synthetic-after'), intervening=Buffer.from('synthetic-concurrent-user-edit')
  await writeFile(join(userData,'model-settings.json'),intervening)
  await writeFile(join(resources,'qwen-rotation.bin'),payload);await writeFile(join(resources,'qwen-rotation.key'),key)
  const overlay={version:1,generation:id,order:100,payloadHash:hash(payload),entries:[{name:'model-settings.json',sourceHash:hash(before),after:after.toString('base64')}]}
  const journal={version:1,generation:id,order:100,payloadHash:hash(payload),keyHash:hash(key),overlay:Buffer.from('synthetic-sealed:'+JSON.stringify(overlay)).toString('base64'),entries:[{name:'model-settings.json',before:before.toString('base64'),after:after.toString('base64')}]}
  await writeFile(join(userData,'qwen-key-rotation-journal.bin'),Buffer.from('synthetic-sealed:'+JSON.stringify(journal)))
  await expect(recoverQwenKeyRotation({userDataPath:userData,resourcesPath:resources})).rejects.toMatchObject({code:'ROTATION_CONFLICT'})
  expect(await readFile(join(userData,'model-settings.json'))).toEqual(intervening)
  expect(await readFile(join(userData,'qwen-key-rotation-journal.bin'))).toBeTruthy()
})

it('resumes an interrupted multi-slot commit without reapplying its completed slot', async () => {
  const id=generation(),key=randomBytes(32),payload=sealQwenRotation(id,newKey,key), hash=(b:Buffer)=>createHash('sha256').update(b).digest('hex')
  const firstBefore=Buffer.from('first-before'),firstAfter=Buffer.from('first-after'),secondBefore=Buffer.from('second-before'),secondAfter=Buffer.from('second-after')
  await writeFile(join(userData,'model-settings.json'),firstBefore);await writeFile(join(userData,'asr-settings.json'),secondBefore)
  await writeFile(join(resources,'qwen-rotation.bin'),payload);await writeFile(join(resources,'qwen-rotation.key'),key)
  const overlay={version:1,generation:id,order:100,payloadHash:hash(payload),entries:[{name:'model-settings.json',sourceHash:hash(firstBefore),after:firstAfter.toString('base64')},{name:'asr-settings.json',sourceHash:hash(secondBefore),after:secondAfter.toString('base64')}]}
  const journal={version:1,generation:id,order:100,payloadHash:hash(payload),keyHash:hash(key),overlay:Buffer.from('synthetic-sealed:'+JSON.stringify(overlay)).toString('base64'),entries:[
    {name:'model-settings.json',before:firstBefore.toString('base64'),after:firstAfter.toString('base64')},
    {name:'asr-settings.json',before:secondBefore.toString('base64'),after:secondAfter.toString('base64')}]}
  await writeFile(join(userData,'qwen-key-rotation-journal.bin'),Buffer.from('synthetic-sealed:'+JSON.stringify(journal)))
  expect(await recoverQwenKeyRotation({userDataPath:userData,resourcesPath:resources})).toBe(true)
  expect(await readFile(join(userData,'model-settings.json'))).toEqual(firstBefore)
  expect(await readFile(join(userData,'asr-settings.json'))).toEqual(secondBefore)
  await expect(readFile(join(userData,'qwen-key-rotation-journal.bin'))).rejects.toMatchObject({code:'ENOENT'})
  await expect(readFile(join(resources,'qwen-rotation.key'))).rejects.toMatchObject({code:'ENOENT'})
  expect(await readFile(join(userData,'qwen-key-rotation',`${id}.bin`))).toBeTruthy()
})

it('does not mark an empty or missing provider setup as a completed rotation',async()=>{
  await makePayload()
  await expect(rotateInstalledQwenKey({userDataPath:userData,resourcesPath:resources})).rejects.toMatchObject({code:'ROTATION_CONFIG_UNREADABLE'})
  await expect(readFile(join(resources,'qwen-rotation.bin'))).resolves.toBeTruthy()
})

it('does not replace any slot when secure encryption cannot round-trip the new key',async()=>{
  const prior={provider:'dashscope',baseUrl:qwenUrl,model:'qwen3-asr-flash',encryptedApiKey:seal(oldKey)}
  await put('asr-settings.json',prior);await makePayload();fixture.corruptNewKey=true
  await expect(rotateInstalledQwenKey({userDataPath:userData,resourcesPath:resources})).rejects.toMatchObject({code:'ROTATION_STORAGE_UNAVAILABLE'})
  expect(await read('asr-settings.json')).toEqual(prior)
  await expect(readFile(join(userData,'qwen-key-rotation-journal.bin'))).rejects.toMatchObject({code:'ENOENT'})
})

it.each([
  ['deepseek-v4-pro','qwen3.8-flash',otherKey,newKey],
  ['qwen3.7-plus','deepseek-v4-flash',newKey,oldKey],
])('splits shared Qwen and non-Qwen credentials without changing model routes',async(model,intent,primaryExpected,intentExpected)=>{
  await put('model-settings.json',{provider:'qwen',baseUrl:qwenUrl,model,voiceIntentModel:intent,encryptedApiKey:seal(model.startsWith('qwen')?oldKey:otherKey)})
  await makePayload();await rotateInstalledQwenKey({userDataPath:userData,resourcesPath:resources})
  const stored=await new ModelSettingsStore(userData).load()
  expect(stored?.model).toBe(model);expect(stored?.voiceIntentModel).toBe(intent);expect(stored?.baseUrl).toBe(qwenUrl)
  expect(stored?.apiKey).toBe(primaryExpected)
  expect(stored?.voiceIntentApiKey).toBe(intentExpected)
})

it('does not send a Qwen intent credential to a non-Qwen endpoint',async()=>{
  const prior={provider:'deepseek',baseUrl:'https://api.deepseek.com',model:'deepseek-v4-pro',voiceIntentModel:'qwen3.8-flash',encryptedApiKey:seal(otherKey)}
  await put('model-settings.json',prior);await makePayload()
  await expect(rotateInstalledQwenKey({userDataPath:userData,resourcesPath:resources})).rejects.toMatchObject({code:'ROTATION_CONFIG_UNREADABLE'})
  expect(await read('model-settings.json')).toEqual(prior)
})

it('rejects a different payload reusing a completed generation without overwriting settings',async()=>{
  await put('asr-settings.json',{provider:'dashscope',baseUrl:qwenUrl,model:'qwen3-asr-flash',encryptedApiKey:seal(oldKey)})
  const id=generation();await makePayload(id);await rotateInstalledQwenKey({userDataPath:userData,resourcesPath:resources})
  const prior=await read('asr-settings.json');await makePayload(id,'synthetic-different-key-never-real')
  await expect(rotateInstalledQwenKey({userDataPath:userData,resourcesPath:resources})).rejects.toMatchObject({code:'ROTATION_CONFLICT'})
  expect(await read('asr-settings.json')).toEqual(prior)
})

it('recovers cleanup when the encrypted payload was already removed before a crash',async()=>{
  const id=generation(),key=randomBytes(32),payload=sealQwenRotation(id,newKey,key),hash=(b:Buffer)=>createHash('sha256').update(b).digest('hex')
  const before=Buffer.from('synthetic-before'),after=Buffer.from('synthetic-after')
  await writeFile(join(userData,'asr-settings.json'),before);await writeFile(join(resources,'qwen-rotation.key'),key)
  const overlay={version:1,generation:id,order:100,payloadHash:hash(payload),entries:[{name:'asr-settings.json',sourceHash:hash(before),after:after.toString('base64')}]}
  const journal={version:1,generation:id,order:100,payloadHash:hash(payload),keyHash:hash(key),overlay:Buffer.from('synthetic-sealed:'+JSON.stringify(overlay)).toString('base64'),entries:[{name:'asr-settings.json',before:before.toString('base64'),after:after.toString('base64')}]}
  await writeFile(join(userData,'qwen-key-rotation-journal.bin'),Buffer.from('synthetic-sealed:'+JSON.stringify(journal)))
  expect(await recoverQwenKeyRotation({userDataPath:userData,resourcesPath:resources})).toBe(true)
  expect(await readFile(join(userData,'asr-settings.json'))).toEqual(before)
  await expect(readFile(join(resources,'qwen-rotation.key'))).rejects.toMatchObject({code:'ENOENT'})
})

it('refuses a previously unseen older generation after a newer one',async()=>{
  await put('asr-settings.json',{provider:'dashscope',baseUrl:qwenUrl,model:'qwen3-asr-flash',encryptedApiKey:seal(oldKey)})
  const firstKey=randomBytes(32),first=sealQwenRotation(generation(),newKey,firstKey,200)
  await writeFile(join(resources,'qwen-rotation.bin'),first);await writeFile(join(resources,'qwen-rotation.key'),firstKey)
  await rotateInstalledQwenKey({userDataPath:userData,resourcesPath:resources})
  const prior=await read('asr-settings.json'),olderKey=randomBytes(32),older=sealQwenRotation(generation(),'synthetic-older-key-only',olderKey,100)
  await writeFile(join(resources,'qwen-rotation.bin'),older);await writeFile(join(resources,'qwen-rotation.key'),olderKey)
  await expect(rotateInstalledQwenKey({userDataPath:userData,resourcesPath:resources})).rejects.toMatchObject({code:'ROTATION_CONFLICT'})
  expect(await read('asr-settings.json')).toEqual(prior)
})

it('creates the installed rotation payload from a private input stream without printing the synthetic key',async()=>{
  await put('asr-settings.json',{provider:'dashscope',baseUrl:qwenUrl,model:'qwen3-asr-flash',encryptedApiKey:seal(oldKey)})
  const child=spawn(process.execPath,[join(process.cwd(),'scripts','seal-qwen-rotation.cjs'),resources],{stdio:['pipe','pipe','pipe']})
  let output='';child.stdout.on('data',chunk=>{output+=chunk});child.stderr.on('data',chunk=>{output+=chunk})
  const ended=new Promise<number|null>(resolve=>child.on('exit',resolve));child.stdin.end(Buffer.from(newKey))
  expect(await ended).toBe(0);expect(output).toBe('ROTATION_PAYLOAD_PREPARED\n');expect(output).not.toContain(newKey)
  const payload=await readFile(join(resources,'qwen-rotation.bin'))
  expect(payload.includes(Buffer.from(newKey))).toBe(false)
  expect(await rotateInstalledQwenKey({userDataPath:userData,resourcesPath:resources})).toEqual({status:'completed',changedSlots:1})
  expect((await new CloudAsrSettingsStore(userData).load())?.apiKey).toBe(newKey)
})

it.each(['model','asr','realtime','voice','pet'] as const)('blocks a changed %s source without falling back to its old key',async slot=>{
  const filename={model:'model-settings.json',asr:'asr-settings.json',realtime:'qwen-realtime-settings.json',voice:'voice-settings.json',pet:'pet-audio-settings.bin'}[slot]
  if(slot==='model') await put(filename,{provider:'qwen',baseUrl:qwenUrl,model:'qwen3.7-plus',voiceIntentModel:'qwen3.8-flash',encryptedApiKey:seal(oldKey)})
  if(slot==='asr') await put(filename,{provider:'dashscope',baseUrl:qwenUrl,model:'qwen3-asr-flash',encryptedApiKey:seal(oldKey)})
  if(slot==='realtime') await put(filename,{endpoint:'https://synthetic.cn-beijing.maas.aliyuncs.com',model:'qwen3.8-omni-flash-realtime',voice:'Tina',enabled:true,encryptedApiKey:seal(oldKey)})
  if(slot==='voice') await put(filename,{provider:'qwen_voice_clone',relayUrl:'',voiceId:'synthetic-voice',model:'qwen3-tts-vc-2026-01-22',region:'cn-beijing',encryptedApiKey:seal(oldKey)})
  if(slot==='pet') await writeFile(join(userData,filename),Buffer.from('synthetic-sealed:'+JSON.stringify({version:1,revision:1,actionId:'synthetic-action',config:{purpose:'pet_original_audio_formative_v1',baseUrl:qwenUrl,model:'qwen3-omni-flash-2025-12-01',apiKey:oldKey,enabled:false,tariffId:''}})))
  await makePayload();await rotateInstalledQwenKey({userDataPath:userData,resourcesPath:resources})
  await writeFile(join(userData,filename),'synthetic-external-change')
  if(slot==='model') await expect(new ModelSettingsStore(userData).loadOrImportDeepSeekFromMacOSKeychain('synthetic-service')).rejects.toThrow('QWEN_ROTATION_SOURCE_CHANGED')
  if(slot==='asr') await expect(new CloudAsrSettingsStore(userData).loadOrImportDashScopeFromMacOSKeychain('synthetic-service')).rejects.toThrow('QWEN_ROTATION_SOURCE_CHANGED')
  if(slot==='realtime') await expect(new QwenRealtimeSettingsStore(userData).loadOrImport({environmentKey:'synthetic-environment-key',endpoint:'https://synthetic.cn-beijing.maas.aliyuncs.com'})).rejects.toThrow('QWEN_ROTATION_SOURCE_CHANGED')
  if(slot==='voice') await expect(new VoiceSettingsStore(userData).loadEffective({apiKey:'synthetic-environment-key',endpoint:'https://synthetic.cn-beijing.maas.aliyuncs.com',model:'qwen3.8-omni-flash-realtime',voice:'Tina',enabled:true})).rejects.toThrow('QWEN_ROTATION_SOURCE_CHANGED')
  if(slot==='pet') expect(()=>new PetAudioSettingsStore(userData).runtimeConfig()).toThrow('QWEN_ROTATION_SOURCE_CHANGED')
  expect(await readFile(join(userData,filename),'utf8')).toBe('synthetic-external-change')
})

it('does not fall back when the protected overlay directory disappears',async()=>{
  await put('asr-settings.json',{provider:'dashscope',baseUrl:qwenUrl,model:'qwen3-asr-flash',encryptedApiKey:seal(oldKey)})
  await makePayload();await rotateInstalledQwenKey({userDataPath:userData,resourcesPath:resources})
  await rm(join(userData,'qwen-key-rotation'),{recursive:true,force:true})
  await expect(new CloudAsrSettingsStore(userData).load()).rejects.toThrow('QWEN_ROTATION_SOURCE_CHANGED')
})

it.each(['partial_stage','linked_stage'] as const)('recovers an interrupted immutable overlay publish: %s',async mode=>{
  const id=generation(),key=randomBytes(32),payload=sealQwenRotation(id,newKey,key),hash=(b:Buffer)=>createHash('sha256').update(b).digest('hex')
  const before=Buffer.from(JSON.stringify({provider:'dashscope',baseUrl:qwenUrl,model:'qwen3-asr-flash',encryptedApiKey:seal(oldKey)}))
  const after=Buffer.from(JSON.stringify({provider:'dashscope',baseUrl:qwenUrl,model:'qwen3-asr-flash',encryptedApiKey:seal(newKey)}))
  const overlay={version:1,generation:id,order:100,payloadHash:hash(payload),entries:[{name:'asr-settings.json',sourceHash:hash(before),after:after.toString('base64')}]}
  const sealed=Buffer.from('synthetic-sealed:'+JSON.stringify(overlay)),dir=join(userData,'qwen-key-rotation');await mkdir(dir)
  const stage=join(dir,`${id}.stage`);await writeFile(stage,mode==='partial_stage'?sealed.subarray(0,Math.floor(sealed.length/2)):sealed)
  if(mode==='linked_stage')await link(stage,join(dir,`${id}.bin`))
  const journal={version:1,generation:id,order:100,payloadHash:hash(payload),keyHash:hash(key),overlay:sealed.toString('base64'),entries:[{name:'asr-settings.json',before:before.toString('base64'),after:after.toString('base64')}]}
  await writeFile(join(userData,'asr-settings.json'),before);await writeFile(join(userData,'qwen-key-rotation-journal.bin'),Buffer.from('synthetic-sealed:'+JSON.stringify(journal)))
  await writeFile(join(resources,'qwen-rotation.bin'),payload);await writeFile(join(resources,'qwen-rotation.key'),key)
  expect(await recoverQwenKeyRotation({userDataPath:userData,resourcesPath:resources})).toBe(true)
  expect((await new CloudAsrSettingsStore(userData).load())?.apiKey).toBe(newKey)
  expect(await readFile(join(userData,'asr-settings.json'))).toEqual(before)
})

it('keeps non-Qwen ASR key while separately rotating the Qwen-VL credential',async()=>{
  await put('asr-settings.json',{provider:'dashscope',baseUrl:qwenUrl,model:'paraformer-realtime-v2',encryptedApiKey:seal(otherKey)})
  await makePayload();await rotateInstalledQwenKey({userDataPath:userData,resourcesPath:resources})
  const config=await new CloudAsrSettingsStore(userData).load()
  expect(config?.apiKey).toBe(otherKey);expect(config?.documentVisionApiKey).toBe(newKey);expect(config?.model).toBe('paraformer-realtime-v2')
  expect(unseal((await read('asr-settings.json')).encryptedApiKey)).toBe(otherKey)
})

it('preserves the implicit non-Qwen CosyVoice key and its dependency on realtime enablement',async()=>{
  const prior={endpoint:'https://synthetic.cn-beijing.maas.aliyuncs.com',model:'qwen3.8-omni-flash-realtime',voice:'Tina',enabled:true,encryptedApiKey:seal(oldKey)}
  await put('qwen-realtime-settings.json',prior);await makePayload();await rotateInstalledQwenKey({userDataPath:userData,resourcesPath:resources})
  const realtime=(await new QwenRealtimeSettingsStore(userData).load())!,voice=new VoiceSettingsStore(userData)
  expect(realtime.apiKey).toBe(newKey)
  expect((await voice.loadEffective(realtime))?.apiKey).toBe(oldKey)
  expect(await voice.loadEffective({...realtime,enabled:false})).toBeNull()
  await expect(readFile(join(userData,'voice-settings.json'))).rejects.toMatchObject({code:'ENOENT'})
})

it('keeps a parent-selected explicit voice explicit across future key generations',async()=>{
  await put('qwen-realtime-settings.json',{endpoint:'https://synthetic.cn-beijing.maas.aliyuncs.com',model:'qwen3.8-omni-flash-realtime',voice:'Tina',enabled:true,encryptedApiKey:seal(oldKey)})
  const key1=randomBytes(32);await writeFile(join(resources,'qwen-rotation.bin'),sealQwenRotation(generation(),newKey,key1,100));await writeFile(join(resources,'qwen-rotation.key'),key1)
  await rotateInstalledQwenKey({userDataPath:userData,resourcesPath:resources})
  const voiceStore=new VoiceSettingsStore(userData),realtimeStore=new QwenRealtimeSettingsStore(userData),realtime=(await realtimeStore.load())!
  await voiceStore.save({provider:'qwen_voice_clone',relayUrl:'',voiceId:'synthetic-voice',model:'qwen3-tts-vc-2026-01-22',region:'cn-beijing',apiKey:newKey})
  await realtimeStore.save({...realtime,enabled:false})
  const key2=randomBytes(32);await writeFile(join(resources,'qwen-rotation.bin'),sealQwenRotation(generation(),'synthetic-later-key-never-real',key2,200));await writeFile(join(resources,'qwen-rotation.key'),key2)
  await rotateInstalledQwenKey({userDataPath:userData,resourcesPath:resources})
  const updated=(await new QwenRealtimeSettingsStore(userData).load())!
  expect(updated.enabled).toBe(false)
  expect((await new VoiceSettingsStore(userData).loadEffective(updated))?.apiKey).toBe('synthetic-later-key-never-real')
})

it('keeps an explicitly selected non-Qwen relay across future generations',async()=>{
  await put('qwen-realtime-settings.json',{endpoint:'https://synthetic.cn-beijing.maas.aliyuncs.com',model:'qwen3.8-omni-flash-realtime',voice:'Tina',enabled:true,encryptedApiKey:seal(oldKey)})
  const first=randomBytes(32);await writeFile(join(resources,'qwen-rotation.bin'),sealQwenRotation(generation(),newKey,first,100));await writeFile(join(resources,'qwen-rotation.key'),first)
  await rotateInstalledQwenKey({userDataPath:userData,resourcesPath:resources})
  const voice=new VoiceSettingsStore(userData), realtime=new QwenRealtimeSettingsStore(userData)
  await voice.save({provider:'relay_pcm',relayUrl:'https://relay.invalid',voiceId:'synthetic-voice',apiKey:otherKey})
  await realtime.save({...((await realtime.load())!),enabled:false})
  const second=randomBytes(32);await writeFile(join(resources,'qwen-rotation.bin'),sealQwenRotation(generation(),'synthetic-later-key-never-real',second,200));await writeFile(join(resources,'qwen-rotation.key'),second)
  await rotateInstalledQwenKey({userDataPath:userData,resourcesPath:resources})
  const result=await new VoiceSettingsStore(userData).loadEffective(await new QwenRealtimeSettingsStore(userData).load())
  expect(result?.provider).toBe('relay_pcm');expect(result?.apiKey).toBe(otherKey)
})

it.each(['aliyun_cosyvoice','relay_pcm'] as const)('preserves an explicit non-Qwen voice credential: %s',async provider=>{
  const voice=provider==='aliyun_cosyvoice'?{provider,relayUrl:'',voiceId:'longanrou_v3',model:'cosyvoice-v3-flash',workspaceId:'synthetic',region:'cn-beijing',encryptedApiKey:seal(otherKey)}
    :{provider,relayUrl:'https://relay.invalid',voiceId:'synthetic-voice',encryptedApiKey:seal(otherKey)}
  await put('voice-settings.json',voice);await put('asr-settings.json',{provider:'dashscope',baseUrl:qwenUrl,model:'qwen3-asr-flash',encryptedApiKey:seal(oldKey)})
  await makePayload();await rotateInstalledQwenKey({userDataPath:userData,resourcesPath:resources})
  expect((await new VoiceSettingsStore(userData).load())?.apiKey).toBe(otherKey)
  expect(await read('voice-settings.json')).toEqual(voice)
})

it('persists normal parent realtime save and clear in the overlay without exposing old key on restart',async()=>{
  const prior={endpoint:'https://synthetic.cn-beijing.maas.aliyuncs.com',model:'qwen3.8-omni-flash-realtime',voice:'Tina',enabled:true,encryptedApiKey:seal(oldKey)}
  await put('qwen-realtime-settings.json',prior);await makePayload();await rotateInstalledQwenKey({userDataPath:userData,resourcesPath:resources})
  const store=new QwenRealtimeSettingsStore(userData), current=(await store.load())!
  const beforeFingerprint=await cloudProviderSettingsFingerprint(userData)
  await store.save({...current,enabled:false})
  expect(await cloudProviderSettingsFingerprint(userData)).not.toBe(beforeFingerprint)
  expect((await new QwenRealtimeSettingsStore(userData).load())?.enabled).toBe(false)
  expect(await read('qwen-realtime-settings.json')).toEqual(prior)
  await store.clear()
  expect(await new QwenRealtimeSettingsStore(userData).load()).toBeNull()
  expect(await new QwenRealtimeSettingsStore(userData).loadOrImport({environmentKey:'synthetic-environment-key',endpoint:prior.endpoint})).toBeNull()
  expect(await store.resolveApiKey('',{environmentKey:'synthetic-environment-key'})).toBe('')
  await store.save({...current,apiKey:'synthetic-user-reenabled-key',enabled:true})
  expect((await new QwenRealtimeSettingsStore(userData).load())?.apiKey).toBe('synthetic-user-reenabled-key')
  expect(await read('qwen-realtime-settings.json')).toEqual(prior)
})

it('fails closed if a confirmed clear edit disappears',async()=>{
  await put('qwen-realtime-settings.json',{endpoint:'https://synthetic.cn-beijing.maas.aliyuncs.com',model:'qwen3.8-omni-flash-realtime',voice:'Tina',enabled:true,encryptedApiKey:seal(oldKey)})
  await makePayload();await rotateInstalledQwenKey({userDataPath:userData,resourcesPath:resources})
  await new QwenRealtimeSettingsStore(userData).clear()
  const dir=join(userData,'qwen-key-rotation'),file=(await readdir(dir)).find(name=>name.endsWith('.1.edit'))!
  await unlink(join(dir,file))
  await expect(new QwenRealtimeSettingsStore(userData).loadOrImport({environmentKey:'synthetic-old-environment-key',endpoint:'https://synthetic.cn-beijing.maas.aliyuncs.com'})).rejects.toThrow('QWEN_ROTATION_SOURCE_CHANGED')
})

it('fails closed if an earlier protected edit head is replayed over a later save',async()=>{
  await put('asr-settings.json',{provider:'dashscope',baseUrl:qwenUrl,model:'qwen3-asr-flash',encryptedApiKey:seal(oldKey)})
  await makePayload();await rotateInstalledQwenKey({userDataPath:userData,resourcesPath:resources})
  const store=new CloudAsrSettingsStore(userData),current=(await store.load())!
  await store.save({...current,model:'qwen-first'})
  const dir=join(userData,'qwen-key-rotation'),headName=(await readdir(dir)).find(name=>name.endsWith('.head'))!,oldHead=await readFile(join(dir,headName))
  await store.save({...current,model:'qwen-second'})
  await writeFile(join(dir,headName),oldHead)
  await expect(new CloudAsrSettingsStore(userData).load()).rejects.toThrow('QWEN_ROTATION_SOURCE_CHANGED')
})

it.each(['journal_only','partial_edit','marker_written','head_written'] as const)('recovers interrupted parent clear at %s',async phase=>{
  const prior={endpoint:'https://synthetic.cn-beijing.maas.aliyuncs.com',model:'qwen3.8-omni-flash-realtime',voice:'Tina',enabled:true,encryptedApiKey:seal(oldKey)}
  await put('qwen-realtime-settings.json',prior);await makePayload();await rotateInstalledQwenKey({userDataPath:userData,resourcesPath:resources})
  const active=readQwenOverlay(userData)!,entry=active.overlay.entries.find(value=>value.name==='qwen-realtime-settings.json')!
  const name='qwen-realtime-settings.json',base=join(userData,'qwen-key-rotation',`${name}.${active.pointer.generation}`)
  const edit={version:1,generation:active.pointer.generation,revision:1,sourceHash:entry.sourceHash,after:null}
  const bytes=Buffer.from('synthetic-sealed:'+JSON.stringify(edit)),digest=(v:Buffer)=>createHash('sha256').update(v).digest('hex')
  const journal={version:1,name,generation:active.pointer.generation,sourceHash:entry.sourceHash,revision:1,priorHeadHash:null,bytes:bytes.toString('base64')}
  await writeFile(join(userData,'qwen-key-edit-journal.bin'),Buffer.from('synthetic-sealed:'+JSON.stringify(journal)))
  if(phase!=='journal_only')await writeFile(`${base}.1.edit`,phase==='partial_edit'?bytes.subarray(0,Math.floor(bytes.length/2)):bytes)
  if(['marker_written','head_written'].includes(phase))await writeFile(`${base}.required`,Buffer.from('synthetic-sealed:'+JSON.stringify({version:1,generation:active.pointer.generation,name})))
  if(phase==='head_written')await writeFile(`${base}.head`,Buffer.from('synthetic-sealed:'+JSON.stringify({version:1,generation:active.pointer.generation,name,revision:1,hash:digest(bytes)})))
  expect(await recoverQwenKeyRotation({userDataPath:userData,resourcesPath:resources})).toBe(true)
  expect(await new QwenRealtimeSettingsStore(userData).load()).toBeNull()
  expect(await read('qwen-realtime-settings.json')).toEqual(prior)
})

it('persists parent model changes and clear without reimporting a prior key',async()=>{
  const prior={provider:'qwen',baseUrl:qwenUrl,model:'qwen3.7-plus',voiceIntentModel:'qwen3.8-flash',encryptedApiKey:seal(oldKey)}
  await put('model-settings.json',prior);await makePayload();await rotateInstalledQwenKey({userDataPath:userData,resourcesPath:resources})
  const store=new ModelSettingsStore(userData),current=(await store.load())!
  await store.save({...current,model:'qwen3.7-plus-new'})
  expect((await new ModelSettingsStore(userData).load())?.model).toBe('qwen3.7-plus-new')
  expect((await new ModelSettingsStore(userData).load())?.apiKey).toBe(newKey)
  await store.clear()
  expect(await new ModelSettingsStore(userData).load()).toBeNull()
  expect(await new ModelSettingsStore(userData).loadOrImportDeepSeekFromMacOSKeychain('synthetic-service')).toBeNull()
  expect(await read('model-settings.json')).toEqual(prior)
})

it('preserves independent voice clear and re-enable without changing the original record',async()=>{
  const prior={provider:'qwen_voice_clone',relayUrl:'',voiceId:'synthetic-voice',model:'qwen3-tts-vc-2026-01-22',region:'cn-beijing',encryptedApiKey:seal(oldKey)}
  await put('voice-settings.json',prior);await makePayload();await rotateInstalledQwenKey({userDataPath:userData,resourcesPath:resources})
  const store=new VoiceSettingsStore(userData),current=(await store.load())!
  await store.clear();expect(await new VoiceSettingsStore(userData).loadEffective(null)).toBeNull()
  await store.save({...current,apiKey:'synthetic-voice-reenable-key'})
  expect((await new VoiceSettingsStore(userData).load())?.apiKey).toBe('synthetic-voice-reenable-key')
  expect(await read('voice-settings.json')).toEqual(prior)
})

it('retains a user edit and explicit tombstone across a later rotation generation',async()=>{
  const asr={provider:'dashscope' as const,baseUrl:qwenUrl,model:'qwen3-asr-flash',encryptedApiKey:seal(oldKey)}
  const realtime={endpoint:'https://synthetic.cn-beijing.maas.aliyuncs.com',model:'qwen3.8-omni-flash-realtime',voice:'Tina',enabled:true,encryptedApiKey:seal(oldKey)}
  await put('asr-settings.json',asr);await put('qwen-realtime-settings.json',realtime)
  const key1=randomBytes(32);await writeFile(join(resources,'qwen-rotation.bin'),sealQwenRotation(generation(),newKey,key1,100));await writeFile(join(resources,'qwen-rotation.key'),key1)
  await rotateInstalledQwenKey({userDataPath:userData,resourcesPath:resources})
  const asrStore=new CloudAsrSettingsStore(userData),asrCurrent=(await asrStore.load())!
  await asrStore.save({...asrCurrent,model:'qwen3-asr-flash-realtime'})
  await new QwenRealtimeSettingsStore(userData).clear()
  const key2=randomBytes(32);await writeFile(join(resources,'qwen-rotation.bin'),sealQwenRotation(generation(),'synthetic-later-key-never-real',key2,200));await writeFile(join(resources,'qwen-rotation.key'),key2)
  await rotateInstalledQwenKey({userDataPath:userData,resourcesPath:resources})
  expect((await new CloudAsrSettingsStore(userData).load())?.model).toBe('qwen3-asr-flash-realtime')
  expect((await new CloudAsrSettingsStore(userData).load())?.apiKey).toBe('synthetic-later-key-never-real')
  expect(await new QwenRealtimeSettingsStore(userData).load()).toBeNull()
  expect(await read('asr-settings.json')).toEqual(asr);expect(await read('qwen-realtime-settings.json')).toEqual(realtime)
})

it('keeps pet audio clear revocation effective without changing original encrypted record',async()=>{
  const pet={version:1,revision:1,actionId:'synthetic-action',config:{purpose:'pet_original_audio_formative_v1',baseUrl:qwenUrl,model:'qwen3-omni-flash-2025-12-01',apiKey:oldKey,enabled:false,tariffId:''}}
  const original=Buffer.from('synthetic-sealed:'+JSON.stringify(pet));await writeFile(join(userData,'pet-audio-settings.bin'),original)
  await makePayload();await rotateInstalledQwenKey({userDataPath:userData,resourcesPath:resources})
  const store=new PetAudioSettingsStore(userData),current=store.runtimeConfig()!
  store.execute({kind:'clear',expectedRevision:current.revision,actionId:'synthetic-clear-action'})
  expect(new PetAudioSettingsStore(userData).runtimeConfig()?.enabled).toBe(false)
  expect(await readFile(join(userData,'pet-audio-settings.bin'))).toEqual(original)
})
