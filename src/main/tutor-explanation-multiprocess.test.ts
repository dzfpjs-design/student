// @vitest-environment node
import { execFileSync, fork } from 'node:child_process'
import { chmod, mkdtemp, readFile, readdir, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { afterAll, beforeAll, expect, it } from 'vitest'
let work:string, bundle:string
const fixture=resolve('src/main/__fixtures__/tutor-lock-process.cjs')
const key='synthetic-multiprocess-key'
const start=(mode:string,dir:string)=>fork(fixture,[mode,dir],{silent:true,env:{...process.env,HSC_SYNTHETIC_BUNDLE:bundle}})
const message=(child:ReturnType<typeof fork>)=>new Promise<any>((yes,no)=>{child.once('message',yes);child.once('error',no);child.once('exit',code=>no(Error(`exited before message ${code}`)))})
const exit=(child:ReturnType<typeof fork>)=>new Promise<number|null>(yes=>{if(child.exitCode!==null)yes(child.exitCode);else child.once('exit',yes)})
const initialize=async()=>{const d=await mkdtemp(join(work,'data-'));await writeFile(join(d,'tutor-explanation-settings.json'),JSON.stringify({version:1,encryptedApiKey:Buffer.from(key).reverse().toString('base64')}));return d}
beforeAll(async()=>{work=await mkdtemp(join(tmpdir(),'hsc-multiprocess-'));bundle=join(work,'settings.cjs');execFileSync(resolve('node_modules/.bin/esbuild'),['src/main/tutor-explanation-settings.ts','--bundle','--platform=node','--format=cjs','--external:electron',`--outfile=${bundle}`],{stdio:'ignore'})})
afterAll(async()=>{await rm(work,{recursive:true,force:true})})
it('rejects a second process during a live journal and recovers after the owner crashes',async()=>{
 const dir=await initialize(),first=start('paused',dir);expect(await message(first)).toEqual({event:'pending'})
 const second=start('read',dir);const blocked=await message(second);await exit(second)
 expect(blocked).toEqual({ok:false,error:'TUTOR_EXPLANATION_BUSY'});expect(await readdir(dir)).toContain('tutor-explanation-settings.pending')
 first.send('continue');expect(await exit(first)).toBe(77)
 const third=start('read',dir);expect(await message(third)).toEqual({ok:true,baseUrl:null});await exit(third)
 expect(await readdir(dir)).toEqual(['tutor-explanation-settings.json']);expect(await readFile(join(dir,'tutor-explanation-settings.json'),'utf8')).toContain('encryptedApiKey')
})
it('recovers the journal after rollback I/O fails and its owner exits',async()=>{
 const dir=await initialize(),first=start('io-failure',dir);const failure=await message(first);await exit(first)
 expect(failure.ok).toBe(false);await chmod(dir,0o700)
 expect(await readdir(dir)).toContain('tutor-explanation-settings.pending')
 const recovery=start('read',dir);expect(await message(recovery)).toEqual({ok:true,baseUrl:null});await exit(recovery)
 expect(await readdir(dir)).toEqual(['tutor-explanation-settings.json'])
})
it('refuses malformed or untrusted lock metadata without touching a pending journal',async()=>{
 const dir=await initialize(),lock=join(dir,'tutor-explanation-settings.lock'),pending=join(dir,'tutor-explanation-settings.pending')
 await writeFile(lock,'malformed owner');await writeFile(pending,'preserve this pending marker')
 const child=start('read',dir);expect(await message(child)).toEqual({ok:false,error:'TUTOR_EXPLANATION_LOCK_RECOVERY_REQUIRED'});await exit(child)
 expect(await readFile(pending,'utf8')).toBe('preserve this pending marker');expect(await readFile(lock,'utf8')).toBe('malformed owner')
})
it('releases its own lock when directory sync fails immediately after linking',async()=>{
 const dir=await initialize(),first=start('sync-fail',dir);const failed=await message(first);await exit(first)
 expect(failed).toEqual({ok:false,error:'synthetic directory sync failure'});expect(await readdir(dir)).toEqual(['tutor-explanation-settings.json'])
 const second=start('read',dir);expect(await message(second)).toEqual({ok:true,baseUrl:null});await exit(second)
})
it('preserves pending work when an orphan recovery marker exists, and recovers after safe marker removal',async()=>{
 const { mkdir, rmdir }=await import('node:fs/promises');const dir=await initialize(),first=start('paused',dir)
 expect(await message(first)).toEqual({event:'pending'});first.kill('SIGKILL');await exit(first)
 const marker=join(dir,'tutor-explanation-settings.lock.recovery');await mkdir(marker)
 const blocked=start('read',dir);expect(await message(blocked)).toEqual({ok:false,error:'TUTOR_EXPLANATION_RECOVERY_MARKER_BLOCKED'});await exit(blocked)
 expect(await readdir(dir)).toContain('tutor-explanation-settings.pending')
 // This simulates the documented operator recovery after all owners exited.
 await rmdir(marker);const recovery=start('read',dir);expect(await message(recovery)).toEqual({ok:true,baseUrl:null});await exit(recovery)
 expect(await readdir(dir)).toEqual(['tutor-explanation-settings.json'])
})
