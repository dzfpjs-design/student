import { cleanup, render } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { useQuestionViewportFocus } from './useQuestionViewportFocus'
const original = Object.getOwnPropertyDescriptor(Element.prototype, 'scrollIntoView')
afterEach(() => {
  cleanup()
  if (original) Object.defineProperty(Element.prototype, 'scrollIntoView', original)
  else Reflect.deleteProperty(Element.prototype, 'scrollIntoView')
})
function Question({ identity, current = true }: { identity: string | null; current?: boolean }) {
  const ref = useQuestionViewportFocus<HTMLParagraphElement>(identity, () => current)
  return <p ref={ref}>当前核验原题</p>
}
it('focuses each current question once and re-focuses a resumed identity', () => {
  const focus = vi.fn()
  Object.defineProperty(Element.prototype, 'scrollIntoView', { configurable: true, value: focus })
  const view = render(<Question identity="child-1:prompt-1:v1" />)
  expect(focus).toHaveBeenCalledWith({ block: 'start', behavior: 'auto' })
  view.rerender(<Question identity="child-1:prompt-1:v1" />)
  expect(focus).toHaveBeenCalledTimes(1)
  view.rerender(<Question identity="child-1:prompt-1:v2" />)
  expect(focus).toHaveBeenCalledTimes(2)
  view.rerender(<Question identity={null} />)
  view.rerender(<Question identity="child-1:prompt-1:v2" />)
  expect(focus).toHaveBeenCalledTimes(3)
})
it('does not focus unverified, retired or privacy-stopped ownership', () => {
  const focus = vi.fn()
  Object.defineProperty(Element.prototype, 'scrollIntoView', { configurable: true, value: focus })
  const view = render(<Question identity={null} />)
  view.rerender(<Question identity="retired-child:prompt" current={false} />)
  expect(focus).not.toHaveBeenCalled()
  view.rerender(<Question identity="retired-child:prompt" current />)
  expect(focus).toHaveBeenCalledTimes(1)
  view.unmount()
  expect(focus).toHaveBeenCalledTimes(1)
})
