import { spawn, type ChildProcess } from 'node:child_process'
import { once } from 'node:events'
import { mkdtemp, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { createInterface } from 'node:readline'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { expect, it } from 'vitest'
import { TextbookLanguageCandidateReview } from './TextbookLanguageCandidateReview'

it('recovers unsubmitted parent editing after SIGKILL, then reconciles a lost import reply without another POST', async () => {
  const root = await mkdtemp(join(tmpdir(),'hsc-parent-file-test-'))
  let child: ChildProcess | undefined, base = '', raw = '', posts = 0, loseReply = false, draftPuts = 0
  const stop = async (): Promise<void> => {
    if (!child || child.exitCode !== null || child.signalCode !== null) return
    const ended = once(child,'exit'); child.kill('SIGKILL'); await ended; child = undefined
  }
  const start = async (): Promise<void> => {
    child = spawn(join(process.cwd(),'.venv/bin/python'),['services/local_agent/tests/fixtures/parent_textbook_http.py','--data-root',root],{
      cwd:process.cwd(),stdio:['ignore','pipe','pipe'],
      env:{PATH:process.env.PATH,HOME:process.env.HOME,PYTHONPATH:join(process.cwd(),'services/local_agent/src'),PYTHONDONTWRITEBYTECODE:'1'},
    })
    let errors = ''; child.stderr!.on('data',(data: Buffer) => {errors=(errors+String(data)).slice(-1200)})
    const lines = createInterface({input:child.stdout!})
    const first = await new Promise<string>((resolve,reject) => {
      const timeout = setTimeout(() => reject(new Error('fixture startup timeout: '+errors)),10000)
      lines.once('line',line => {clearTimeout(timeout);resolve(line)})
      child!.once('exit',code => {clearTimeout(timeout);reject(new Error('fixture exited '+code+': '+errors))})
    })
    lines.close();child.stdout!.resume(); const info = JSON.parse(first); base=`http://127.0.0.1:${info.port}`;raw=info.synthetic_image
    for(let n=0;n<40;n++) {
      try {if((await fetch(base+'/openapi.json')).ok) return} catch { /* socket readiness is not service readiness */ }
      await new Promise(resolve => setTimeout(resolve,50))
    }
    throw new Error('service not ready')
  }
  const api = async (path: string, init?: RequestInit): Promise<unknown> => {
    if(path.includes('/drafts/')&&init?.method==='PUT')draftPuts++
    const response = await fetch(base+path,{...init,headers:{'X-HSC-Parent':'synthetic-parent',...init?.headers}})
    const value = await response.json(); if(!response.ok) throw new Error(JSON.stringify(value))
    if(path.endsWith('/textbook-pages') && init?.method === 'POST') {
      posts++; if(loseReply) {loseReply=false;throw new Error('intentional lost import reply')}
    }
    return value
  }
  const mount = (): ReturnType<typeof render> => render(<TextbookLanguageCandidateReview recoveryFactory={()=>'memory-only'} api={api} childId="synthetic-child-4" captureOnly />)
  const saved = async (): Promise<void> => { await screen.findByText('草稿已保存到本机。') }
  try {
    await start(); const first = mount()
    await waitFor(() => expect(screen.getByLabelText('教材页标题')).toBeEnabled())
    fireEvent.change(screen.getByLabelText('教材页标题'),{target:{value:'家长尚未提交的四年级教材'}})
    fireEvent.change(screen.getByLabelText('教材版本'),{target:{value:'家长校对版本'}})
    fireEvent.change(screen.getByLabelText('教材页 OCR 文字'),{target:{value:'尚未导入的校对内容'}})
    await saved(); first.unmount(); await stop(); await start(); const second=mount()
    await waitFor(() => expect(screen.getByLabelText('教材页标题')).toHaveValue('家长尚未提交的四年级教材'))
    expect(screen.getByLabelText('教材版本')).toHaveValue('家长校对版本')
    expect(screen.getByLabelText('教材页 OCR 文字')).toHaveValue('尚未导入的校对内容')
    expect(posts).toBe(0);expect(localStorage.length).toBe(0)
    const image = new File([Uint8Array.from(Buffer.from(raw,'base64'))],'synthetic.png',{type:'image/png'})
    fireEvent.change(document.querySelector<HTMLInputElement>('input[type=file]')!,{target:{files:[image]}})
    await screen.findByAltText('待识别的作业图片'); fireEvent.click(screen.getByRole('button',{name:'确认并识别'}))
    const review = await screen.findByRole('textbox',{name:'校对后的教材 OCR 文字（第 1 项）'})
    fireEvent.change(review,{target:{value:'Words: apple banana'}}); await saved()
    second.unmount();await stop();await start();const third=mount()
    await waitFor(() => expect(screen.getByLabelText('校对后的教材 OCR 文字（第 1 项）')).toHaveValue('Words: apple banana'))
    expect(screen.getByLabelText('教材版本')).toHaveValue('家长校对版本')
    await waitFor(() => expect(screen.getByRole('button',{name:'校对并带入表单'})).toBeEnabled())
    fireEvent.click(screen.getByRole('button',{name:'校对并带入表单'}))
    await waitFor(() => expect(screen.getByLabelText('教材页 OCR 文字')).toHaveValue('Words: apple banana'));await saved()
    await waitFor(() => expect(screen.getByRole('button',{name:'保存本机 OCR 页'})).toBeEnabled())
    loseReply=true;fireEvent.click(screen.getByRole('button',{name:'保存本机 OCR 页'}))
    await screen.findByText(/页面提交或草稿收尾尚未核对/)
    third.unmount();await stop();await start();mount()
    await screen.findByRole('button',{name:'核对上次提交'})
    fireEvent.click(screen.getByRole('button',{name:'核对上次提交'}))
    await screen.findByText('上次页面已真实保存，未重复导入；草稿收尾已核对。')
    expect(posts).toBe(1);expect(screen.getByLabelText('教材页 OCR 文字')).toHaveValue('')
    const pages = await api('/v1/parent/knowledge-graph/textbook-pages') as {items:unknown[]}
    expect(pages.items).toHaveLength(1)
    const draftPath='/v1/parent/knowledge-graph/textbook-capture/synthetic-child-4/drafts/capture'
    const draft = await api(draftPath) as {draft:{revision:number;storage_generation:string;payload:{action_ids:unknown;capture_ref:string;page:Record<string,unknown>}}}
    expect(draft.draft.payload.capture_ref).toBe('');expect(draft.draft.payload.action_ids).toEqual({})
    const otherWindowSave = async (title:string): Promise<void> => {
      const current=await api(draftPath) as typeof draft
      await api(draftPath,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({
        expected_revision:current.draft.revision,expected_generation:current.draft.storage_generation,
        client_action_id:`synthetic-conflict-${crypto.randomUUID()}`,
        payload:{...current.draft.payload,page:{...current.draft.payload.page,title,ocr_text:'另一窗口已保存的文字'}},
      })})
    }
    await saved();await otherWindowSave('另一窗口的教材')
    fireEvent.change(screen.getByLabelText('教材页标题'),{target:{value:'本窗口未保存的教材'}})
    await screen.findByText(/另一个窗口或删除操作已改变草稿/)
    expect(screen.getByRole('button',{name:'保存本机 OCR 页'})).toBeDisabled()
    fireEvent.click(screen.getByRole('button',{name:'对照两份草稿'}))
    await screen.findByLabelText('本机已保存内容的 OCR 文字')
    expect(screen.getByLabelText('本窗口未保存内容').textContent).toContain('本窗口未保存的教材')
    expect(screen.getByLabelText('本机已保存内容').textContent).toContain('另一窗口的教材')
    const consentName='我已核对，放弃本窗口未保存内容，采用已保存版本'
    expect(screen.getByRole('button',{name:'采用已保存版本'})).toBeDisabled()
    await otherWindowSave('比较后再次改变的教材')
    fireEvent.click(screen.getByRole('checkbox',{name:consentName}))
    fireEvent.click(screen.getByRole('button',{name:'采用已保存版本'}))
    await screen.findByText(/版本或连接已变化/)
    expect(screen.getByLabelText('教材页标题')).toHaveValue('本窗口未保存的教材')
    fireEvent.click(screen.getByRole('button',{name:'对照两份草稿'}))
    await screen.findByLabelText('本机已保存内容的 OCR 文字')
    expect(screen.getByLabelText('本机已保存内容').textContent).toContain('比较后再次改变的教材')
    const beforeAdopt=await api(draftPath) as typeof draft
    const putsBeforeAdopt=draftPuts
    fireEvent.click(screen.getByRole('checkbox',{name:consentName}))
    fireEvent.click(screen.getByRole('button',{name:'采用已保存版本'}))
    await waitFor(()=>expect(screen.getByLabelText('教材页标题')).toHaveValue('比较后再次改变的教材'))
    await screen.findByText('已采用核对过的本机草稿，没有重新提交或覆盖它。')
    expect(draftPuts).toBe(putsBeforeAdopt)
    expect((await api(draftPath) as typeof draft).draft.revision).toBe(beforeAdopt.draft.revision)
    cleanup();await stop();await start();mount()
    await waitFor(()=>expect(screen.getByLabelText('教材页标题')).toHaveValue('比较后再次改变的教材'))
    expect(posts).toBe(1);expect(localStorage.length).toBe(0)
    expect((await api('/v1/parent/knowledge-graph/textbook-pages') as {items:unknown[]}).items).toHaveLength(1)
  } finally {cleanup();await stop();await rm(root,{recursive:true,force:true})}
},40000)
