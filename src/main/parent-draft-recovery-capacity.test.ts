import {expect,it} from 'vitest'
import * as fs from 'node:fs'
import * as path from 'node:path'
import * as os from 'node:os'
import {createCipheriv,createDecipheriv,randomBytes} from 'node:crypto'
import {ParentDraftRecoveryStore} from './parent-draft-recovery'
it('reserves the orphan scope before allowing another canonical scope at capacity',()=>{
 const dir=fs.mkdtempSync(path.join(fs.realpathSync(os.tmpdir()),'hsc-orphan-capacity-')),key=randomBytes(32)
 const codec={assertAvailable(){},encrypt(s:string){const iv=randomBytes(12),c=createCipheriv('aes-256-gcm',key,iv),b=Buffer.concat([c.update(s),c.final()]);return Buffer.concat([iv,c.getAuthTag(),b])},decrypt(b:Buffer){const d=createDecipheriv('aes-256-gcm',key,b.subarray(0,12));d.setAuthTag(b.subarray(12,28));return Buffer.concat([d.update(b.subarray(28)),d.final()]).toString('utf8')}}
 const value=(childId:string)=>({schema:1 as const,childId,scope:'capture' as const,acknowledged:null,latest:{page:{title:'synthetic'},capture_ref:'',capture_version:'',review_drafts:{},action_ids:{}},pending:null})
 const root=path.join(dir,'parent-draft-recovery'),options={userDataPath:dir,profileId:'profile',codec}
 try{
  const first=new ParentDraftRecoveryStore(options)
  for(let i=0;i<15;i++)first.write('child-'+i,'capture',0,value('child-'+i))
  const orphan=path.join(root,'.pending-'+ 'a'.repeat(48))
  const interrupted=new ParentDraftRecoveryStore({...options,fault(p){if(p==='before-rename'){const real=fs.readdirSync(root).find(n=>n.startsWith('.pending-'))!;fs.copyFileSync(path.join(root,real),orphan,fs.constants.COPYFILE_EXCL);const fd=fs.openSync(orphan,'r+');fs.fsyncSync(fd);fs.closeSync(fd);throw Error('synthetic interruption')}}})
  expect(()=>interrupted.write('orphan-child','capture',0,value('orphan-child'))).toThrow('DRAFT_RECOVERY_IO')
  const rebuilt=new ParentDraftRecoveryStore(options)
  // This should not occupy the sole remaining scope slot already owned by the orphan.
  let newWriteAllowed=false, promoted=false, reconstructCode='none'
  try{rebuilt.write('new-child','capture',0,value('new-child'));newWriteAllowed=true}catch{}
  const before=fs.readdirSync(root).filter(n=>n.endsWith('.pdr')).length
  try{rebuilt.recover('orphan-child','capture',1);promoted=true}catch{}
  const after=fs.readdirSync(root).filter(n=>n.endsWith('.pdr')).length
  try{new ParentDraftRecoveryStore(options)}catch(e){reconstructCode=(e as Error).message==='DRAFT_RECOVERY_CAPACITY'?'DRAFT_RECOVERY_CAPACITY':'other'}
  expect(newWriteAllowed).toBe(false)
  expect(after).toBeLessThanOrEqual(16)
  expect(promoted).toBe(true)
  expect(reconstructCode).toBe('none')
 }finally{fs.rmSync(dir,{recursive:true,force:true})}
})
