import { spawn, type ChildProcess } from 'node:child_process'
import { once } from 'node:events'
import { mkdtemp, rm } from 'node:fs/promises'
import { existsSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { createInterface } from 'node:readline'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { expect, it, vi } from 'vitest'
import { TextbookLanguageCandidateReview } from './TextbookLanguageCandidateReview'

it('mounts file capture through real TCP, encrypted storage, review and import across two service restarts', async () => {
  const root = await mkdtemp(join(tmpdir(),'hsc-parent-file-test-'))
  let child: ChildProcess | undefined, base = '', raw = ''
  const stop = async (): Promise<void> => {
    if (!child) return
    if (child.exitCode !== null || child.signalCode !== null) { child = undefined; return }
    const ended = once(child,'exit')
    const owned = child
    const timer = setTimeout(() => { owned.kill('SIGKILL') },3_000)
    try { owned.kill('SIGTERM'); await ended } finally { clearTimeout(timer); child = undefined }
  }
  const start = async (): Promise<void> => {
    const venvPython = join(process.cwd(),'.venv',process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python')
    child = spawn(existsSync(venvPython) ? venvPython : process.platform === 'win32' ? 'python' : 'python3',
      ['services/local_agent/tests/fixtures/parent_textbook_http.py','--data-root',root],{
      cwd:process.cwd(),stdio:['ignore','pipe','pipe'],
      env:{ PATH:process.env.PATH, HOME:process.env.HOME, PYTHONPATH:join(process.cwd(),'services/local_agent/src'), PYTHONDONTWRITEBYTECODE:'1' },
    })
    let errors = ''
    child.stderr!.on('data',(data: Buffer) => { errors = (errors + String(data)).slice(-1200) })
    const lines = createInterface({ input:child.stdout! })
    const first = await new Promise<string>((resolve,reject) => {
      const timeout = setTimeout(() => reject(new Error('fixture startup timeout: '+errors)),10_000)
      lines.once('line',(line) => { clearTimeout(timeout); resolve(line) })
      child!.once('exit',(code) => { clearTimeout(timeout); reject(new Error('fixture exited '+code+': '+errors)) })
    })
    lines.close()
    const info = JSON.parse(first) as { port: number, synthetic_image: string }
    base = `http://127.0.0.1:${info.port}`; raw = info.synthetic_image
    for (let attempt=0;attempt<40;attempt++) {
      try { if ((await fetch(base+'/openapi.json')).ok) return } catch { /* socket is bound before service startup */ }
      await new Promise((resolve) => setTimeout(resolve,50))
    }
    throw new Error('fixture did not become ready')
  }
  const api = async (path: string, init?: RequestInit): Promise<unknown> => {
    const response = await fetch(base+path,{ ...init,headers:{ 'X-HSC-Parent':'synthetic-parent',...init?.headers } })
    const value = await response.json()
    if (!response.ok) throw new Error(JSON.stringify(value))
    return value
  }
  try {
    await start()
    const first = render(<TextbookLanguageCandidateReview recoveryFactory={()=>'memory-only'} api={api} childId="synthetic-child-4" captureOnly />)
    await waitFor(() => expect(screen.getByLabelText('教材页标题')).toBeEnabled())
    fireEvent.change(screen.getByLabelText('教材页标题'),{ target:{ value:'合成文件教材' } })
    const input = document.querySelector<HTMLInputElement>('input[type=file]')!
    fireEvent.change(input,{ target:{ files:[new File([Uint8Array.from(Buffer.from(raw,'base64'))],'synthetic-page.png',{ type:'image/png' })] } })
    await screen.findByAltText('待识别的作业图片')
    fireEvent.click(screen.getByRole('button',{ name:'确认并识别' }))
    await screen.findByRole('textbox',{ name:'校对后的教材 OCR 文字（第 1 项）' })
    const items = await api('/v1/parent/knowledge-graph/textbook-capture/synthetic-child-4/artifacts') as { items: { capture_ref:string }[] }
    const ref = items.items[0].capture_ref
    first.unmount(); await stop(); await start()
    render(<TextbookLanguageCandidateReview recoveryFactory={()=>'memory-only'} api={api} childId="synthetic-child-4" captureOnly />)
    const corrected = await screen.findByRole('textbox',{ name:'校对后的教材 OCR 文字（第 1 项）' })
    fireEvent.change(corrected,{ target:{ value:'Words: apple banana' } })
    fireEvent.click(screen.getByRole('button',{ name:'校对并带入表单' }))
    await waitFor(() => expect(screen.getByLabelText('教材页 OCR 文字')).toHaveValue('Words: apple banana'))
    const save = screen.getByRole('button',{ name:'保存本机 OCR 页' })
    await waitFor(() => expect(save).toBeEnabled())
    fireEvent.click(save)
    await screen.findByText('本机 OCR 页面及原页来源已保存。学科知识尚未核验，不能直接作为教学或掌握度证据。')
    cleanup(); await stop(); await start()
    await act(async () => { render(<TextbookLanguageCandidateReview recoveryFactory={()=>'memory-only'} api={api} childId="synthetic-child-4" captureOnly />) })
    await screen.findByText('已保存 1 页资料。这些页面的学科知识尚未核验；保存页面不代表已形成可用的教学知识。')
    const source = await fetch(base+`/v1/parent/knowledge-graph/textbook-capture/synthetic-child-4/artifacts/${ref}/source`,{ headers:{ 'X-HSC-Parent':'synthetic-parent' } })
    expect(Buffer.from(await source.arrayBuffer()).toString('base64')).toBe(raw)
    expect(source.headers.get('cache-control')).toBe('no-store')
    const pending = await api('/v1/parent/knowledge-graph/textbook-capture/synthetic-child-4/artifacts') as { items:unknown[] }
    expect(pending.items).toEqual([])
  } finally {
    cleanup(); await stop(); await rm(root,{ recursive:true,force:true }); vi.restoreAllMocks()
  }
},30_000)
