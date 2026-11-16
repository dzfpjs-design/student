import { beforeEach, expect, it, vi } from 'vitest'
const bridge = vi.hoisted(() => ({ expose:vi.fn(), on:vi.fn(), send:vi.fn(), remove:vi.fn(), invoke:vi.fn() }))
vi.mock('electron', () => ({ contextBridge:{exposeInMainWorld:bridge.expose},
  ipcRenderer:{on:bridge.on,send:bridge.send,removeListener:bridge.remove,invoke:bridge.invoke} }))
beforeEach(() => { vi.clearAllMocks(); vi.resetModules() })
it('preserves the exact backend resource identities in open, close and native events', async () => {
  await import('./index')
  const api=bridge.expose.mock.calls[0][1]
  const values={path:'synthetic.webm',title:'synthetic',maxSeconds:60,contentSessionId:'original-content'}
  api.openControlledMedia(values)
  expect(bridge.invoke).toHaveBeenLastCalledWith('controlled-media:open',values)
  api.closeControlledMedia('renderer_reloaded','original-content')
  expect(bridge.invoke).toHaveBeenLastCalledWith('controlled-media:close','renderer_reloaded','original-content')
  api.closeControlledGame('child_requested','original-game')
  expect(bridge.invoke).toHaveBeenLastCalledWith('controlled-game:close','child_requested','original-game')
  const callback=vi.fn(),remove=api.onControlledMediaClosed(callback)
  const listener=bridge.on.mock.calls.find(call=>call[0]==='controlled-media:closed')![1]
  listener({},'window_closed','original-content')
  expect(callback).toHaveBeenCalledExactlyOnceWith('window_closed','original-content')
  remove()
  expect(bridge.remove).toHaveBeenCalledWith('controlled-media:closed',listener)
})
