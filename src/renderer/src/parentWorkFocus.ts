import { useLayoutEffect, useRef, type RefObject } from 'react'

export type ParentWorkFocus = { objectId: string; nonce: string; relatedId?: string | null }
export type ParentWorkFocusProps = {
  focusRequest?: ParentWorkFocus | null
  onFocusResolved?: (nonce: string, error?: string) => void
}

// A deep link succeeds only when its exact evidence is rendered, never on a panel scroll alone.
export function useParentWorkFocus(
  request: ParentWorkFocus | null | undefined, ready: boolean,
  root: RefObject<HTMLElement | null>, onResolved: ParentWorkFocusProps['onFocusResolved'],
): void {
  const settled = useRef('')
  useLayoutEffect(() => {
    if (!request || !ready || settled.current === request.nonce || !root.current) return
    const item = Array.from(root.current.querySelectorAll<HTMLElement>('[data-work-object]'))
      .find(element => element.dataset.workObject === request.objectId)
    settled.current = request.nonce
    if (!item) { onResolved?.(request.nonce, '这条待办已变化或尚未加载，请刷新后重试。'); return }
    item.scrollIntoView?.({ block: 'center', behavior: 'smooth' })
    item.tabIndex = -1
    item.focus({ preventScroll: true })
    onResolved?.(request.nonce)
  }, [request, ready, root, onResolved])
}
