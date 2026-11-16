// @vitest-environment jsdom
import { fireEvent } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { APP_SPACE_VOICE_PRIORITY, registerAppSpaceVoice } from './appSpaceVoice'

const handles: ReturnType<typeof registerAppSpaceVoice>[] = []
const space = { key: ' ', code: 'Space' }

function binding(priority = APP_SPACE_VOICE_PRIORITY.general as number, condition = () => true) {
  const callbacks = { priority, eligible: vi.fn(condition), begin: vi.fn(() => true), finish: vi.fn(), cancel: vi.fn() }
  const handle = registerAppSpaceVoice(callbacks)
  handles.push(handle)
  return { ...callbacks, handle }
}

function down(extra: Partial<KeyboardEventInit> = {}) {
  return fireEvent.keyDown(window, { ...space, ...extra })
}

function up() {
  return fireEvent.keyUp(window, space)
}

afterEach(() => {
  for (const handle of handles.splice(0)) handle.unregister()
  document.body.replaceChildren()
  vi.restoreAllMocks()
})

it('gives one held key to the highest eligible binding and suppresses repeat', () => {
  const low = binding()
  const focused = binding(APP_SPACE_VOICE_PRIORITY.focused)
  expect(down()).toBe(false)
  expect(down({ repeat: true })).toBe(false)
  expect(down()).toBe(false)
  expect(focused.begin).toHaveBeenCalledTimes(1)
  expect(low.begin).not.toHaveBeenCalled()
  expect(up()).toBe(false)
  expect(up()).toBe(true)
  expect(focused.finish).toHaveBeenCalledTimes(1)
  expect(low.finish).not.toHaveBeenCalled()
})

it('does not continue a declined or failed request on another surface', () => {
  const low = binding()
  const focused = binding(APP_SPACE_VOICE_PRIORITY.focused)
  focused.begin.mockReturnValueOnce(false).mockImplementationOnce(() => { throw new Error('unavailable') })
  expect(down()).toBe(true)
  expect(up()).toBe(true)
  expect(down()).toBe(true)
  expect(up()).toBe(true)
  expect(focused.cancel).toHaveBeenCalledTimes(1)
  expect(focused.finish).not.toHaveBeenCalled()
  expect(low.begin).not.toHaveBeenCalled()
  focused.eligible.mockImplementationOnce(() => { throw new Error('stale surface') })
  expect(down()).toBe(true)
  expect(low.begin).not.toHaveBeenCalled()
})

it('cancels a held key when a new eligible route opens and does not transfer the release', () => {
  const old = binding()
  down()
  const next = binding(APP_SPACE_VOICE_PRIORITY.focused)
  expect(old.cancel).toHaveBeenCalledTimes(1)
  expect(down()).toBe(false)
  expect(up()).toBe(false)
  expect(next.begin).not.toHaveBeenCalled()
  expect(old.finish).not.toHaveBeenCalled()
  expect(next.finish).not.toHaveBeenCalled()
  down()
  up()
  expect(next.begin).toHaveBeenCalledTimes(1)
  expect(next.finish).toHaveBeenCalledTimes(1)
})

it('rechecks scope on refresh and keyup, and cancels on unregistration', () => {
  let focusedAllowed = false
  const old = binding()
  const focused = binding(APP_SPACE_VOICE_PRIORITY.focused, () => focusedAllowed)
  down()
  focusedAllowed = true
  focused.handle.refresh()
  expect(old.cancel).toHaveBeenCalledTimes(1)
  up()
  down()
  focusedAllowed = false
  up()
  expect(focused.cancel).toHaveBeenCalledTimes(1)
  down()
  old.handle.unregister()
  up()
  expect(old.cancel).toHaveBeenCalledTimes(2)
  expect(old.finish).not.toHaveBeenCalled()
  expect(focused.finish).not.toHaveBeenCalled()
})

it('cancels on blur, hidden state or privacy pause without accepting the stale release', () => {
  const surface = binding()
  down()
  fireEvent(window, new Event('blur'))
  expect(up()).toBe(false)
  down()
  const hidden = vi.spyOn(document, 'hidden', 'get').mockReturnValue(true)
  fireEvent(document, new Event('visibilitychange'))
  up()
  expect(down()).toBe(true)
  hidden.mockReturnValue(false)
  down()
  fireEvent(window, new Event('hsc-privacy-pause'))
  up()
  expect(surface.begin).toHaveBeenCalledTimes(3)
  expect(surface.cancel).toHaveBeenCalledTimes(3)
  expect(surface.finish).not.toHaveBeenCalled()
})

it('leaves Space available for native controls, focus navigation, and IME or modified keys', () => {
  const surface = binding()
  document.body.innerHTML = `<input><textarea></textarea><button>按钮</button><a href="#link">链接</a>
    <div role="switch" tabindex="-1">开关</div><div contenteditable="true"><span>编辑内容</span></div>
    <div role="dialog"><span>对话框内容</span></div>`
  for (const selector of ['input', 'textarea', 'button', 'a', '[role="switch"]', '[contenteditable] span', '[role="dialog"] span']) {
    const target = document.querySelector(selector) as HTMLElement
    target.focus()
    expect(fireEvent.keyDown(target, space)).toBe(true)
    expect(fireEvent.keyUp(target, space)).toBe(true)
    target.blur()
  }
  for (const extra of [{ isComposing: true }, { key: 'Process' }, { altKey: true }, { ctrlKey: true }, { shiftKey: true }, { metaKey: true }]) {
    expect(down(extra)).toBe(true)
  }
  expect(surface.begin).not.toHaveBeenCalled()
  expect(surface.finish).not.toHaveBeenCalled()
})

it('cancels a hold when focus enters a control and verifies the release target again', () => {
  const surface = binding()
  const button = document.createElement('button')
  document.body.append(button)

  down()
  button.focus()
  expect(surface.cancel).toHaveBeenCalledTimes(1)
  expect(fireEvent.keyUp(button, space)).toBe(false)
  button.blur()
  down()
  // Even if the window missed focusin, the release target has no authority to send.
  expect(fireEvent.keyUp(button, space)).toBe(false)
  expect(surface.cancel).toHaveBeenCalledTimes(2)
  expect(surface.finish).not.toHaveBeenCalled()
  button.focus()
  expect(fireEvent.keyDown(button, space)).toBe(true)
  expect(fireEvent.keyUp(button, space)).toBe(true)
  button.blur()
  surface.begin.mockImplementationOnce(() => { button.focus(); return true })
  expect(down()).toBe(false)
  expect(surface.cancel).toHaveBeenCalledTimes(3)
  expect(fireEvent.keyUp(button, space)).toBe(false)
})

it('shares one keyboard listener and removes it after the last binding', () => {
  const attach = vi.spyOn(window, 'addEventListener')
  const detach = vi.spyOn(window, 'removeEventListener')
  const first = binding()
  const second = binding()
  expect(attach.mock.calls.filter(([type]) => type === 'keydown')).toHaveLength(1)
  first.handle.unregister()
  expect(detach.mock.calls.filter(([type]) => type === 'keydown')).toHaveLength(0)
  second.handle.unregister()
  expect(detach.mock.calls.filter(([type]) => type === 'keydown')).toHaveLength(1)
  expect(down()).toBe(true)
})
