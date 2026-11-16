import type { Input, MenuItemConstructorOptions } from 'electron'

export const PARENT_ENTRY_EVENT = 'parent-center:open'
export const PARENT_ENTRY_READY = 'parent-center:ready'

type ParentWindow = {
  isDestroyed(): boolean
  isMinimized(): boolean
  restore(): void
  show(): void
  focus(): void
  webContents: { id: number; send(channel: string): void }
}

export function parentEntryMenuItem(request: () => void): MenuItemConstructorOptions {
  return { id: 'parent-center-open', label: '打开家长中心…', accelerator: 'CmdOrCtrl+Shift+P', click: request }
}

export function createParentEntryShortcut(request: () => void, platform: NodeJS.Platform) {
  let pressed = false
  return {
    reset(): void { pressed = false },
    handle(event: { preventDefault(): void }, input: Input): void {
      const isP = input.code === 'KeyP' && input.key.toLowerCase() === 'p'
      if (!isP || input.isComposing) return
      if (input.type === 'keyUp' && pressed) {
        pressed = false
        event.preventDefault()
        return
      }
      const command = platform === 'darwin' ? input.meta && !input.control : input.control && !input.meta
      if (!command || !input.shift || input.alt || !['keyDown', 'keyUp'].includes(input.type)) return
      // Suppress the menu accelerator too, so one key cannot deliver two requests.
      event.preventDefault()
      if (input.type !== 'keyDown' || input.isAutoRepeat || pressed) return
      pressed = true
      request()
    },
  }
}

export function createParentEntryDelivery(getWindow: () => ParentWindow | null, createWindow: () => void) {
  let pending = false
  let readyId: number | null = null
  const deliver = (): void => {
    const window = getWindow()
    if (!pending || !window || window.isDestroyed() || readyId !== window.webContents.id) return
    pending = false
    window.webContents.send(PARENT_ENTRY_EVENT)
  }
  return {
    request(): void {
      pending = true
      if (!getWindow() || getWindow()!.isDestroyed()) createWindow()
      const window = getWindow()
      if (window && !window.isDestroyed()) {
        if (window.isMinimized()) window.restore()
        window.show(); window.focus()
      }
      deliver()
    },
    ready(senderId: number): void {
      const window = getWindow()
      if (!window || window.isDestroyed() || senderId !== window.webContents.id) return
      readyId = senderId
      deliver()
    },
    loading(): void { readyId = null },
  }
}
