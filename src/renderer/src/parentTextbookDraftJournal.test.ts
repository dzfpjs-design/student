import { describe, expect, it } from 'vitest'
import { ParentTextbookDraftJournal, type TextbookEditingPayload } from './parentTextbookDraftJournal'
import { ApiError } from './api'

const payload = (title: string): TextbookEditingPayload => ({ page:{ title }, capture_ref:'', capture_version:'', review_drafts:{}, action_ids:{} })
const empty = { child_id:'four',scope:'capture',storage_generation:'generation-a', revision:0,payload:null,last_action_id:null,last_expected_revision:null,updated_at:null,stale_refs:[],cloud_transmission_allowed:false }

function store() {
  let current = structuredClone(empty) as Record<string, unknown>
  let writes = 0
  const api = async (_: string, init?: RequestInit): Promise<unknown> => {
    if (init) {
      const request = JSON.parse(String(init.body))
      if (request.expected_revision !== current.revision) throw new Error('conflict')
      current = {...current,revision:Number(current.revision)+1,payload:request.payload,last_action_id:request.client_action_id,last_expected_revision:request.expected_revision,updated_at:'now'}
      writes++
    }
    return {draft:structuredClone(current)}
  }
  return {api,get writes(){ return writes }}
}

describe('parent draft journal', () => {
  it.each([409,422])('only an explicit product %s rejection plus unchanged readback permits a corrected new action',async(status)=>{
    let reject=true;const actions:string[]=[];const storage=store()
    const api=async(path:string,init?:RequestInit)=>{
      if(init){actions.push(JSON.parse(String(init.body)).client_action_id);if(reject)throw new ApiError('explicit product refusal',status,'TEXTBOOK_DRAFT_CONFLICT')}
      return storage.api(path,init)
    }
    const journal=new ParentTextbookDraftJournal(api,'four','capture',undefined,'memory-only');await journal.load();journal.update(payload('未提交的旧原页'))
    await expect(journal.flush()).rejects.toThrow(/本次保存被拒/);expect(journal.state.phase).toBe('rejected');expect(storage.writes).toBe(0)
    journal.update(payload('明确修正后的原页'));expect(actions).toHaveLength(1)
    reject=false;await journal.retry();expect(storage.writes).toBe(1);expect(actions[1]).not.toBe(actions[0])
    expect(journal.state.payload).toEqual(payload('明确修正后的原页'))
  })
  it.each([['one excessive line', ['测'.repeat(201)]], ['too many lines', Array(25).fill('17')]] as const)(
    'retains %s locally without an invalid pending request, then saves a correction', async (_name, answers) => {
      const storage=store(),journal=new ParentTextbookDraftJournal(storage.api,'four','capture',undefined,'memory-only')
      await journal.load()
      const value={...payload('原先文字不丢'),objective_draft:{question_number:'6',full_question:'公开题干',official_answers:[...answers],candidate_node_id:null,
        paper:{capture_ref:'paper',source_version:'paper-v1'},answer_key:{capture_ref:'answer',source_version:'answer-v1'}}}
      journal.update(value);await expect(journal.flush()).rejects.toThrow(/编辑超过草稿保存范围/)
      expect(journal.state.phase).toBe('invalid');expect(journal.state.payload).toEqual(value);expect(storage.writes).toBe(0)
      const corrected={...value,objective_draft:{...value.objective_draft,official_answers:['17']}}
      journal.update(corrected);await journal.flush()
      expect(storage.writes).toBe(1);expect(journal.state.phase).toBe('saved');expect(journal.state.payload).toEqual(corrected)
    })

  it('loads before writing and restores actual acknowledged editing after remount', async () => {
    const storage = store()
    const first = new ParentTextbookDraftJournal(storage.api,'four','capture',undefined,'memory-only')
    expect(() => first.update(payload('before load'))).toThrow()
    await first.load(); first.update(payload('苹果')); await first.flush()
    expect(first.state.phase).toBe('saved')
    const next = new ParentTextbookDraftJournal(storage.api,'four','capture',undefined,'memory-only')
    expect((await next.load()).payload).toEqual(payload('苹果'))
    expect(storage.writes).toBe(1)
  })
  it('reconciles a lost save reply through readback without resending', async () => {
    const storage = store(); let lost = false
    const api = async (path: string, init?: RequestInit): Promise<unknown> => {
      const result = await storage.api(path,init)
      if (init && !lost) { lost = true; throw new Error('lost response') }
      return result
    }
    const journal = new ParentTextbookDraftJournal(api,'four','capture',undefined,'memory-only')
    await journal.load(); journal.update(payload('one')); await journal.flush()
    expect(journal.state.phase).toBe('saved'); expect(storage.writes).toBe(1)
  })
  it('serializes newer typing while an older write is in flight', async () => {
    const storage = store(); let release!: () => void
    const gate = new Promise<void>(resolve => { release = resolve })
    let first = true
    const api = async (path: string, init?: RequestInit): Promise<unknown> => {
      if (init && first) { first=false; await gate }
      return storage.api(path,init)
    }
    const journal = new ParentTextbookDraftJournal(api,'four','capture',undefined,'memory-only')
    await journal.load(); journal.update(payload('old'))
    await Promise.resolve(); journal.update(payload('new')); release(); await journal.flush()
    expect(journal.state.payload).toEqual(payload('new')); expect(journal.state.phase).toBe('saved')
    expect(storage.writes).toBe(2)
  })
  it('retains the exact failed request until explicit retry', async () => {
    const storage = store(); let offline = true; const ids: string[] = []
    const api = async (path: string, init?: RequestInit): Promise<unknown> => {
      if (init) { ids.push(JSON.parse(String(init.body)).client_action_id); if(offline) throw new Error('offline') }
      return storage.api(path,init)
    }
    const journal = new ParentTextbookDraftJournal(api,'four','capture',undefined,'memory-only')
    await journal.load(); journal.update(payload('old')); await expect(journal.flush()).rejects.toThrow()
    journal.update(payload('new')); expect(ids).toHaveLength(1)
    offline=false; await journal.retry(); expect(ids[0]).toBe(ids[1]); expect(journal.state.payload).toEqual(payload('new'))
  })
  it('rejects foreign or invented replies without claiming saved', async () => {
    const api = async (_: string, init?: RequestInit): Promise<unknown> => ({draft:init ? {...empty,revision:1,payload:payload('invented')} : empty})
    const journal = new ParentTextbookDraftJournal(api,'four','capture',undefined,'memory-only')
    await journal.load(); journal.update(payload('real')); await expect(journal.flush()).rejects.toThrow()
    expect(journal.state.phase).not.toBe('saved')
  })
  it('does not overwrite newer windows or tombstones', async () => {
    const storage = store()
    const one = new ParentTextbookDraftJournal(storage.api,'four','capture',undefined,'memory-only')
    const two = new ParentTextbookDraftJournal(storage.api,'four','capture',undefined,'memory-only')
    await one.load(); one.update(payload('saved')); await one.flush(); await two.load(); one.update(null); await one.flush()
    two.update(payload('revived')); await expect(two.flush()).rejects.toThrow()
    expect(two.state.phase).toBe('conflict'); expect(storage.writes).toBe(2)
    await expect(two.retry()).rejects.toThrow(); expect(storage.writes).toBe(2)
  })
  it('does not replace corrupted load with an empty writable journal', async () => {
    const journal = new ParentTextbookDraftJournal(async () => {throw new Error('decrypt failed')},'four','capture',undefined,'memory-only')
    await expect(journal.load()).rejects.toThrow()
    expect(() => journal.update(payload('overwrite'))).toThrow(); expect(journal.state.phase).toBe('error')
  })
  it('rejects a different child load and a restored storage generation even at the same revision', async () => {
    const foreign = new ParentTextbookDraftJournal(async () => ({draft:{...empty,child_id:'other-child'}}),'four','capture',undefined,'memory-only')
    await expect(foreign.load()).rejects.toThrow();expect(() => foreign.update(payload('foreign'))).toThrow()
    let restored = false
    const journal = new ParentTextbookDraftJournal(async (_path,init) => {
      if(init) {restored=true;throw new Error('storage changed')}
      return {draft:{...empty,storage_generation:restored?'generation-b':'generation-a'}}
    },'four','capture',undefined,'memory-only')
    await journal.load();journal.update(payload('old window'));await expect(journal.flush()).rejects.toThrow()
    expect(journal.state.phase).toBe('conflict');await expect(journal.retry()).rejects.toThrow()
  })
  it('shares one in-flight initial load and never overwrites edits with a second delayed load', async () => {
    let release!: (value:unknown) => void;let count=0
    const wait = new Promise(resolve => {release=resolve})
    const journal = new ParentTextbookDraftJournal(async () => {count++;return wait},'four','capture',undefined,'memory-only')
    const one=journal.load(),two=journal.load();expect(one).toBe(two)
    release({draft:empty});await one;expect(count).toBe(1)
  })
  it('compares a conflict without writing and adopts only the rechecked saved version', async () => {
    const storage=store()
    const one=new ParentTextbookDraftJournal(storage.api,'four','capture',undefined,'memory-only')
    const two=new ParentTextbookDraftJournal(storage.api,'four','capture',undefined,'memory-only')
    await one.load();await two.load()
    one.update(payload('已保存'));await one.flush()
    two.update(payload('本窗口'));await expect(two.flush()).rejects.toThrow()
    const comparison=await two.inspectConflict()
    expect(comparison.local).toEqual(payload('本窗口'));expect(comparison.saved).toEqual(payload('已保存'))
    expect(two.state.payload).toEqual(payload('本窗口'));expect(storage.writes).toBe(1)
    expect(await two.adoptSavedConflict()).toEqual(payload('已保存'))
    await two.flush();expect(storage.writes).toBe(1)
    const restarted=new ParentTextbookDraftJournal(storage.api,'four','capture',undefined,'memory-only')
    expect((await restarted.load()).payload).toEqual(payload('已保存'))
  })
  it('rejects a saved version changed after comparison, then accepts an explicitly rechecked tombstone', async () => {
    const storage=store(),one=new ParentTextbookDraftJournal(storage.api,'four','capture',undefined,'memory-only'),two=new ParentTextbookDraftJournal(storage.api,'four','capture',undefined,'memory-only')
    await one.load();await two.load();one.update(payload('saved'));await one.flush()
    two.update(payload('local'));await expect(two.flush()).rejects.toThrow();await two.inspectConflict()
    one.update(null);await one.flush()
    await expect(two.adoptSavedConflict()).rejects.toThrow();expect(two.state.payload).toEqual(payload('local'))
    expect(two.state.phase).toBe('conflict');expect(storage.writes).toBe(2)
    expect((await two.inspectConflict()).saved).toBeNull()
    expect(await two.adoptSavedConflict()).toBeNull();await two.flush();expect(storage.writes).toBe(2)
  })
  it('does not discard newer local typing, a foreign read, or an unknown save', async () => {
    const storage=store();let foreign=false
    const api=async (path:string,init?:RequestInit) => {
      const result=await storage.api(path,init) as {draft:typeof empty}
      return foreign?{draft:{...result.draft,child_id:'other'}}:result
    }
    const one=new ParentTextbookDraftJournal(api,'four','capture',undefined,'memory-only'),two=new ParentTextbookDraftJournal(api,'four','capture',undefined,'memory-only')
    await one.load();await two.load();one.update(payload('saved'));await one.flush()
    two.update(payload('local'));await expect(two.flush()).rejects.toThrow();await two.inspectConflict()
    two.update(payload('newer local'));await expect(two.adoptSavedConflict()).rejects.toThrow()
    foreign=true;await expect(two.inspectConflict()).rejects.toThrow();expect(two.state.payload).toEqual(payload('newer local'))
    expect(storage.writes).toBe(1)
    const unknown=new ParentTextbookDraftJournal(async (_p,init)=>{if(init)throw new Error('offline');return {draft:empty}},'four','capture',undefined,'memory-only')
    await unknown.load();unknown.update(payload('unknown'));await expect(unknown.flush()).rejects.toThrow()
    await expect(unknown.inspectConflict()).rejects.toThrow();await expect(unknown.adoptSavedConflict()).rejects.toThrow()
    expect(unknown.state.payload).toEqual(payload('unknown'))
  })
  it('rejects a late comparison or adoption after local input changes in flight', async () => {
    const storage=store();let gate:Promise<void>|null=null,release!:()=>void
    const api=async(path:string,init?:RequestInit)=>{if(!init&&gate)await gate;return storage.api(path,init)}
    const one=new ParentTextbookDraftJournal(api,'four','capture',undefined,'memory-only'),two=new ParentTextbookDraftJournal(api,'four','capture',undefined,'memory-only')
    await one.load();await two.load();one.update(payload('saved'));await one.flush()
    two.update(payload('local'));await expect(two.flush()).rejects.toThrow()
    gate=new Promise(resolve=>{release=resolve})
    const inspecting=two.inspectConflict();await Promise.resolve();two.update(payload('new local'));release()
    await expect(inspecting).rejects.toThrow();expect(two.state.payload).toEqual(payload('new local'))
    gate=null;await two.inspectConflict();gate=new Promise(resolve=>{release=resolve})
    const adopting=two.adoptSavedConflict();await Promise.resolve();two.update(payload('newest local'));release()
    await expect(adopting).rejects.toThrow();expect(two.state.payload).toEqual(payload('newest local'))
    expect(storage.writes).toBe(1);expect(two.state.phase).toBe('conflict')
  })
  it('requires renewed comparison when storage is restored during confirmation', async () => {
    const storage=store();let restored=false
    const api=async(path:string,init?:RequestInit)=>{
      const result=await storage.api(path,init) as {draft:typeof empty}
      return restored?{draft:{...result.draft,storage_generation:'generation-b',payload:null,revision:0}}:result
    }
    const one=new ParentTextbookDraftJournal(api,'four','capture',undefined,'memory-only'),two=new ParentTextbookDraftJournal(api,'four','capture',undefined,'memory-only')
    await one.load();await two.load();one.update(payload('saved'));await one.flush()
    two.update(payload('local'));await expect(two.flush()).rejects.toThrow();await two.inspectConflict()
    restored=true;await expect(two.adoptSavedConflict()).rejects.toThrow()
    const comparison=await two.inspectConflict();expect(comparison.generationChanged).toBe(true);expect(comparison.saved).toBeNull()
    await two.adoptSavedConflict();await two.flush();expect(two.storageGeneration).toBe('generation-b')
    expect(storage.writes).toBe(1)
  })
})
