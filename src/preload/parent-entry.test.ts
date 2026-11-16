import { beforeEach, expect, it, vi } from 'vitest'
const bridge = vi.hoisted(() => ({ expose: vi.fn(), on: vi.fn(), send: vi.fn(), remove: vi.fn(), invoke: vi.fn() }))
vi.mock('electron', () => ({ contextBridge: { exposeInMainWorld: bridge.expose },
  ipcRenderer: { on: bridge.on, send: bridge.send, removeListener: bridge.remove, invoke: bridge.invoke } }))
beforeEach(() => { vi.clearAllMocks(); vi.resetModules() })
it('subscribes before readiness and delivers no menu-supplied identity or authorization payload', async () => {
  await import('./index')
  const api = bridge.expose.mock.calls[0][1]
  const callback = vi.fn(), unsubscribe = api.onOpenParentCenter(callback)
  expect(bridge.on).toHaveBeenCalledWith('parent-center:open', expect.any(Function))
  expect(bridge.on.mock.invocationCallOrder[0]).toBeLessThan(bridge.send.mock.invocationCallOrder[0])
  expect(bridge.send).toHaveBeenCalledExactlyOnceWith('parent-center:ready')
  bridge.on.mock.calls[0][1]({ sender: 'ignored' }, { parent_token: 'not-accepted', child_id: 'not-accepted' })
  expect(callback).toHaveBeenCalledExactlyOnceWith()
  expect(bridge.invoke).not.toHaveBeenCalled()
  unsubscribe()
  expect(bridge.remove).toHaveBeenCalledWith('parent-center:open', bridge.on.mock.calls[0][1])
})
