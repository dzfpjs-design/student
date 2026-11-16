// @vitest-environment node
// Original Node file guards with an explicitly injected synthetic native port; no Windows/native claim.
import * as fs from 'node:fs'
import * as path from 'node:path'
import * as os from 'node:os'
import {afterEach,beforeEach,expect,it,vi} from 'vitest'
import {createWindowsDraftAtomicIO} from './parent-draft-windows-atomic'
import {spawnSync} from 'node:child_process'
vi.mock('node:child_process',()=>{const spawnSync=vi.fn();return {spawnSync,default:{spawnSync}}})
const descriptor=Object.getOwnPropertyDescriptor(process,'platform')!
let root:string,store:string,helper:string,temp:string,final:string
beforeEach(()=>{root=fs.mkdtempSync(path.join(fs.realpathSync(os.tmpdir()),'commit-port-public-'));store=path.join(root,'store');helper=path.join(root,'helper');fs.mkdirSync(store);temp=path.join(store,'.pending-'+'a'.repeat(48));final=path.join(store,'b'.repeat(64)+'.pdr');fs.writeFileSync(temp,'synthetic cipher');vi.mocked(spawnSync).mockReset();Object.defineProperty(process,'platform',{...descriptor,value:'win32'})})
afterEach(()=>{Object.defineProperty(process,'platform',descriptor);fs.rmSync(root,{recursive:true,force:true})})
function port(){return {root:store,assertAvailable:vi.fn(),replaceAndFlush:vi.fn((a:string,b:string)=>fs.renameSync(path.join(store,a),path.join(store,b)))}}
it('explicit fixed-root port replaces exactly the original temp/target without compiler or one-shot spawn',()=>{
 const p=port(),atomic=createWindowsDraftAtomicIO(store,helper,p);atomic.replaceAndFlush(temp,final)
 expect(p.replaceAndFlush).toHaveBeenCalledExactlyOnceWith(path.basename(temp),path.basename(final));expect(p.assertAvailable).toHaveBeenCalled();expect(spawnSync).not.toHaveBeenCalled();expect(fs.readFileSync(final,'utf8')).toBe('synthetic cipher')
})
it('injected native failure is IO with no process fallback and no unearned success',()=>{
 const p=port();p.replaceAndFlush.mockImplementation(()=>{throw Error('private native failure')});const atomic=createWindowsDraftAtomicIO(store,helper,p)
 expect(()=>atomic.replaceAndFlush(temp,final)).toThrow('DRAFT_RECOVERY_IO');expect(spawnSync).not.toHaveBeenCalled();expect(fs.existsSync(temp)).toBe(true);expect(fs.existsSync(final)).toBe(false)
})
it('retains original root/file/alias guards before invoking the port',()=>{
 const p=port(),atomic=createWindowsDraftAtomicIO(store,helper,p)
 const other=path.join(root,'outside');fs.writeFileSync(other,'public');fs.unlinkSync(temp);fs.symlinkSync(other,temp)
 expect(()=>atomic.replaceAndFlush(temp,final)).toThrow('DRAFT_RECOVERY_IO');expect(p.replaceAndFlush).not.toHaveBeenCalled()
 fs.unlinkSync(temp);fs.writeFileSync(temp,'public');expect(()=>atomic.replaceAndFlush(temp,other)).toThrow('DRAFT_RECOVERY_IO');expect(p.replaceAndFlush).not.toHaveBeenCalled()
 expect(()=>createWindowsDraftAtomicIO(store,helper,{...p,root})).toThrow('DRAFT_RECOVERY_IO');expect(spawnSync).not.toHaveBeenCalled()
})
