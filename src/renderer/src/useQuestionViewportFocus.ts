import { useLayoutEffect, useRef } from 'react'

/** Focus a verified question once per identity, without delayed work surviving its owner. */
export function useQuestionViewportFocus<T extends HTMLElement>(key: string | null, isCurrent: () => boolean) {
  const ref = useRef<T | null>(null)
  const focused = useRef<string | null>(null)
  useLayoutEffect(() => {
    if (!key) { focused.current = null; return }
    if (focused.current === key || !ref.current || !isCurrent()) return
    ref.current.scrollIntoView?.({ block: 'start', behavior: 'auto' })
    focused.current = key
  }, [key, isCurrent])
  return ref
}
