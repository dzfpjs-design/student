const port = Number(process.argv[2] || 9222)
const targets = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json()
const page = targets.find((candidate) => candidate.type === 'page' && candidate.webSocketDebuggerUrl)
if (!page) throw new Error('Desktop page not found')

const socket = new WebSocket(page.webSocketDebuggerUrl)
await new Promise((resolve, reject) => {
  socket.addEventListener('open', resolve, { once: true })
  socket.addEventListener('error', reject, { once: true })
})
socket.send(JSON.stringify({
  id: 1,
  method: 'Runtime.evaluate',
  params: { expression: 'window.close(); true', returnByValue: true },
}))
await new Promise((resolve) => setTimeout(resolve, 1_000))
socket.close()
console.log(JSON.stringify({ close_requested: true, port }))
