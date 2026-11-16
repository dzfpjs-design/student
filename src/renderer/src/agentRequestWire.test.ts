import { createServer } from 'node:http'
import { once } from 'node:events'
import { expect, it, vi } from 'vitest'
import { localApi, resetRuntimeCache } from './api'
import { pendingAgentRequests } from './agentRequestJournal'

const nativeFetch = globalThis.fetch
it('preserves the original identity when an actual loopback HTTP200 reply is truncated', async () => {
  let posts = 0
  const server = createServer((request, response) => {
    request.resume()
    request.on('end', () => {
      posts++
      response.writeHead(200, { 'Content-Type': 'application/json', 'Content-Length': 500 })
      response.write('{"agent_turn_id":"wire-saved",')
      setTimeout(() => response.destroy(), 30)
    })
  })
  try {
    server.listen(0, '127.0.0.1')
    await once(server, 'listening')
    const port = (server.address() as { port: number }).port
    resetRuntimeCache()
    vi.stubGlobal('desktop', { getRuntimeConfig: async () => ({ status: 'ready', localApiUrl: `http://127.0.0.1:${port}`, sessionToken: 'synthetic' }) })
    vi.stubGlobal('fetch', nativeFetch)
    await expect(localApi.agentTurn('wire-session', 'wire fixture only')).rejects.toMatchObject({ code: 'AGENT_REQUEST_UNCONFIRMED' })
    expect(posts).toBe(1)
    expect(pendingAgentRequests('wire-session')).toHaveLength(1)
  } finally {
    server.closeAllConnections()
    if (server.listening) await new Promise<void>(resolve => server.close(() => resolve()))
    localStorage.clear(); resetRuntimeCache(); vi.unstubAllGlobals()
  }
})
