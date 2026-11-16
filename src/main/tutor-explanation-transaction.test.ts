import { createHash } from 'node:crypto'
import { mkdtemp, readFile, readdir, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { beforeEach, afterEach, expect, it, vi } from 'vitest'

vi.mock('electron',()=>({safeStorage:{isEncryptionAvailable:()=>true,encryptString:(v:string)=>Buffer.from(v).reverse(),decryptString:(v:Buffer)=>Buffer.from(v).reverse().toString()}}))
import { loadTutorExplanationConfig, saveTutorExplanationConfig } from './tutor-explanation-settings'
let dir:string
const KEY='synthetic-transaction-key', URL='https://tutor.example.test/v1', MODEL='deepseek-test'
const path=()=>join(dir,'tutor-explanation-settings.json'), journal=()=>join(dir,'tutor-explanation-settings.pending')
const sha=(v:Buffer)=>createHash('sha256').update(v).digest('hex')
const prior=()=>Buffer.from(JSON.stringify({version:1,encryptedApiKey:Buffer.from(KEY).reverse().toString('base64')}))
const next=()=>Buffer.from(JSON.stringify({version:2,encryptedConfig:Buffer.from(JSON.stringify({apiKey:KEY,baseUrl:URL,model:MODEL})).reverse().toString('base64')}))
const pending=(old:Buffer|null, fresh:Buffer, state='pending')=>Buffer.from(JSON.stringify({version:1,state,previous:old?.toString('base64')??null,previousHash:old?sha(old):null,nextHash:sha(fresh)}))
beforeEach(async()=>{dir=await mkdtemp(join(tmpdir(),'hsc-txn-'))})
afterEach(async()=>{await rm(dir,{recursive:true,force:true})})

it.each(['before replacement','after replacement'])('recovers the prior encrypted value after crash %s',async phase=>{
 const old=prior(), fresh=next();await writeFile(journal(),pending(old,fresh));await writeFile(path(),phase==='before replacement'?old:fresh)
 expect(await loadTutorExplanationConfig(dir)).toEqual({apiKey:KEY});expect(await readFile(path())).toEqual(old);expect(await readdir(dir)).toEqual(['tutor-explanation-settings.json'])
})
it('reverts an uncommitted first-time setup by deleting only the exact new value',async()=>{
 const fresh=next();await writeFile(journal(),pending(null,fresh));await writeFile(path(),fresh)
 expect(await loadTutorExplanationConfig(dir)).toBeNull();expect(await readdir(dir)).toEqual([])
})
it('keeps a committed new value when cleanup was interrupted',async()=>{
 const old=prior(),fresh=next();await writeFile(journal(),pending(old,fresh,'committed'));await writeFile(path(),fresh)
 expect(await loadTutorExplanationConfig(dir)).toEqual({apiKey:KEY,baseUrl:URL,model:MODEL});expect(await readdir(dir)).toEqual(['tutor-explanation-settings.json'])
})
it.each(['corrupt','changed','committed-old'])('fails closed on %s journal or unexpected target',async type=>{
 const old=prior(),fresh=next();await writeFile(journal(),type==='corrupt'?'not-json':pending(old,fresh,type==='committed-old'?'committed':'pending'))
 const changed=Buffer.from('independent change');await writeFile(path(),type==='committed-old'?old:changed)
 await expect(loadTutorExplanationConfig(dir)).rejects.toThrow('TUTOR_EXPLANATION_RECOVERY_REQUIRED')
 expect(await readFile(path())).toEqual(type==='committed-old'?old:changed);expect((await readdir(dir))).toContain('tutor-explanation-settings.pending')
})
it('reverts replacement if parent authorization expires after the target rename',async()=>{
 const old=prior();await writeFile(path(),old);let calls=0
 await expect(saveTutorExplanationConfig(dir,{apiKey:'',baseUrl:URL,model:MODEL},async()=>{if(++calls===3)throw Error('parent expired')})).rejects.toThrow('parent expired')
 expect(await readFile(path())).toEqual(old);expect(await readdir(dir)).toEqual(['tutor-explanation-settings.json'])
})
it('serializes concurrent writes and readers while a journal is active',async()=>{
 await writeFile(path(),prior());let release!:()=>void, entered!:()=>void
 const ready=new Promise<void>(resolve=>{entered=resolve}),held=new Promise<void>(resolve=>{release=resolve});let calls=0
 const first=saveTutorExplanationConfig(dir,{apiKey:'',baseUrl:URL,model:MODEL},async()=>{if(++calls===2){entered();await held}})
 await ready;const journalText=await readFile(journal(),'utf8');expect(journalText).not.toContain(KEY);expect(journalText).not.toContain(URL)
 let completed=false;const second=saveTutorExplanationConfig(dir,{apiKey:'',baseUrl:'https://other.example.test/v1',model:MODEL}).then(()=>{completed=true})
 const reader=loadTutorExplanationConfig(dir);await Promise.resolve();expect(completed).toBe(false)
 release();await first;await second;await reader
 expect((await loadTutorExplanationConfig(dir))?.baseUrl).toBe('https://other.example.test/v1');expect(await readdir(dir)).toEqual(['tutor-explanation-settings.json'])
})

it('does not overwrite a target changed after the journal was prepared',async()=>{
 const old=prior(),changed=Buffer.from('synthetic-external-change');await writeFile(path(),old);let calls=0
 await expect(saveTutorExplanationConfig(dir,{apiKey:'',baseUrl:URL,model:MODEL},async()=>{if(++calls===2)await writeFile(path(),changed)})).rejects.toThrow('TUTOR_EXPLANATION_RECOVERY_REQUIRED')
 expect(await readFile(path())).toEqual(changed);expect((await readdir(dir))).toContain('tutor-explanation-settings.pending')
})
it('recovers first setup interrupted before target replacement',async()=>{
 await writeFile(journal(),pending(null,next()))
 expect(await loadTutorExplanationConfig(dir)).toBeNull();expect(await readdir(dir)).toEqual([])
})
