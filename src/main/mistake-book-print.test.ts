import { EventEmitter } from 'node:events'
import { mkdtemp, readFile, readdir, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import type { BrowserWindow, IpcMainInvokeEvent } from 'electron'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const native = vi.hoisted(() => ({ windows: [] as any[], sessions: [] as any[], save: vi.fn(), printing: vi.fn(), pdf: vi.fn(), ready: vi.fn() }))
vi.mock('electron', () => ({
  BrowserWindow: class extends EventEmitter {
    destroyed = false
    webContents = Object.assign(new EventEmitter(), {
      setWindowOpenHandler: vi.fn(),
      executeJavaScript: vi.fn(),
      executeJavaScriptInIsolatedWorld: native.ready,
      print: native.printing,
      printToPDF: native.pdf,
    })
    loadURL = vi.fn(async () => undefined)
    show = vi.fn()
    focus = vi.fn()
    isDestroyed = (): boolean => this.destroyed
    destroy = vi.fn(() => { this.destroyed = true;this.emit('closed') })
    constructor(public options: unknown) { super();native.windows.push(this) }
  },
  dialog: { showSaveDialog: native.save },
  session: { fromPartition: vi.fn((partition: string, options: unknown) => {
    const value = { partition, options, setPermissionCheckHandler: vi.fn(), setPermissionRequestHandler: vi.fn(), webRequest: { onBeforeRequest: vi.fn() } }
    native.sessions.push(value);return value
  }) },
}))

import { createMistakeBookPrintHandler, isolatedMistakePrintHtml, validateMistakePrintRequest, writeMistakePdf } from './mistake-book-print'

const html = '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><style>@page{size:A4;margin:14mm}p{white-space:pre-wrap}</style></head><body><h1>数学错题本</h1><p>2 + 3 = ?</p></body></html>'
const request = { html, title: '数学错题本', action: 'print' as const }
function setup(observe?: Parameters<typeof createMistakeBookPrintHandler>[1]) {
  const webContents = { mainFrame: {}, isDestroyed: vi.fn(() => false), getURL: vi.fn(() => 'file:///app/index.html') }
  const owner = { isDestroyed: vi.fn(() => false), webContents } as unknown as BrowserWindow
  const event = { sender: webContents, senderFrame: webContents.mainFrame } as unknown as IpcMainInvokeEvent
  return { owner, event, handler: createMistakeBookPrintHandler(() => owner, observe) }
}

beforeEach(() => {
  vi.clearAllMocks();native.windows.length = 0;native.sessions.length = 0
  native.ready.mockResolvedValue(true)
  native.printing.mockImplementation((_options, callback) => callback(true, ''))
  native.pdf.mockResolvedValue(Buffer.from('%PDF-1.7 synthetic'))
  native.save.mockResolvedValue({ canceled: true })
})

describe('mistake print static input isolation', () => {
  it('accepts printable Unicode and inline raster images, then installs CSP before document markup', () => {
    const image = html.replace('</body>', '<img src="data:image/png;base64,aGVsbG8="></body>')
    expect(validateMistakePrintRequest({ ...request, html: image }).html).toBe(image)
    expect(isolatedMistakePrintHtml(request)).toContain('<head><meta http-equiv="Content-Security-Policy"')
  })
  it('prints programming exercises as escaped text rather than rejecting resource-like words', () => {
    const question = '<p>请解释 @import、url()、expression()、onload=、href="https://example.test" 和 xlink:href="x"。&lt;script&gt; 是什么？</p><p title="解释 onload= 事件">代码阅读</p>'
    const document = html.replace('</body>', `${question}</body>`)
    expect(validateMistakePrintRequest({ ...request, html: document }).html).toBe(document)
  })
  it.each([
    '<script>alert(1)</script>', '<img src="https://example.test/family.jpg">', '<img src="file:///private/data.jpg">',
    '<img src="data:image/svg+xml;base64,abcd">', '<img src="&#104;ttps://example.test">',
    '<iframe srcdoc="private"></iframe>', '<p onclick="alert(1)">click</p>', '<meta http-equiv="refresh" content="0;url=https://example.test">',
    '<style>@import "https://example.test/a.css";</style>', '<style>p{background:url(https://example.test)}</style>',
    '<p style="background:url(https://example.test)">test</p>', '<p title="quoted > content" onclick="alert(1)">test</p>',
  ])('rejects active or remotely loaded markup: %s', extra => {
    expect(() => validateMistakePrintRequest({ ...request, html: html.replace('</body>', `${extra}</body>`) })).toThrow()
  })
  it('rejects unbounded and malformed requests before opening a window', async () => {
    const { handler, event } = setup()
    expect((await handler(event, { ...request, html: 'x'.repeat(32 * 1024 * 1024 + 1) })).status).toBe('failed')
    expect((await handler(event, { ...request, action: 'silent' })).status).toBe('failed')
    expect(native.windows).toHaveLength(0)
  })
})

describe('native print and PDF lifecycle', () => {
  it('admits only the current primary main frame', async () => {
    const { handler, event, owner } = setup()
    expect((await handler({ ...event, senderFrame: {} } as IpcMainInvokeEvent, request)).status).toBe('failed')
    expect((await handler({ ...event, sender: {} } as IpcMainInvokeEvent, request)).status).toBe('failed')
    vi.mocked(owner.isDestroyed).mockReturnValue(true)
    expect((await handler(event, request)).status).toBe('failed')
    expect(native.windows).toHaveLength(0)
  })
  it('prints to the system default from an isolated window without opening a dialog', async () => {
    const { handler, event } = setup()
    expect(await handler(event, request)).toEqual({ status: 'submitted' })
    const window = native.windows[0]
    expect(window.options.webPreferences).toMatchObject({ sandbox: true, nodeIntegration: false, javascript: false, contextIsolation: true })
    expect(window.webContents.executeJavaScript).not.toHaveBeenCalled()
    expect(native.ready).toHaveBeenCalledWith(1, [{ code: expect.stringContaining('document.images') }])
    expect(native.sessions[0].partition).not.toMatch(/^persist:/)
    const block = native.sessions[0].webRequest.onBeforeRequest.mock.calls[0][0], respond = vi.fn()
    block({ url: 'https://example.test/image' }, respond);expect(respond).toHaveBeenLastCalledWith({ cancel: true })
    block({ url: 'file:///private/family' }, respond);expect(respond).toHaveBeenLastCalledWith({ cancel: true })
    block({ url: 'data:image/png;base64,AAA=' }, respond);expect(respond).toHaveBeenLastCalledWith({ cancel: false })
    const navigation = { preventDefault: vi.fn() };window.webContents.emit('will-navigate', navigation)
    expect(navigation.preventDefault).toHaveBeenCalledOnce()
    expect(native.printing.mock.calls[0][0]).toMatchObject({ silent: true, pageSize: 'A4' })
    expect(native.printing.mock.calls[0][0]).not.toHaveProperty('deviceName')
    expect(window.show).not.toHaveBeenCalled();expect(window.focus).not.toHaveBeenCalled()
    expect(window.destroy).toHaveBeenCalledOnce()
  })
  it('reports cancellation separately from printer errors and always destroys the print window', async () => {
    const { handler, event } = setup()
    native.printing.mockImplementationOnce((_options, callback) => callback(false, 'Print job canceled'))
    expect(await handler(event, request)).toEqual({ status: 'cancelled' })
    native.printing.mockImplementationOnce((_options, callback) => callback(false, 'Printer unavailable'))
    expect((await handler(event, request)).status).toBe('failed')
    expect(native.windows.every(window => window.destroyed)).toBe(true)
  })
  it('does not report incomplete pictures as printable', async () => {
    native.ready.mockResolvedValue(false)
    const { handler, event } = setup()
    expect((await handler(event, request)).status).toBe('failed')
    expect(native.printing).not.toHaveBeenCalled();expect(native.windows[0].destroyed).toBe(true)
  })
  it('reports scope, layout and spool failures without recording document or printer text', async () => {
    const observe = vi.fn()
    const { handler, event } = setup(observe)
    await handler({ ...event, senderFrame: {} } as IpcMainInvokeEvent, request)
    expect(observe).toHaveBeenLastCalledWith(expect.objectContaining({ print_stage: 'admission', print_failure: 'owner' }), expect.any(String))
    native.ready.mockRejectedValueOnce(Error('secret content from injected mapping'))
    await handler(event, request)
    expect(observe).toHaveBeenLastCalledWith(expect.objectContaining({ print_stage: 'layout_readiness', print_failure: 'readiness_error' }), expect.any(String))
    expect(native.printing).not.toHaveBeenCalled()
    native.printing.mockImplementationOnce((_options, callback) => callback(false, 'Invalid printer settings'))
    await handler(event, request)
    expect(observe).toHaveBeenLastCalledWith(expect.objectContaining({ print_stage: 'queue_submit', print_failure: 'printer_settings' }), expect.any(String))
    expect(JSON.stringify(observe.mock.calls)).not.toMatch(/数学错题本|secret content|Invalid printer settings/)
  })
  it('reports an unconfirmed submission after a renderer crash and releases the busy state', async () => {
    const { handler, event } = setup()
    native.printing.mockImplementationOnce(() => native.windows[0].webContents.emit('render-process-gone'))
    expect((await handler(event, request)).status).toBe('unconfirmed')
    expect(native.windows[0].destroyed).toBe(true)
    expect((await handler(event, request)).status).toBe('submitted')
  })
  it('times out an unanswered submission without retrying and disregards a late callback', async () => {
    vi.useFakeTimers()
    try {
      const { handler, event } = setup()
      let reply: ((success: boolean, reason: string) => void) | undefined
      native.printing.mockImplementationOnce((_options, callback) => { reply = callback })
      const first = handler(event, request)
      await vi.advanceTimersByTimeAsync(1)
      expect(native.printing).toHaveBeenCalledOnce()
      expect((await handler(event, request)).status).toBe('failed')
      await vi.advanceTimersByTimeAsync(30_000)
      expect(await first).toEqual({ status: 'unconfirmed' })
      expect(native.windows[0].destroyed).toBe(true)
      reply?.(true, '')
      expect(native.printing).toHaveBeenCalledOnce()
      expect(await handler(event, request)).toEqual({ status: 'submitted' })
    } finally { vi.useRealTimers() }
  })
  it('does not generate a PDF after save cancellation or navigation away', async () => {
    const { handler, event, owner } = setup()
    expect(await handler(event, { ...request, action: 'pdf' })).toEqual({ status: 'cancelled' })
    native.save.mockImplementationOnce(async () => {
      vi.mocked(owner.webContents.getURL).mockReturnValue('file:///changed.html')
      return { canceled: false, filePath: '/unused.pdf' }
    })
    expect(await handler(event, { ...request, action: 'pdf' })).toEqual({ status: 'cancelled' })
    expect(native.windows).toHaveLength(0);expect(native.pdf).not.toHaveBeenCalled()
  })
  it('prevents overlapping print jobs, then allows retry after cancellation', async () => {
    const { handler, event } = setup()
    let release!: (value: { canceled: boolean }) => void
    native.save.mockReturnValueOnce(new Promise(resolve => { release = resolve }))
    const pending = handler(event, { ...request, action: 'pdf' })
    expect((await handler(event, request)).status).toBe('failed')
    release({ canceled: true });expect((await pending).status).toBe('cancelled')
    expect((await handler(event, request)).status).toBe('submitted')
  })
  it('writes the actual returned PDF before acknowledging saved, without returning the path', async () => {
    const root = await mkdtemp(join(tmpdir(), 'hsc-print-test-'))
    try {
      const filePath = join(root, '错题.pdf')
      native.save.mockResolvedValue({ canceled: false, filePath })
      const { handler, event } = setup()
      expect(await handler(event, { ...request, action: 'pdf' })).toEqual({ status: 'saved' })
      expect(await readFile(filePath, 'utf8')).toBe('%PDF-1.7 synthetic')
      expect(await readdir(root)).toEqual(['错题.pdf'])
      expect(native.pdf.mock.calls[0][0]).toMatchObject({ pageSize: 'A4', preferCSSPageSize: true })
      expect(native.windows[0].destroyed).toBe(true)
    } finally { await rm(root, { recursive: true, force: true }) }
  })
  it('preserves an existing selected file when PDF generation fails', async () => {
    const root = await mkdtemp(join(tmpdir(), 'hsc-print-failure-'))
    try {
      const filePath = join(root, 'existing.pdf');await writeFile(filePath, 'old PDF')
      native.save.mockResolvedValue({ canceled: false, filePath });native.pdf.mockRejectedValue(Error('failure'))
      const { handler, event } = setup()
      expect((await handler(event, { ...request, action: 'pdf' })).status).toBe('failed')
      expect(await readFile(filePath, 'utf8')).toBe('old PDF')
      expect(native.windows[0].destroyed).toBe(true)
    } finally { await rm(root, { recursive: true, force: true }) }
  })
  it('atomically replaces an explicitly chosen existing PDF', async () => {
    const root = await mkdtemp(join(tmpdir(), 'hsc-print-replace-'))
    try {
      const path = join(root, 'chosen.pdf');await writeFile(path, 'old')
      await writeMistakePdf(path, Buffer.from('new PDF'))
      expect(await readFile(path, 'utf8')).toBe('new PDF');expect(await readdir(root)).toEqual(['chosen.pdf'])
    } finally { await rm(root, { recursive: true, force: true }) }
  })
})
