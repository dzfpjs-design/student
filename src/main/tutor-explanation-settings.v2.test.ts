import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { loadTutorExplanationConfig, saveTutorExplanationConfig, summarizeTutorExplanation, validateTutorEndpoint } from './tutor-explanation-settings'

const storage = vi.hoisted(() => ({ available: true }))
vi.mock('electron', () => ({ safeStorage: {
  isEncryptionAvailable: () => storage.available,
  encryptString: (v: string) => Buffer.from(v).reverse(),
  decryptString: (v: Buffer) => Buffer.from(v).reverse().toString(),
} }))
let root: string
const KEY='synthetic-existing-key', URL='https://tutor.example.test:7791/v1', MODEL='deepseek-v4.1-flash'
const path=(): string=>join(root,'tutor-explanation-settings.json')
beforeEach(async()=>{root=await mkdtemp(join(tmpdir(),'hsc-tutor-v2-'));storage.available=true})
afterEach(async()=>{await rm(root,{recursive:true,force:true})})
async function v1():Promise<void>{await writeFile(path(),JSON.stringify({version:1,encryptedApiKey:Buffer.from(KEY).reverse().toString('base64')}))}

describe('parent owned tutor settings v2',()=>{
  it('preserves existing v1 key and only migrates after explicit endpoint save',async()=>{
    await v1(); const before=await readFile(path()); const old=await loadTutorExplanationConfig(root)
    expect(old).toEqual({apiKey:KEY});expect(summarizeTutorExplanation(old)).toMatchObject({keyConfigured:true,configured:false,requiresEndpoint:true})
    expect(await readFile(path())).toEqual(before)
    await saveTutorExplanationConfig(root,{baseUrl:URL,model:MODEL,apiKey:''})
    const persisted=await readFile(path(),'utf8'); expect(persisted).not.toContain(KEY);expect(persisted).not.toContain(URL)
    expect(await loadTutorExplanationConfig(root)).toEqual({apiKey:KEY,baseUrl:URL,model:MODEL})
  })
  it.each(['http://tutor.example.test/v1','https://user:pw@tutor.example.test/v1','https://127.0.0.1/v1','https://localhost/v1','https://tutor.example.test/v1?x=1','https://tutor.example.test/%2e%2e/v1','https://tutor.example.test:0/v1'])('rejects unsafe endpoint %s without modifying prior data',async(url)=>{
    await v1();const before=await readFile(path());expect(()=>validateTutorEndpoint(url)).toThrow()
    await expect(saveTutorExplanationConfig(root,{baseUrl:url,model:MODEL,apiKey:''})).rejects.toThrow();expect(await readFile(path())).toEqual(before)
  })
  it('requires a key on first configuration and rejects an unapproved model',async()=>{
    await expect(saveTutorExplanationConfig(root,{baseUrl:URL,model:MODEL,apiKey:''})).rejects.toThrow()
    await expect(saveTutorExplanationConfig(root,{baseUrl:URL,model:'qwen-fast',apiKey:KEY})).rejects.toThrow()
  })
  it('keeps original data on encryption unavailable or parental authorization expiry',async()=>{
    await v1();const before=await readFile(path());storage.available=false
    await expect(saveTutorExplanationConfig(root,{baseUrl:URL,model:MODEL,apiKey:''})).rejects.toThrow();expect(await readFile(path())).toEqual(before)
    storage.available=true
    await expect(saveTutorExplanationConfig(root,{baseUrl:URL,model:MODEL,apiKey:''},async()=>{throw Error('parent expired')})).rejects.toThrow('parent expired')
    expect(await readFile(path())).toEqual(before)
  })
})

it('preserves a confirmed endpoint when a private installer rotates the key',async()=>{
  const { createCipheriv, randomBytes } = await import('node:crypto')
  const { mkdir } = await import('node:fs/promises')
  await v1(); await saveTutorExplanationConfig(root,{baseUrl:URL,model:MODEL,apiKey:''})
  const resources=join(root,'resources');await mkdir(resources)
  const magic=Buffer.from('HSCTUT01'), key=randomBytes(32), iv=randomBytes(12), cipher=createCipheriv('aes-256-gcm',key,iv)
  cipher.setAAD(magic);const data=Buffer.concat([cipher.update('synthetic-replacement-key','utf8'),cipher.final()])
  await writeFile(join(resources,'tutor-explanation.key'),key);await writeFile(join(resources,'tutor-explanation.bin'),Buffer.concat([magic,iv,cipher.getAuthTag(),data]))
  expect(await loadTutorExplanationConfig(root,resources)).toEqual({apiKey:'synthetic-replacement-key',baseUrl:URL,model:MODEL})
  expect(await loadTutorExplanationConfig(root)).toEqual({apiKey:'synthetic-replacement-key',baseUrl:URL,model:MODEL})
})

it('normalizes a safe HTTPS origin to the OpenAI-compatible API path', () => {
  expect(validateTutorEndpoint('https://tutor.example.test:7791')).toBe('https://tutor.example.test:7791/v1')
  expect(validateTutorEndpoint('https://tutor.example.test/')).toBe('https://tutor.example.test/v1')
  expect(() => validateTutorEndpoint('https://tutor.example.test/other')).toThrow()
  expect(() => validateTutorEndpoint('https://user:pw@tutor.example.test')).toThrow()
})
