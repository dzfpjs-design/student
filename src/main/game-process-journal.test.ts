// @vitest-environment node
import { randomBytes, randomUUID } from 'node:crypto'
import { mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync, mkdirSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterEach, expect, it } from 'vitest'
import { GameProcessJournal, type GameRecord } from './game-process-journal'
const roots:string[]=[]
function fixture(){const root=mkdtempSync(join(tmpdir(),'hsc-game-journal-'));roots.push(root);return {root,journal:new GameProcessJournal(root)}}
function record(id='one'):GameRecord{return {schema:1,revision:1,sessionId:id,instanceId:randomUUID(),token:randomBytes(32).toString('hex'),state:'reserved',launchIdentity:{pathHash:'a'.repeat(64),fileId:'test'},deadline:Date.now()+60000,supervisor:null,process:null,port:null,exitProof:null,reason:null}}
afterEach(()=>{for(const root of roots.splice(0))rmSync(root,{recursive:true,force:true})})
it('reserves before launching, rejects another source and never replays a closed ID',()=>{
  const {journal}=fixture(),a=record();journal.reserve(a)
  expect(()=>journal.reserve(record('two'))).toThrow()
  expect(new GameProcessJournal(journal.directory).hasUnresolvedLease()).toBe(true)
  journal.update(a,{state:'closed',token:null,exitProof:'launch_failed'})
  expect(journal.hasUnresolvedLease()).toBe(false)
  expect(()=>journal.reserve(a)).toThrow()
  journal.reserve(record('two'))
  expect(journal.list().filter(r=>r.state!=='closed')).toHaveLength(1)
})
it('rejects stale revisions without altering current facts',()=>{
  const {journal}=fixture(),a=record();journal.reserve(a)
  const next=journal.update(a,{state:'launching'})
  expect(()=>journal.update(a,{state:'closed',token:null,exitProof:'launch_failed'})).toThrow('CONFLICT')
  expect(journal.read('one')).toEqual(next)
})
it('explicit cancellation of reserved is a no-spawn fence, not a fabricated exit of a running game',()=>{
  const {journal}=fixture(),a=record();journal.reserve(a)
  journal.update(a,{state:'closed',token:null,exitProof:'cancelled_before_spawn'})
  expect(()=>journal.update(a,{state:'launching'})).toThrow('CONFLICT')
  expect(journal.confirmClosed('one').exitProof).toBe('cancelled_before_spawn')
})
it.each(['json','partial_lease','writer_crash'])('corrupt/partial state blocks new launch and remains intact: %s',kind=>{
  const {journal,root}=fixture(),a=record();journal.reserve(a)
  if(kind==='json')writeFileSync(join(root,readdirSync(root).find(p=>p.endsWith('.json'))!),'{broken')
  if(kind==='partial_lease')writeFileSync(join(root,'active.lock'),'')
  if(kind==='writer_crash')mkdirSync(join(root,'.writer-lock'))
  const before=readdirSync(root).filter(p=>!p.startsWith('.')).map(p=>[p,readFileSync(join(root,p),'utf8')])
  expect(()=>journal.reserve(record('two'))).toThrow()
  expect(readdirSync(root).filter(p=>!p.startsWith('.')).map(p=>[p,readFileSync(join(root,p),'utf8')])).toEqual(before)
})
