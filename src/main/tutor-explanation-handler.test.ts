import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { beforeEach, afterEach, expect, it, vi } from 'vitest'
import { createTutorExplanationHandler } from './tutor-explanation-handler'
const storage=vi.hoisted(()=>({available:true}))
vi.mock('electron',()=>({safeStorage:{isEncryptionAvailable:()=>storage.available,encryptString:(s:string)=>Buffer.from(s).reverse(),decryptString:(s:Buffer)=>Buffer.from(s).reverse().toString()}}))
let root:string
const key='synthetic-previous-key',url='https://tutor.example.test/v1',model='deepseek-v4.1-flash'
beforeEach(async()=>{root=await mkdtemp(join(tmpdir(),'hsc-tutor-handler-'));await writeFile(join(root,'tutor-explanation-settings.json'),JSON.stringify({version:1,encryptedApiKey:Buffer.from(key).reverse().toString('base64')}))})
afterEach(async()=>{await rm(root,{recursive:true,force:true})})
it('requires parent authority and explicit confirmation before saving a destination',async()=>{
 const authorize=vi.fn().mockResolvedValue(undefined),apply=vi.fn().mockResolvedValue(undefined)
 const h=createTutorExplanationHandler({userData:root,authorize,apply})
 await expect(h(1,'token',{action:'save',baseUrl:url,model,apiKey:'',confirmed:false} as unknown as import('./tutor-explanation-handler').TutorExplanationCommand)).rejects.toThrow()
 expect(authorize).not.toHaveBeenCalled();expect(apply).not.toHaveBeenCalled()
 const result=await h(1,'token',{action:'save',baseUrl:url,model,apiKey:'',confirmed:true})
 expect(result).toMatchObject({configured:true,baseUrl:url,runtimeApplied:true})
 expect(authorize).toHaveBeenCalledTimes(4);expect(apply).toHaveBeenCalledWith({apiKey:key,baseUrl:url,model})
 expect(JSON.stringify(result)).not.toContain(key)
})
it('does not commit if authorization expires while writing',async()=>{
 const before=await readFile(join(root,'tutor-explanation-settings.json'))
 const authorize=vi.fn().mockResolvedValueOnce(undefined).mockRejectedValueOnce(Error('parent expired')),apply=vi.fn()
 const h=createTutorExplanationHandler({userData:root,authorize,apply})
 await expect(h(1,'token',{action:'save',baseUrl:url,model,apiKey:'',confirmed:true})).rejects.toThrow('parent expired')
 expect(await readFile(join(root,'tutor-explanation-settings.json'))).toEqual(before);expect(apply).not.toHaveBeenCalled()
})
it('reports saved but runtime unapplied on restart failure without discarding durable configuration',async()=>{
 const h=createTutorExplanationHandler({userData:root,authorize:async()=>{},apply:async()=>{throw Error('restart failed')}})
 const result=await h(1,'token',{action:'save',baseUrl:url,model,apiKey:'',confirmed:true})
 expect(result).toMatchObject({configured:true,runtimeApplied:false,previousRuntimeMayStillRun:true})
 const reread=await h(1,'token',{action:'get'});expect(reread).toMatchObject({configured:true,baseUrl:url})
})

it('rolls back if parent authority is revoked after replacement but before commit',async()=>{
 const before=await readFile(join(root,'tutor-explanation-settings.json'))
 const authorize=vi.fn().mockResolvedValueOnce(undefined).mockResolvedValueOnce(undefined).mockResolvedValueOnce(undefined).mockRejectedValueOnce(Error('parent expired')),apply=vi.fn()
 const h=createTutorExplanationHandler({userData:root,authorize,apply})
 await expect(h(1,'token',{action:'save',baseUrl:url,model,apiKey:'',confirmed:true})).rejects.toThrow('parent expired')
 expect(await readFile(join(root,'tutor-explanation-settings.json'))).toEqual(before);expect(apply).not.toHaveBeenCalled()
})
