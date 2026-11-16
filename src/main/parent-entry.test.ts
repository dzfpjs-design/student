import { describe, expect, it, vi } from 'vitest'
import { createParentEntryDelivery, createParentEntryShortcut, parentEntryMenuItem, PARENT_ENTRY_EVENT } from './parent-entry'
import type { Input } from 'electron'

function setup() {
  const window = { isDestroyed: vi.fn(() => false), isMinimized: vi.fn(() => true), restore: vi.fn(), show: vi.fn(), focus: vi.fn(), webContents: { id: 17, send: vi.fn() } }
  const create = vi.fn()
  const delivery = createParentEntryDelivery(() => window, create)
  return { window, delivery, create }
}
describe('native parent entry is only a PIN navigation request', () => {
  it('has a permanent menu identity and cross-platform accelerator', () => {
    const request = vi.fn(), item = parentEntryMenuItem(request)
    expect(item.id).toBe('parent-center-open'); expect(item.accelerator).toBe('CmdOrCtrl+Shift+P')
    ;(item.click as () => void)(); expect(request).toHaveBeenCalledOnce()
  })
  it('coalesces early requests and ignores a different renderer readiness message', () => {
    const { window, delivery } = setup()
    delivery.request(); delivery.request(); delivery.ready(99)
    expect(window.webContents.send).not.toHaveBeenCalled()
    delivery.ready(17)
    expect(window.webContents.send).toHaveBeenCalledExactlyOnceWith(PARENT_ENTRY_EVENT)
    expect(window.restore).toHaveBeenCalled()
    delivery.ready(17); expect(window.webContents.send).toHaveBeenCalledOnce()
  })
  it('waits for a new listener after renderer reload without replaying old delivered requests', () => {
    const { window, delivery } = setup()
    delivery.ready(17); delivery.request()
    delivery.loading(); delivery.ready(17)
    expect(window.webContents.send).toHaveBeenCalledOnce()
    delivery.loading(); delivery.request()
    expect(window.webContents.send).toHaveBeenCalledOnce()
    delivery.ready(17); expect(window.webContents.send).toHaveBeenCalledTimes(2)
  })
  it('does not send data to a destroyed primary window', () => {
    const { window, delivery, create } = setup()
    delivery.ready(17); window.isDestroyed.mockReturnValue(true); delivery.request()
    expect(create).toHaveBeenCalledOnce(); expect(window.webContents.send).not.toHaveBeenCalled()
  })
})

describe('primary-window parent shortcut', () => {
  const input = (patch: Partial<Input> = {}): Input => ({
    type: 'keyDown', key: 'P', code: 'KeyP', location: 0, modifiers: ['meta', 'shift'], meta: true, shift: true,
    control: false, alt: false, isAutoRepeat: false, isComposing: false, ...patch,
  })
  it('prevents the menu default and opens only once per physical press', () => {
    const request = vi.fn(), event = { preventDefault: vi.fn() }
    const shortcut = createParentEntryShortcut(request, 'darwin')
    shortcut.handle(event, input()); shortcut.handle(event, input())
    shortcut.handle(event, input({ isAutoRepeat: true }))
    expect(request).toHaveBeenCalledOnce()
    expect(event.preventDefault).toHaveBeenCalledTimes(3)
    shortcut.handle(event, input({ type: 'keyUp', meta: false, shift: false }))
    shortcut.handle(event, input())
    expect(request).toHaveBeenCalledTimes(2)
  })
  it.each([
    { alt: true }, { control: true }, { meta: false }, { shift: false },
    { key: 'Q', code: 'KeyQ' }, { isComposing: true }, { type: 'char' as const },
  ])('does not consume other input: %j', patch => {
    const request = vi.fn(), event = { preventDefault: vi.fn() }
    createParentEntryShortcut(request, 'darwin').handle(event, input(patch))
    expect(request).not.toHaveBeenCalled(); expect(event.preventDefault).not.toHaveBeenCalled()
  })
  it('uses Control, not Command, on Windows and releases on blur', () => {
    const request = vi.fn(), event = { preventDefault: vi.fn() }
    const shortcut = createParentEntryShortcut(request, 'win32')
    shortcut.handle(event, input()); expect(request).not.toHaveBeenCalled()
    shortcut.handle(event, input({ meta: false, control: true }))
    shortcut.reset()
    shortcut.handle(event, input({ meta: false, control: true }))
    expect(request).toHaveBeenCalledTimes(2)
  })
  it('consumes an orphan auto-repeat without entering the parent view', () => {
    const request = vi.fn(), event = { preventDefault: vi.fn() }
    createParentEntryShortcut(request, 'darwin').handle(event, input({ isAutoRepeat: true }))
    expect(request).not.toHaveBeenCalled(); expect(event.preventDefault).toHaveBeenCalledOnce()
  })
})
