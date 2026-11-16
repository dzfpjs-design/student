import { spawn, type ChildProcess } from 'node:child_process'
import { once } from 'node:events'
import { mkdtemp, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { isAbsolute,join } from 'node:path'
import { createInterface } from 'node:readline'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { expect, it, vi } from 'vitest'
import { TextbookLanguageCandidateReview } from './TextbookLanguageCandidateReview'
import type { TextbookEditingPayload } from './parentTextbookDraftJournal'
import { ApiError } from './api'
import { useState } from 'react'
import { requestParentDraftNavigation } from './parentDraftNavigation'

it('real local HTTP/SQLite paired editing survives a lost save reply, remount and original backend SIGKILL/restart',async()=>{
  const python=process.env.HSC_OBJECTIVE_TEST_PYTHON??join(process.cwd(),process.platform==='win32'?'.venv/Scripts/python.exe':'.venv/bin/python')
  if(!isAbsolute(python))throw Error('explicit absolute test Python required')
  const root=await mkdtemp(join(tmpdir(),'hsc-parent-file-test-'))
  let child:ChildProcess|undefined,base='',image='',lose=false,offline=false,puts=0,lostAction='',lostActionPuts=0
  const created=vi.fn(()=>`blob:public-synthetic-${Math.random()}`),revoked=vi.fn()
  const oldCreate=Object.getOwnPropertyDescriptor(URL,'createObjectURL'),oldRevoke=Object.getOwnPropertyDescriptor(URL,'revokeObjectURL')
  Object.defineProperty(URL,'createObjectURL',{configurable:true,value:created})
  Object.defineProperty(URL,'revokeObjectURL',{configurable:true,value:revoked})
  async function stop(){if(child&&child.exitCode===null&&child.signalCode===null){const ended=once(child,'exit');child.kill('SIGKILL');await ended}child=undefined}
  async function start(){
    child=spawn(python!,['-B','services/local_agent/tests/fixtures/parent_textbook_http.py','--data-root',root],{
      cwd:process.cwd(),stdio:['ignore','pipe','pipe'],env:{PATH:process.env.PATH,PYTHONPATH:join(process.cwd(),'services/local_agent/src'),
        PYTHONDONTWRITEBYTECODE:'1',HSC_DATA_DIR:root,HSC_DATABASE_PATH:join(root,'fixture.db')}})
    let errors='';child.stderr!.on('data',(data:Buffer)=>{errors=(errors+String(data)).slice(-1500)})
    const lines=createInterface({input:child.stdout!})
    const first=await new Promise<string>((yes,no)=>{const timer=setTimeout(()=>no(Error('fixture start timeout '+errors)),10000)
      lines.once('line',line=>{clearTimeout(timer);yes(line)});child!.once('exit',code=>{clearTimeout(timer);no(Error('fixture exited '+code+' '+errors))})})
    const info=JSON.parse(first);base=`http://127.0.0.1:${info.port}`;image=info.synthetic_image;lines.close();child.stdout!.resume()
  }
  async function api(path:string,init?:RequestInit):Promise<unknown>{
    if(offline)throw Error('controlled local network unavailable')
    const headers=new Headers(init?.headers);headers.set('X-HSC-Parent','synthetic-parent')
    if(init?.method==='PUT') {puts++;const action=JSON.parse(String(init.body)).client_action_id;if(action===lostAction)lostActionPuts++}
    const response=await fetch(base+path,{...init,headers})
    if(!response.ok){const failed=await response.json();throw new ApiError('local public HTTP '+response.status,response.status,failed.detail?.code)}
    const data=await response.json()
    if(lose&&init?.method==='PUT'){lose=false;lostAction=JSON.parse(String(init.body)).client_action_id;lostActionPuts=1;throw Error('controlled dropped reply after real commit')}
    return data
  }
  const path='/v1/parent/knowledge-graph/textbook-capture/synthetic-child-4/drafts/capture'
  const readSource=async(childId:string,ref:string,signal:AbortSignal)=>{
    const response=await fetch(base+`/v1/parent/knowledge-graph/textbook-capture/${encodeURIComponent(childId)}/artifacts/${encodeURIComponent(ref)}/source`,{signal,headers:{'X-HSC-Parent':'synthetic-parent'}})
    if(!response.ok)throw Error('public original unavailable')
    expect(response.headers.get('content-type')).toMatch(/^image\/png/)
    return {bytes:await response.arrayBuffer(),mediaType:'image/png' as const}
  }
  function NavigationHost():React.JSX.Element {
    const [owner,setOwner]=useState('synthetic-child-4'),[open,setOpen]=useState(true)
    return <><p>当前合成档案：{owner}</p>
      <button onClick={()=>{if(requestParentDraftNavigation())setOwner(value=>value==='synthetic-child-4'?'synthetic-child-9':'synthetic-child-4')}}>选择另一合成档案</button>
      <button onClick={()=>{if(requestParentDraftNavigation())setOpen(false)}}>离开合成家长页</button>
      {open&&<TextbookLanguageCandidateReview recoveryFactory={()=>'memory-only'} api={api} childId={owner} captureOnly readSource={readSource}/>}</>
  }
  const mount=()=>render(<NavigationHost/>)
  try{
    await start()
    const refs:string[]=[]
    for(const title of ['公开合成试卷','公开合成官方答案']){
      const result=await api('/v1/parent/knowledge-graph/textbook-capture/synthetic-child-4/file-ocr',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({image_base64:image,page_context:{title,page_number:1},client_action_id:'public-objective-'+refs.length})}) as {artifact:{capture_ref:string}}
      refs.push(result.artifact.capture_ref)
    }
    let view=mount();fireEvent.click(await screen.findByRole('button',{name:'整理单题审核草稿'}))
    await within(screen.getByLabelText('试卷原页')).findByRole('option',{name:/公开合成试卷/})
    fireEvent.change(screen.getByLabelText('试卷原页'),{target:{value:refs[0]}})
    fireEvent.change(screen.getByLabelText('答案原页'),{target:{value:refs[1]}})
    fireEvent.change(screen.getByLabelText('题号'),{target:{value:'6'}})
    fireEvent.change(screen.getByLabelText('完整题干（待审核）'),{target:{value:'合成完整原题：十二加五等于多少？'}})
    fireEvent.change(screen.getByLabelText('官方答案（待审核，每行一项）'),{target:{value:'17'}})
    await screen.findByAltText('试卷原页');await screen.findByAltText('答案原页')
    await waitFor(async()=>expect(((await api(path))as{draft:{payload:TextbookEditingPayload}}).draft.payload.objective_draft?.official_answers).toEqual(['17']))
    lose=true;fireEvent.change(screen.getByLabelText('完整题干（待审核）'),{target:{value:'合成完整原题：十二加五等于多少？保留单位。'}})
    await waitFor(()=>expect(lostAction).not.toBe(''))
    await waitFor(async()=>expect(((await api(path))as{draft:{payload:TextbookEditingPayload}}).draft.payload.objective_draft?.full_question).toContain('保留单位'))
    fireEvent.click(screen.getByRole('button',{name:'保存单题审核草稿'}));await screen.findByText('单题审核草稿已保存；尚未批准或生成复测。')
    expect(lostActionPuts).toBe(1)
    const saved=await api(path);view.unmount();view=mount()
    expect(await screen.findByLabelText('完整题干（待审核）')).toHaveValue('合成完整原题：十二加五等于多少？保留单位。')
    view.unmount();await stop();await start();view=mount()
    expect(await screen.findByLabelText('完整题干（待审核）')).toHaveValue('合成完整原题：十二加五等于多少？保留单位。')
    expect(await api(path)).toEqual(saved);expect(puts).toBeGreaterThan(0)
    await screen.findByAltText('试卷原页');expect(revoked).toHaveBeenCalled()
    // Unreviewed raw input persists even when it cannot form an official candidate.
    for(const original of ['测'.repeat(201),Array(25).fill('17').join('\n')]){
      await waitFor(()=>expect(screen.getByLabelText('官方答案（待审核，每行一项）')).not.toBeDisabled())
      fireEvent.change(screen.getByLabelText('官方答案（待审核，每行一项）'),{target:{value:original}})
      await screen.findByText('原文不会截断，仍可保存和重开；当前规范候选待修正，不能据此发布题源。')
      expect(screen.getByLabelText('官方答案（待审核，每行一项）')).not.toBeDisabled()
      await waitFor(async()=>expect(((await api(path))as{draft:{payload:TextbookEditingPayload}}).draft.payload.objective_draft?.raw_editing?.official_answers_text).toBe(original))
      expect(((await api(path))as{draft:{payload:TextbookEditingPayload}}).draft.payload.objective_draft?.official_answers).toEqual([])
      view.unmount();view=mount();expect(await screen.findByLabelText('官方答案（待审核，每行一项）')).toHaveValue(original)
      view.unmount();await stop();await start();view=mount()
      expect(await screen.findByLabelText('官方答案（待审核，每行一项）')).toHaveValue(original)
    }
    await waitFor(()=>expect(screen.getByLabelText('官方答案（待审核，每行一项）')).not.toBeDisabled())
    fireEvent.change(screen.getByLabelText('完整题干（待审核）'),{target:{value:'𝑥'.repeat(601)}})
    fireEvent.change(screen.getByLabelText('官方答案（待审核，每行一项）'),{target:{value:'😀'.repeat(200)}})
    await waitFor(async()=>expect(((await api(path))as{draft:{payload:TextbookEditingPayload}}).draft.payload.objective_draft?.official_answers).toEqual(['😀'.repeat(200)]))
    expect(((await api(path))as{draft:{payload:TextbookEditingPayload}}).draft.payload.objective_draft?.full_question).toBe('𝑥'.repeat(601))
    fireEvent.change(screen.getByLabelText('官方答案（待审核，每行一项）'),{target:{value:'测'.repeat(200)}})
    await waitFor(async()=>expect(((await api(path))as{draft:{payload:TextbookEditingPayload}}).draft.payload.objective_draft?.official_answers).toEqual(['测'.repeat(200)]))
    offline=true
    fireEvent.change(screen.getByLabelText('完整题干（待审核）'),{target:{value:'离线期间保留的公开题干'}})
    await screen.findByRole('button',{name:'重试草稿保存或恢复'})
    expect(screen.getByLabelText('完整题干（待审核）')).not.toBeDisabled()
    expect(screen.getByAltText('试卷原页')).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('完整题干（待审核）'),{target:{value:'离线期间又修正的完整公开题干'}})
    fireEvent.click(screen.getByRole('button',{name:'选择另一合成档案'}))
    expect(screen.getByText('当前合成档案：synthetic-child-4')).toBeInTheDocument()
    expect(screen.getByLabelText('完整题干（待审核）')).toHaveValue('离线期间又修正的完整公开题干')
    fireEvent.click(screen.getByRole('button',{name:'离开合成家长页'}))
    expect(screen.getByLabelText('完整题干（待审核）')).toHaveValue('离线期间又修正的完整公开题干')
    offline=false;fireEvent.click(screen.getByRole('button',{name:'重试草稿保存或恢复'}))
    await waitFor(async()=>expect(((await api(path))as{draft:{payload:TextbookEditingPayload}}).draft.payload.objective_draft?.full_question).toBe('离线期间又修正的完整公开题干'))
    expect(screen.getByText('当前合成档案：synthetic-child-4')).toBeInTheDocument()
    const putsBeforeNewChild=puts
    fireEvent.click(screen.getByRole('button',{name:'选择另一合成档案'}))
    await screen.findByText('当前合成档案：synthetic-child-9')
    await screen.findByText('草稿已从本机恢复。')
    const untouchedChild=await api('/v1/parent/knowledge-graph/textbook-capture/synthetic-child-9/drafts/capture')as{draft:{revision:number;payload:unknown}}
    expect(untouchedChild.draft.revision).toBe(0);expect(untouchedChild.draft.payload).toBeNull();expect(puts).toBe(putsBeforeNewChild)
    expect(screen.queryByLabelText('完整题干（待审核）')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button',{name:'选择另一合成档案'}))
    expect(await screen.findByLabelText('完整题干（待审核）')).toHaveValue('离线期间又修正的完整公开题干')
    // A real source review changes its version. Reject the stale save, then bind
    // the new source explicitly; no unknown pending request may be discarded.
    await api(`/v1/parent/knowledge-graph/textbook-capture/synthetic-child-4/artifacts/${refs[0]}/review`,{
      method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({extracted_text:'公开原页改正后的完整 OCR',client_action_id:'objective-current-source-review'})})
    fireEvent.change(screen.getByLabelText('完整题干（待审核）'),{target:{value:'原页变化时不能静默保存的公开题干'}})
    await screen.findByText('本次保存被拒，已保存版本未覆盖。文字仍在本窗口；请刷新并核对原页或修正输入，再保存。')
    expect(screen.getByLabelText('完整题干（待审核）')).not.toBeDisabled()
    fireEvent.click(screen.getByRole('button',{name:'刷新资料页'}))
    await screen.findByText('原页版本已变化，已保存文字仍保留。请重新选择当前原页核对。')
    expect(screen.getByRole('button',{name:'保存单题审核草稿'})).toBeDisabled()
    fireEvent.click(screen.getByRole('button',{name:'使用当前原页版本继续核对'}))
    fireEvent.click(screen.getByRole('button',{name:'保存单题审核草稿'}))
    await screen.findByText('单题审核草稿已保存；尚未批准或生成复测。')
    await waitFor(async()=>expect(((await api(path))as{draft:{payload:TextbookEditingPayload}}).draft.payload.objective_draft?.full_question).toBe('原页变化时不能静默保存的公开题干'))
  }finally{
    cleanup();await stop();await rm(root,{recursive:true,force:true})
    if(oldCreate)Object.defineProperty(URL,'createObjectURL',oldCreate);else delete (URL as unknown as Record<string,unknown>).createObjectURL
    if(oldRevoke)Object.defineProperty(URL,'revokeObjectURL',oldRevoke);else delete (URL as unknown as Record<string,unknown>).revokeObjectURL
  }
},20000)
