// @vitest-environment node
import { once } from 'node:events'
import WebSocket, { WebSocketServer } from 'ws'
import { expect, it, vi } from 'vitest'
vi.mock('./money-egress', () => ({ denyUnpricedMoneyEgress: () => undefined }))
vi.mock('electron', () => ({ app: { getPath: () => '' }, safeStorage: {} }))
import { QwenNativeSocketRelay } from './qwen-native-socket'

it('sends a masked WebSocket control ping and accepts the real peer pong without audio', async () => {
  const server = new WebSocketServer({ host: '127.0.0.1', port: 0 })
  await once(server, 'listening')
  const port = (server.address() as {port: number}).port
  let now = 0, peer: WebSocket | undefined, payload: Buffer | undefined, messages = 0
  server.on('connection', socket => { peer = socket; socket.on('ping', value => { payload = value }); socket.on('message', () => { messages++ }) })
  const ids = new Set<string>()
  const relay = new QwenNativeSocketRelay({ allowed: () => true, current: (_event, id) => ids.has(id),
    register: () => { ids.add('host-real'); return 'host-real' }, retire: (_event, id) => { ids.delete(id) },
    policy: async () => ({capture: 'configured', runtimeEpoch: 1}),
    config: async () => ({apiKey:'public-synthetic-key-not-a-real-credential',endpoint:'https://public-workspace.cn-beijing.maas.aliyuncs.com',model:'qwen3.8-omni-flash-realtime',voice:'Tina',enabled:true}),
    emit: () => undefined, now: () => now,
    dial: () => new WebSocket(`ws://127.0.0.1:${port}`) })
  const event = {sender:{id:1}}
  try {
    const opened = await relay.open(event, 'session-public')
    const row = [...(relay as any).rows.values()][0] as any
    expect(peer).toBeDefined(); now = 30000
    await (relay as any).refresh(row)
    const pong = once(row.socket, 'pong')
    ;(relay as any).watch(row)
    let deadline: ReturnType<typeof setTimeout> | undefined
    try { await Promise.race([pong, new Promise((_, reject) => { deadline = setTimeout(() => reject(Error('PUBLIC_LOOPBACK_PONG_TIMEOUT')), 2000) })]) }
    finally { if (deadline) clearTimeout(deadline) }
    expect(payload).toHaveLength(8); expect(row.pingPayload).toBeNull(); expect(row.nextPingAt).toBe(60000)
    expect(messages).toBe(0); expect(ids.has(opened.nativeHostConnectionId)).toBe(true)
    relay.close(event, opened.nativeHostConnectionId); expect(ids.size).toBe(0)
  } finally {
    relay.retireOwner(1); peer?.terminate(); await new Promise<void>(resolve => server.close(() => resolve()))
  }
})
