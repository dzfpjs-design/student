import { BrowserWindow } from 'electron'

type Failure = 'backend_pending' | 'backend_abnormal' | 'external' | 'diagnostics'
type State = { reason: Failure; acknowledge?: () => boolean }

/** A local recovery surface owns only quit actions, never a service/data bridge. */
export function createVerifiedQuitRecovery(options: {
  canExit: () => boolean
  retry: () => void
  loadFailed: () => void
}): { show: (reason: Failure, acknowledge?: () => boolean) => void; focusIfActive: () => boolean; clear: () => void } {
  let window: BrowserWindow | null = null, state: State | null = null
  let generation = 0, page = '', retryUrl = '', acknowledgeUrl = ''
  const focus = (current: BrowserWindow): void => {
    if (current.isMinimized()) current.restore()
    current.show(); current.focus()
  }
  const show = (reason: Failure, acknowledge?: () => boolean): void => {
    state = { reason, acknowledge: reason === 'backend_abnormal' ? acknowledge : undefined }
    generation += 1
    retryUrl = `hsc-verified-quit:retry/${generation}`
    acknowledgeUrl = `hsc-verified-quit:acknowledge/${generation}`
    const message = reason === 'backend_pending'
      ? '后台或草稿保护组件还在保存或关闭。应用保留原进程，不会强制结束或启动替代服务。请稍后重试安全退出。'
      : reason === 'diagnostics'
        ? '运行日志尚未确认写入完成。请重试保存日志并安全退出。'
      : reason === 'external'
        ? '还有外部活动尚未确认关闭。请先关闭游戏或视频窗口，再重试安全退出。'
        : '后台已经异常终止，本次保存尚未确认。你可以关闭应用，下次启动会按原恢复流程检查未完成的工作。'
    const acknowledgement = reason === 'backend_abnormal' && acknowledge
      ? `<p><a href="${acknowledgeUrl}">关闭应用，下次启动检查恢复</a></p>` : ''
    page = 'data:text/html;charset=utf-8,' + encodeURIComponent(`<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'"><title>安全退出尚未完成</title><style>body{font:18px/1.7 system-ui,sans-serif;max-width:36em;margin:3em auto;padding:0 2em;background:#f5f6f8;color:#1b2733}a{display:inline-block;padding:.6em 1em;background:#174ea6;color:white;border-radius:.4em}a:focus{outline:3px solid #111;outline-offset:4px}</style><h1>安全退出尚未完成</h1><p>${message}</p><p><a href="${retryUrl}">重试安全退出</a></p>${acknowledgement}</html>`)
    if (window && !window.isDestroyed()) {
      void window.loadURL(page).catch(options.loadFailed); focus(window); return
    }
    const current = new BrowserWindow({ title: '家庭学习伙伴 · 安全退出尚未完成', width: 720, height: 540, minWidth: 560, minHeight: 440, show: false, autoHideMenuBar: true,
      webPreferences: { sandbox: true, contextIsolation: true, nodeIntegration: false, webSecurity: true, partition: 'verified-quit-recovery' } })
    window = current
    current.setMenu(null)
    current.webContents.setWindowOpenHandler(() => ({ action: 'deny' }))
    current.webContents.session.setPermissionRequestHandler((_contents, _permission, callback) => callback(false))
    current.webContents.session.setPermissionCheckHandler(() => false)
    current.webContents.session.webRequest.onBeforeRequest({ urls: ['<all_urls>'] }, (details, callback) => callback({ cancel: details.url !== page }))
    current.webContents.on('will-navigate', (event, url) => {
      event.preventDefault()
      if (window !== current || current.isDestroyed() || !state) return
      if (url === retryUrl) options.retry()
      else if (url === acknowledgeUrl && state.reason === 'backend_abnormal' && state.acknowledge?.() === true) options.retry()
    })
    current.on('close', event => {
      if (!options.canExit()) { event.preventDefault(); if (window === current && state) options.retry() }
    })
    current.on('closed', () => { if (window === current) window = null })
    void current.loadURL(page).catch(options.loadFailed)
    focus(current)
  }
  return {
    show,
    focusIfActive: () => { if (!state || options.canExit()) return false; if (window && !window.isDestroyed()) focus(window); else show(state.reason, state.acknowledge); return true },
    clear: () => { state = null },
  }
}
