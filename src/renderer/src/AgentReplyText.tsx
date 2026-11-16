import { useSyncExternalStore } from 'react'

// Streaming transcripts should redraw only the text, not the entire workspace.
export function createAgentReplyStore(initial: string) {
  let value = initial
  const listeners = new Set<() => void>()
  return {
    getSnapshot: () => value,
    subscribe: (listener: () => void) => { listeners.add(listener); return () => { listeners.delete(listener) } },
    set: (next: string) => {
      if (next === value) return
      value = next
      listeners.forEach((listener) => listener())
    },
  }
}

export function AgentReplyText({ store }: { store: ReturnType<typeof createAgentReplyStore> }) {
  const reply = useSyncExternalStore(store.subscribe, store.getSnapshot, store.getSnapshot)
  return <span>{reply}</span>
}
