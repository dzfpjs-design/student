// Synthetic-only bridge, freshly bundled from the current checkout Main modules.
import fs from 'node:fs'
import { syncBuiltinESMExports } from 'node:module'
import { join, resolve } from 'node:path'
import { createPrivateKey, createPublicKey } from 'node:crypto'
import { PetAudioCatalogStore } from '../../../../../src/main/pet-audio-catalog'
import { PetAudioSettingsStore, createPetAudioSettingsHandler } from '../../../../../src/main/pet-audio-settings'
import { PET_AUDIO_TARIFF } from '../../../../../src/shared/petAudioTariffs'
const directory=resolve(process.argv[2]),mode=process.argv[3]
if(!['encrypt','rename'].includes(mode)||!fs.statSync(directory).isDirectory())throw Error('SYNTHETIC_SCOPE_REQUIRED')
Date.now=()=>Date.parse('2026-10-24T12:00:00Z')
const key=createPrivateKey({key:Buffer.concat([Buffer.from('302e020100300506032b657004220420','hex'),Buffer.alloc(32,17)]),format:'der',type:'pkcs8'})
const trusted={'synthetic-only-key':{issuer:'Synthetic test maintainer',publicKeyPem:createPublicKey(key).export({format:'pem',type:'spki'}).toString()}}
const store=new PetAudioSettingsStore(directory,new PetAudioCatalogStore(directory,trusted))
const config={purpose:'pet_original_audio_formative_v1' as const,baseUrl:'https://dashscope.aliyuncs.com/compatible-mode/v1',model:PET_AUDIO_TARIFF.model,apiKey:'synthetic-secret',enabled:true,tariffId:PET_AUDIO_TARIFF.id}
store.execute({kind:'save',expectedRevision:0,actionId:'original-builtin-config',config})
const bytes=fs.readFileSync(resolve(process.argv[4]))
const staged=store.catalogs.preview(bytes,'7:primary:parent');let revoked=0,applied=0,caught='NONE'
const handler=createPetAudioSettingsHandler(store,async()=>{}, {apply:async()=>{applied++},applied:()=>false,revoke:()=>{revoked++}})
const originalRename=fs.renameSync
if(mode==='rename')fs.renameSync=((from,to)=>{if(String(to)===join(directory,'pet-audio-settings.bin'))throw Error('SYNTHETIC_SETTINGS_RENAME_FAILED');return originalRename(from,to)}) as typeof fs.renameSync
syncBuiltinESMExports();(globalThis as Record<string,unknown>).__syntheticSettingsEncryptFailure=mode==='encrypt'
try{await handler({sender:{id:7}},'primary','parent',{kind:'save',expectedRevision:1,actionId:'post-fence-failure',catalogToken:staged.token,config:{...config,tariffId:staged.tariff.id}})}
catch(error){caught=error instanceof Error&&['SYNTHETIC_SETTINGS_ENCRYPT_FAILED','SYNTHETIC_SETTINGS_RENAME_FAILED'].includes(error.message)?error.message:'OTHER'}
finally{fs.renameSync=originalRename;syncBuiltinESMExports();(globalThis as Record<string,unknown>).__syntheticSettingsEncryptFailure=false}
console.log(JSON.stringify({status:'SYNTHETIC_POST_FLOOR_SETTINGS_FAILURE',mode,original_error:caught,revoked,applied,floor_exists:fs.existsSync(join(directory,'pet-audio-catalog-control.bin')),summary_enabled:store.execute({kind:'read'}).enabled}))
