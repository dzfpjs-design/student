import { BrowserWindow, dialog, session, type IpcMainInvokeEvent } from 'electron'
import { randomUUID } from 'node:crypto'
import { rename, unlink, writeFile } from 'node:fs/promises'
import { dirname, join } from 'node:path'
import type { MistakeBookPrintRequest, MistakeBookPrintResult } from '../shared/mistakeBookPrint'

const MAX_HTML_BYTES = 32 * 1024 * 1024
const PRINT_CSP = "default-src 'none'; script-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src 'none'; connect-src 'none'; object-src 'none'; frame-src 'none'; base-uri 'none'; form-action 'none'"
const unsupportedCss = /@import\b|url\s*\(|expression\s*\(/i

// User content is escaped by the document renderer. Inspect actual opening tags,
// respecting quoted attributes, so code exercises remain printable as plain text.
function* printOpeningTags(html: string): Generator<{ name: string; attributes: string }> {
  for (let cursor = 0; cursor < html.length; cursor++) {
    const start = html.indexOf('<', cursor)
    if (start < 0) return
    cursor = start
    const name = /^<([a-z][a-z0-9:-]*)/i.exec(html.slice(start, start + 128))
    if (!name) continue
    let quote: string | null = null
    for (let end = start + name[0].length; end < html.length; end++) {
      const character = html[end]
      if (quote) { if (character === quote) quote = null }
      else if (character === '"' || character === "'") quote = character
      else if (character === '>') {
        yield { name: name[1].toLowerCase(), attributes: html.slice(start + name[0].length, end) }
        cursor = end
        break
      } else if (character === '<') throw Error('打印内容格式无效，请重新预览。')
      if (end === html.length - 1) throw Error('打印内容格式无效，请重新预览。')
    }
  }
}

/** Reject active/resource-bearing markup; CSP, disabled JS and network isolation also apply. */
export function validateMistakePrintRequest(value: unknown): MistakeBookPrintRequest {
  if (!value || typeof value !== 'object') throw Error('打印内容无效，请重新预览。')
  const input = value as MistakeBookPrintRequest
  if (!['print', 'pdf'].includes(input.action) || typeof input.title !== 'string' || !input.title.trim()
    || input.title.length > 100 || /[\u0000-\u001f]/.test(input.title)
    || typeof input.html !== 'string' || Buffer.byteLength(input.html, 'utf8') > MAX_HTML_BYTES
    || !/^\s*<!doctype html>\s*<html\b/i.test(input.html) || !/<head\s*>/i.test(input.html)
    || !/<body\b/i.test(input.html) || !/<\/html>\s*$/i.test(input.html)) {
    throw Error('打印内容无效或过大，请缩小筛选范围后重试。')
  }
  if (/[\u0000]/.test(input.html)) throw Error('打印内容包含不支持的资源，请重新预览。')
  for (const tag of printOpeningTags(input.html)) {
    if (/^(?:script|iframe|frame|frameset|object|embed|base|form|input|button|textarea|select|audio|video|source|track)$/.test(tag.name)) {
      throw Error('打印内容包含不支持的资源，请重新预览。')
    }
    const attributes = tag.attributes.matchAll(/([^\s=\/<>]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'=<>`]+)))?/g)
    for (const attribute of attributes) {
      const name = attribute[1].toLowerCase(), value = attribute[2] ?? attribute[3] ?? attribute[4] ?? ''
      if (/^(?:on[a-z]+|srcset|srcdoc|http-equiv)$/.test(name) || name === 'style' && unsupportedCss.test(value)) {
        throw Error('打印内容包含不支持的资源，请重新预览。')
      }
      // Remote/file URLs, entities and unquoted resource values are rejected.
      if (/^(?:src|href|poster|background|action|xlink:href)$/.test(name)
        && (attribute[2] === undefined && attribute[3] === undefined || !/^data:image\/(?:png|jpeg|webp);base64,[A-Za-z0-9+/]+=*$/.test(value))) {
        throw Error('打印图片尚未完整读取，请重新预览。')
      }
    }
  }
  for (const style of input.html.matchAll(/<style\b[^>]*>([\s\S]*?)<\/style\s*>/gi)) {
    if (unsupportedCss.test(style[1])) throw Error('打印内容包含不支持的资源，请重新预览。')
  }
  return { action: input.action, title: input.title.trim(), html: input.html }
}

export function isolatedMistakePrintHtml(input: MistakeBookPrintRequest): string {
  return input.html.replace(/<head\s*>/i, `<head><meta http-equiv="Content-Security-Policy" content="${PRINT_CSP}">`)
}

export async function writeMistakePdf(path: string, pdf: Uint8Array): Promise<void> {
  const temporary = join(dirname(path), `.hsc-print-${randomUUID()}.tmp`)
  try {
    await writeFile(temporary, pdf, { flag: 'wx', mode: 0o600 })
    await rename(temporary, path)
  } finally {
    await unlink(temporary).catch(() => undefined)
  }
}

const readyScript = `Promise.all([document.fonts.ready,...Array.from(document.images,image=>image.decode())]).then(()=>Array.from(document.images).every(image=>image.complete&&image.naturalWidth>0),()=>false)`
// The document itself keeps scripting disabled. This world only runs the fixed readiness check.
const PRINT_READINESS_WORLD = 1

type PrintStage = 'admission' | 'validation' | 'save_dialog' | 'layout_load' | 'layout_readiness' | 'queue_submit' | 'pdf_render' | 'pdf_save'
type PrintOutcome = 'started' | 'completed' | MistakeBookPrintResult['status']
type PrintFailure = 'owner' | 'busy' | 'invalid_document' | 'load_error' | 'readiness_error' | 'unreadable_image'
  | 'layout_timeout' | 'window_closed' | 'renderer_gone' | 'queue_timeout' | 'printer_settings' | 'printer_failed' | 'printer_callback' | 'native_exception'
type ReportStage = (stage: PrintStage, outcome: PrintOutcome, failure?: PrintFailure) => void
type PrintDiagnostic = { print_stage: PrintStage; print_outcome: PrintOutcome; print_failure?: PrintFailure;
  print_action?: MistakeBookPrintRequest['action']; elapsed_ms: number }

function preparePage(window: BrowserWindow, html: string, report: ReportStage): Promise<void> {
  return new Promise((resolve, reject) => {
    let stage: PrintStage = 'layout_load'
    let settled = false
    const timer = setTimeout(() => finish('layout_timeout', '打印排版等待超时，请重试。'), 30_000)
    const closed = (): void => finish('window_closed', '打印窗口已关闭，请重新预览。')
    const crashed = (): void => finish('renderer_gone', '打印排版进程已中断，请重试。')
    const finish = (failure?: PrintFailure, message?: string): void => {
      if (settled) return
      settled = true
      clearTimeout(timer);window.removeListener('closed', closed)
      window.webContents.removeListener('render-process-gone', crashed)
      report(stage, failure ? 'failed' : 'completed', failure)
      if (failure) reject(Error(message));else resolve()
    }
    window.once('closed', closed)
    window.webContents.once('render-process-gone', crashed)
    report(stage, 'started')
    void window.loadURL(`data:text/html;charset=utf-8;base64,${Buffer.from(html).toString('base64')}`)
      .then(async () => {
        if (settled) return
        report(stage, 'completed')
        stage = 'layout_readiness'
        report(stage, 'started')
        const ready = await window.webContents.executeJavaScriptInIsolatedWorld(PRINT_READINESS_WORLD, [{ code: readyScript }])
        if (ready === true) finish()
        else finish('unreadable_image', '有题目图片未能读取，请重新预览。')
      })
      .catch(() => finish(stage === 'layout_load' ? 'load_error' : 'readiness_error', '打印内容未能完成排版，请重试。'))
  })
}

function submitToDefaultPrinter(window: BrowserWindow, report: ReportStage): Promise<MistakeBookPrintResult> {
  return new Promise(resolve => {
    let settled = false
    const finish = (result: MistakeBookPrintResult, failure?: PrintFailure): void => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      window.removeListener('closed', closed)
      window.webContents.removeListener('render-process-gone', crashed)
      report('queue_submit', result.status, failure)
      resolve(result)
    }
    // Once dispatched, a lost callback cannot tell us whether the spooler accepted it.
    const closed = (): void => finish({ status: 'unconfirmed' }, 'window_closed')
    const crashed = (): void => finish({ status: 'unconfirmed' }, 'renderer_gone')
    const timer = setTimeout(() => finish({ status: 'unconfirmed' }, 'queue_timeout'), 30_000)
    window.once('closed', closed)
    window.webContents.once('render-process-gone', crashed)
    report('queue_submit', 'started')
    try {
      // Electron resolves the current system default when deviceName is omitted.
      window.webContents.print({ silent: true, printBackground: true, pageSize: 'A4', margins: { marginType: 'none' } }, (success, reason) => {
        if (success) { finish({ status: 'submitted' }); return }
        if (/cancel/i.test(reason)) { finish({ status: 'cancelled' }); return }
        const failure = reason === 'Invalid printer settings' ? 'printer_settings' : reason === 'Print job failed' ? 'printer_failed' : 'printer_callback'
        finish({ status: 'failed', error: '系统未能提交打印，请检查默认打印机和连接后重试。' }, failure)
      })
    } catch { finish({ status: 'failed', error: '系统未能启动默认打印，请检查打印机设置或保存为 PDF。' }, 'native_exception') }
  })
}

/** Each job uses a nonpersistent, network-disabled session and never writes source HTML to disk. */
export function createMistakeBookPrintHandler(getPrimaryWindow: () => BrowserWindow | null,
  observe?: (event: PrintDiagnostic, requestId: string) => void) {
  let busy = false
  return async (event: IpcMainInvokeEvent, raw: unknown): Promise<MistakeBookPrintResult> => {
    const requestId = randomUUID(), started = performance.now()
    let action: MistakeBookPrintRequest['action'] | undefined
    const report: ReportStage = (stage, outcome, failure) => {
      try { observe?.({ print_stage: stage, print_outcome: outcome, print_failure: failure, print_action: action,
        elapsed_ms: Math.round(performance.now() - started) }, requestId) } catch { /* Diagnostics cannot affect printing. */ }
    }
    const owner = getPrimaryWindow()
    if (!owner || owner.isDestroyed() || owner.webContents.isDestroyed()) {
      report('admission', 'failed', 'owner')
      return { status: 'failed', error: '请从当前错题本窗口发起打印。' }
    }
    const frame = event.senderFrame
    const ownerUrl = owner.webContents.getURL()
    const currentOwner = (): boolean => Boolean(owner && getPrimaryWindow() === owner && !owner.isDestroyed()
      && !owner.webContents.isDestroyed() && event.sender === owner.webContents
      && frame === owner.webContents.mainFrame && owner.webContents.getURL() === ownerUrl)
    if (!currentOwner()) {
      report('admission', 'failed', 'owner')
      return { status: 'failed', error: '请从当前错题本窗口发起打印。' }
    }
    if (busy) {
      report('admission', 'failed', 'busy')
      return { status: 'failed', error: '已有打印任务正在处理，请先完成或取消。' }
    }
    let window: BrowserWindow | null = null
    let stage: PrintStage = 'validation'
    let layoutReported = false
    busy = true
    try {
      const input = validateMistakePrintRequest(raw)
      action = input.action
      let savePath: string | undefined
      if (input.action === 'pdf') {
        stage = 'save_dialog'
        const chosen = await dialog.showSaveDialog(owner!, {
          title: '保存错题本 PDF',
          defaultPath: `${input.title.replace(/[<>:"/\\|?*]/g, '_')}.pdf`,
          filters: [{ name: 'PDF 文档', extensions: ['pdf'] }],
          properties: ['showOverwriteConfirmation'],
        })
        if (chosen.canceled || !chosen.filePath) {
          report(stage, 'cancelled')
          return { status: 'cancelled' }
        }
        savePath = chosen.filePath
      }
      if (!currentOwner()) {
        report('admission', 'cancelled', 'owner')
        return { status: 'cancelled' }
      }
      stage = 'layout_load'
      const printSession = session.fromPartition(`mistake-print-${randomUUID()}`, { cache: false })
      printSession.setPermissionCheckHandler(() => false)
      printSession.setPermissionRequestHandler((_contents, _permission, callback) => callback(false))
      printSession.webRequest.onBeforeRequest((details, callback) => {
        callback({ cancel: !details.url.startsWith('data:') })
      })
      window = new BrowserWindow({
        show: false, title: input.title, width: 850, height: 1100, parent: owner!, autoHideMenuBar: true,
        webPreferences: { session: printSession, sandbox: true, contextIsolation: true,
          nodeIntegration: false, javascript: false, webSecurity: true, devTools: false,
          backgroundThrottling: false, spellcheck: false },
      })
      window.webContents.setWindowOpenHandler(() => ({ action: 'deny' }))
      window.webContents.on('will-navigate', navigation => navigation.preventDefault())
      window.webContents.on('will-redirect', navigation => navigation.preventDefault())
      await preparePage(window, isolatedMistakePrintHtml(input), (step, outcome, failure) => {
        if (outcome === 'failed') layoutReported = true
        report(step, outcome, failure)
      })
      if (!currentOwner()) {
        report('admission', 'cancelled', 'owner')
        return { status: 'cancelled' }
      }
      if (input.action === 'print') {
        stage = 'queue_submit'
        return await submitToDefaultPrinter(window, report)
      }
      stage = 'pdf_render'
      const pdf = await window.webContents.printToPDF({ pageSize: 'A4', printBackground: true, preferCSSPageSize: true,
        margins: { top: 0, bottom: 0, left: 0, right: 0 } })
      if (!currentOwner()) {
        report('admission', 'cancelled', 'owner')
        return { status: 'cancelled' }
      }
      stage = 'pdf_save'
      await writeMistakePdf(savePath!, pdf)
      report(stage, 'saved')
      return { status: 'saved' }
    } catch (error) {
      if (!layoutReported) report(stage, 'failed', stage === 'validation' ? 'invalid_document' : 'native_exception')
      return { status: 'failed', error: error instanceof Error && /^打印|^有题目|^系统/.test(error.message)
        ? error.message : '打印或保存失败，请检查磁盘空间及权限后重试。' }
    } finally {
      if (window && !window.isDestroyed()) window.destroy()
      busy = false
    }
  }
}
