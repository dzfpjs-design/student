import {StrictMode} from 'react'
import {useEffect,useState} from 'react'
import {spawn,type ChildProcess} from 'node:child_process'
import {once} from 'node:events'
import {mkdtemp,rm} from 'node:fs/promises'
import {tmpdir} from 'node:os'
import {isAbsolute,join} from 'node:path'
import {createInterface} from 'node:readline'
import {act,cleanup,fireEvent,render,screen,waitFor} from '@testing-library/react'
import {expect,it,vi} from 'vitest'
import {ParentPaperFormalReview} from './ParentPaperFormalReview'
import {ApiError} from './api'
import {ParentPaperObjectiveDraft} from './ParentPaperObjectiveDraft'
import {curriculumLocalKey} from './curriculumSupplyApi'
import {ParentTextbookDraftJournal} from './parentTextbookDraftJournal'

it.each(['validate','review','publish'] as const)('real HTTP/SQLite original panel %s commit with hung response keeps the exact pending action across deadline and retry',async heldPhase=>{
  localStorage.removeItem(curriculumLocalKey('child-primary-4'))
  const python=process.env.HSC_OBJECTIVE_TEST_PYTHON??join(process.cwd(),process.platform==='win32'?'.venv/Scripts/python.exe':'.venv/bin/python')
  if(!isAbsolute(python))throw Error('explicit absolute Python required')
  const root=await mkdtemp(join(tmpdir(),'hsc-paper-formal-test-'));let child:ChildProcess|undefined,base='',token='',lose=true
  const writes:string[]=[],attemptedPosts:string[]=[],readPaths:string[]=[]
  let holdOnce=true,releaseLate!:(v:unknown)=>void,committed!:()=>void
  const phaseCommitted=new Promise<void>(resolve=>{committed=resolve})
  const phaseBodies:unknown[]=[],phaseSignals:AbortSignal[]=[]
  try{
    child=spawn(python,['-B','services/local_agent/tests/fixtures/paper_objective_formal_http.py','--data-root',root],
      {cwd:process.cwd(),stdio:['ignore','pipe','pipe'],env:{PATH:process.env.PATH,PYTHONPATH:join(process.cwd(),'services/local_agent/src'),PYTHONDONTWRITEBYTECODE:'1'}})
    let errors='';child.stderr!.on('data',(b:Buffer)=>{errors=(errors+String(b)).slice(-1600)})
    const lines=createInterface({input:child.stdout!})
    const first=await new Promise<string>((resolve,reject)=>{const timer=setTimeout(()=>reject(Error('public fixture startup timeout '+errors)),10000)
      lines.once('line',line=>{clearTimeout(timer);resolve(line)});child!.once('exit',code=>{clearTimeout(timer);reject(Error('fixture exited '+code+' '+errors))})})
    lines.close();child.stdout!.resume();base='http://127.0.0.1:'+JSON.parse(first).port
    async function api(path:string,init?:RequestInit):Promise<unknown>{
      if(!init?.method||init.method==='GET')readPaths.push(path)
      const headers=new Headers(init?.headers);headers.set('X-HSC-Session','public-paper-formal-fixture');headers.set('Content-Type','application/json')
      if(token)headers.set('X-HSC-Parent',token)
      if(init?.method==='POST'){attemptedPosts.push(path);if(path.endsWith('/'+heldPhase)){phaseBodies.push(JSON.parse(String(init.body)));phaseSignals.push(init.signal as AbortSignal)}}
      const response=await fetch(base+path,{...init,headers});const result=await response.json()
      if(!response.ok)throw new ApiError('public local HTTP',response.status,result.detail?.code)
      if(init?.method==='POST'){writes.push(path)
        if(lose&&path.endsWith('/objective-review')){lose=false;throw Error('controlled response dropped after real SQLite commit')}}
      if(init?.method==='POST'&&path.endsWith('/'+heldPhase)&&holdOnce){holdOnce=false;committed();return new Promise(resolve=>{releaseLate=resolve})}
      return result
    }
    token=((await api('/v1/parent/setup',{method:'POST',body:JSON.stringify({pin:'7391'})})) as {parent_token:string}).parent_token
    const props={childId:'child-primary-4',api,onSave:async()=>undefined,blocked:false,draftBinding:'public-Q6'}
    let view=render(<StrictMode><ParentPaperFormalReview {...props}/></StrictMode>)
    fireEvent.click(screen.getByRole('button',{name:'核对已保存稿的送审条件'}))
    await screen.findByLabelText('答案如何评分')
    fireEvent.change(screen.getByLabelText('答案如何评分'),{target:{value:'whole_answer_variants'}})
    const roots=screen.getByLabelText('对应的已审核原题') as HTMLSelectElement
    fireEvent.change(roots,{target:{value:roots.options[1].value}})
    fireEvent.change(screen.getByLabelText('对应课标'),{target:{value:'req:12'}})
    fireEvent.change(screen.getByLabelText('原页使用范围'),{target:{value:'public'}})
    fireEvent.click(screen.getByRole('checkbox'))
    fireEvent.click(screen.getByRole('button',{name:'送审核（不批准、不发布）'}))
    await screen.findByText(/本次送审结果尚未确认/)
    expect(writes.some(p=>p.endsWith('/review')||p.endsWith('/publish'))).toBe(false)
    const draftPath=`/v1/parent/knowledge-graph/textbook-capture/${props.childId}/drafts/capture`
    const original=(await api(draftPath)) as {draft:{revision:number;storage_generation:string;payload:Record<string,unknown> & {objective_draft:Record<string,unknown>}}}
    const revised=structuredClone(original.draft.payload)
    revised.objective_draft={...revised.objective_draft,question_number:'7',full_question:'公开合成另一未送审原稿：十二加六等于多少？',official_answers:['18']}
    await api(draftPath,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({expected_revision:original.draft.revision,
      expected_generation:original.draft.storage_generation,client_action_id:'public-new-unsent-edit',payload:revised})})
    view.rerender(<StrictMode><ParentPaperFormalReview {...props} draftBinding="public-Q7-unsent"/></StrictMode>)
    fireEvent.click(screen.getByRole('button',{name:'核对上次送审'}))
    await waitFor(()=>expect(screen.getByRole('button',{name:'刷新已保存送审'})).not.toBeDisabled())
    expect(attemptedPosts.filter(p=>p.endsWith('/objective-review'))).toHaveLength(2)
    expect(writes.filter(p=>p.endsWith('/objective-review'))).toHaveLength(1)
    fireEvent.click(screen.getByRole('button',{name:'刷新已保存送审'}))
    fireEvent.click(await screen.findByRole('button',{name:/继续送审：单题原页核对 · 6/ }))
    // The existing queue is loaded only after current permission and immutable receipt validation.
    await screen.findByText(/已进入正式审核队列/)
    async function doOriginalOperation(kind:'validate'|'review'|'publish',label:string):Promise<void>{
      const button=await screen.findByRole('button',{name:label});await waitFor(()=>expect(button).not.toBeDisabled())
      if(kind!==heldPhase){fireEvent.click(button);return}
      vi.useFakeTimers();fireEvent.click(button)
      await phaseCommitted
      const key=curriculumLocalKey(props.childId),raw=localStorage.getItem(key)!
      const pending=JSON.parse(raw).pending;expect(pending.kind).toBe(kind)
      await act(async()=>{await vi.advanceTimersByTimeAsync(10001)})
      const atDeadline=localStorage.getItem(key)!
      expect(JSON.parse(atDeadline).pending).toEqual(pending)
      const actual=await api('/api/parent/children/child-primary-4/curriculum-supply/audit') as {packages:{state:string;version:number}[]}
      console.info('PUBLIC_SYNTHETIC_COMMITTED_WAIT',JSON.stringify({phase:kind,package_states:actual.packages.map(p=>({state:p.state,version:p.version})),pending_unchanged:JSON.stringify(JSON.parse(atDeadline).pending)===JSON.stringify(pending),retry_enabled:!(screen.getByRole('button',{name:'重试原请求'}) as HTMLButtonElement).disabled}))
      expect(screen.getByRole('button',{name:'重试原请求'})).not.toBeDisabled()
      expect(phaseSignals[0]?.aborted).toBe(true)
      vi.useRealTimers()
      fireEvent.click(screen.getByRole('button',{name:'重试原请求'}))
      await waitFor(()=>expect(JSON.parse(localStorage.getItem(key)!).pending).toBeNull())
      expect(phaseBodies).toEqual([pending.body,pending.body])
      await act(async()=>{releaseLate({late_untrusted_result:true})})
      expect(JSON.parse(localStorage.getItem(key)!).pending).toBeNull()
    }
    await doOriginalOperation('validate','独立校验')
    await screen.findByRole('button',{name:'审核通过'})
    fireEvent.change(screen.getByLabelText('审核备注'),{target:{value:'公开合成两原页、完整题干答案、节点和课标均已核对。'}})
    await waitFor(()=>expect(screen.getByRole('button',{name:'审核通过'})).not.toBeDisabled())
    await doOriginalOperation('review','审核通过')
    await screen.findByRole('button',{name:'发布到题库'})
    await waitFor(()=>expect(screen.getByRole('button',{name:'发布到题库'})).not.toBeDisabled())
    await doOriginalOperation('publish','发布到题库')
    await waitFor(()=>expect(screen.getByText('当前有效题目：1道')).toBeInTheDocument())
    const audit=await api('/api/parent/children/child-primary-4/curriculum-supply/audit') as {packages:{candidate_id:string;state:string}[]}
    expect(audit.packages.filter(p=>p.state==='published')).toHaveLength(1)
    const published=await api('/api/parent/children/child-primary-4/curriculum-supply/'+encodeURIComponent(audit.packages[0].candidate_id)) as {version:number;events:{kind:string}[]}
    expect(published.version).toBe(4)
    for(const kind of ['validate','review','publish'])expect(published.events.filter(event=>event.kind===kind)).toHaveLength(1)
    console.info('PUBLIC_SYNTHETIC_SINGLE_PUBLICATION',JSON.stringify({held_phase:heldPhase,version:published.version,publish_events:published.events.filter(event=>event.kind==='publish').length}))
    expect(writes.filter(p=>p.endsWith('/objective-review'))).toHaveLength(1)
    // Restore/replay the same real published receipt. Read-only display must not
    // label it unapproved or repeat any validate/review/publish transition.
    const beforeResumePosts=[...writes]
    for(let replay=0;replay<2;replay++){
      fireEvent.click(screen.getByRole('button',{name:'刷新已保存送审'}))
      const resume=await screen.findByRole('button',{name:/继续送审：单题原页核对 · 6/})
      await waitFor(()=>expect(resume).not.toBeDisabled());fireEvent.click(resume)
      await waitFor(()=>expect(screen.getAllByRole('status').some(node=>node.textContent?.includes('已有发布记录'))).toBe(true))
      expect(screen.queryByText(/尚未批准或发布/)).toBeNull()
    }
    for(const kind of ['validate','review','publish'])expect(writes.filter(path=>path.endsWith('/'+kind))).toHaveLength(beforeResumePosts.filter(path=>path.endsWith('/'+kind)).length)
    const restored=await api('/api/parent/children/child-primary-4/curriculum-supply/'+encodeURIComponent(audit.packages[0].candidate_id)) as {state:string;version:number;events:{kind:string}[]}
    expect(restored.state).toBe('published');expect(restored.version).toBe(4)
    expect(restored.events.filter(event=>event.kind==='publish')).toHaveLength(1)
    // Real editor/journal + actual HTTP: hold the autosave before it reaches
    // SQLite. The old Q6 identity is still valid, but the screen now shows Q7.
    view.unmount()
    let hold=false,release!:()=>void,putStarted!:()=>void
    const started=new Promise<void>(resolve=>{putStarted=resolve})
    const held=new Promise<void>(resolve=>{release=resolve})
    async function journalApi(path:string,init?:RequestInit):Promise<unknown>{
      if(hold&&init?.method==='PUT'){putStarted();await held}
      return api(path,init)
    }
    const journal=new ParentTextbookDraftJournal(journalApi,props.childId,'capture',undefined,'memory-only')
    await journal.load()
    const pages=(await api(`/v1/parent/knowledge-graph/textbook-capture/${props.childId}/artifacts`) as {items:never[]}).items
    function ActualEditingHost():React.JSX.Element{
      const [state,setState]=useState(journal.state)
      useEffect(()=>journal.subscribe(()=>setState({...journal.state})),[])
      return <ParentPaperObjectiveDraft childId={props.childId} api={journalApi} artifacts={pages}
        value={state.payload?.objective_draft??null} onChange={value=>journal.update({...journal.state.payload!,objective_draft:value})}
        onSave={()=>journal.flush()} blocked={false}/>
    }
    view=render(<ActualEditingHost/>)
    fireEvent.click(screen.getByRole('button',{name:'核对已保存稿的送审条件'}));await screen.findByLabelText('答案如何评分')
    fireEvent.change(screen.getByLabelText('答案如何评分'),{target:{value:'whole_answer_variants'}})
    const selectedRoot=screen.getByLabelText('对应的已审核原题') as HTMLSelectElement
    fireEvent.change(selectedRoot,{target:{value:selectedRoot.options[1].value}})
    fireEvent.change(screen.getByLabelText('对应课标'),{target:{value:'req:12'}})
    fireEvent.change(screen.getByLabelText('原页使用范围'),{target:{value:'public'}});fireEvent.click(screen.getByRole('checkbox'))
    const oldSubmit=screen.getByRole('button',{name:'送审核（不批准、不发布）'})
    expect(oldSubmit).not.toBeDisabled()
    hold=true
    fireEvent.change(screen.getByLabelText('完整题干（待审核）'),{target:{value:'公开合成Q7新编辑：十二加六等于多少？'}})
    await started
    const server=await api(`/v1/parent/knowledge-graph/textbook-capture/${props.childId}/drafts/capture`) as {draft:{payload:{objective_draft:{full_question:string}}}}
    expect(server.draft.payload.objective_draft.full_question).not.toContain('Q7新编辑')
    fireEvent.click(oldSubmit)
    await new Promise(resolve=>setTimeout(resolve,30))
    expect(attemptedPosts.filter(p=>p.endsWith('/objective-review'))).toHaveLength(2)
    expect(screen.getByLabelText('完整题干（待审核）')).toHaveValue('公开合成Q7新编辑：十二加六等于多少？')
    expect(screen.queryByRole('button',{name:'送审核（不批准、不发布）'})).toBeNull()
    hold=false;release();await journal.flush()
    // The same actual parent controls make another unapproved Q7 candidate.
    // Reusing qualified Q6 afterwards must not auto-open this other draft.
    view.unmount()
    async function sendSaved(binding:string):Promise<void>{
      view=render(<StrictMode><ParentPaperFormalReview {...props} draftBinding={binding}/></StrictMode>)
      fireEvent.click(screen.getByRole('button',{name:'核对已保存稿的送审条件'}));await screen.findByLabelText('答案如何评分')
      fireEvent.change(screen.getByLabelText('答案如何评分'),{target:{value:'whole_answer_variants'}})
      const choices=screen.getByLabelText('对应的已审核原题') as HTMLSelectElement
      fireEvent.change(choices,{target:{value:choices.options[1].value}})
      fireEvent.change(screen.getByLabelText('对应课标'),{target:{value:'req:12'}})
      fireEvent.change(screen.getByLabelText('原页使用范围'),{target:{value:'public'}});fireEvent.click(screen.getByRole('checkbox'))
      fireEvent.click(screen.getByRole('button',{name:'送审核（不批准、不发布）'}))
    }
    await sendSaved('public-qualified-root-Q7-new-draft')
    await screen.findByText(/已进入正式审核队列/)
    const withOther=await api('/api/parent/children/child-primary-4/curriculum-supply/audit') as {packages:{candidate_id:string;state:string}[]}
    expect(withOther.packages).toHaveLength(2)
    const other=withOther.packages.find(candidate=>candidate.candidate_id!==audit.packages[0].candidate_id)!
    expect(other.state).toBe('draft');view.unmount()
    const latest=await api(draftPath) as typeof original
    await api(draftPath,{method:'PUT',body:JSON.stringify({expected_revision:latest.draft.revision,expected_generation:latest.draft.storage_generation,
      client_action_id:'public-return-to-qualified-Q6-for-reuse',payload:original.draft.payload})})
    const beforeReuseReads=readPaths.length,beforeReuseTransitions=writes.filter(path=>/\/(validate|review|publish)$/.test(path)).length
    await sendSaved('public-exact-qualified-Q6-reuse')
    await waitFor(()=>expect(screen.getAllByRole('status').some(node=>node.textContent?.includes('沿用已审核的合格原题版本'))).toBe(true))
    await act(async()=>{})
    expect(screen.queryByRole('region',{name:'公开合成孩子的课程供给审核'})).toBeNull()
    expect(screen.queryByRole('button',{name:'独立校验'})).toBeNull()
    expect(readPaths.slice(beforeReuseReads).filter(path=>path.includes('/curriculum-supply/'))).toEqual([])
    expect(writes.filter(path=>/\/(validate|review|publish)$/.test(path))).toHaveLength(beforeReuseTransitions)
    const afterReuse=await api('/api/parent/children/child-primary-4/curriculum-supply/audit') as {packages:{candidate_id:string;state:string;version:number}[]}
    expect(afterReuse.packages).toHaveLength(2)
    expect(afterReuse.packages.find(candidate=>candidate.candidate_id===other.candidate_id)?.state).toBe('draft')
    expect(afterReuse.packages.find(candidate=>candidate.candidate_id===audit.packages[0].candidate_id)?.version).toBe(4)
  }finally{
    vi.useRealTimers();cleanup();if(child&&child.exitCode===null&&child.signalCode===null){const end=once(child,'exit');child.kill('SIGTERM');await end}
    await rm(root,{recursive:true,force:true})
  }
},20000)
