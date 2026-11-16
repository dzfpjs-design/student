import assert from 'node:assert/strict'
import { writeFile } from 'node:fs/promises'

const close = process.argv.includes('--close')
const output = process.argv.find((value) => value.startsWith('--output='))?.slice(9)
const targets = await (await fetch('http://127.0.0.1:9222/json/list', { signal: AbortSignal.timeout(5000) })).json()
const page = targets.find((item) => item.type === 'page' && item.url.startsWith('file:') && item.url.includes('index.html'))
assert(page, 'Installed application page is not available')
const socket = new WebSocket(page.webSocketDebuggerUrl)
await new Promise((resolve, reject) => {
  socket.addEventListener('open', resolve, { once: true })
  socket.addEventListener('error', reject, { once: true })
})
try {
  const expression = close ? 'window.close(); true' : `(async () => {
    const runtime = await window.desktop.getRuntimeConfig();
    const model = await window.desktop.getModelSettings();
    const voice = await window.desktop.getVoiceSettings();
    const response = await fetch(runtime.localApiUrl + '/v1/health', {headers: {'X-HSC-Session': runtime.sessionToken}});
    const schemaResponse = await fetch(runtime.localApiUrl + '/openapi.json', {headers: {'X-HSC-Session': runtime.sessionToken}});
    const schema = schemaResponse.ok ? await schemaResponse.json() : {};
    const paths = schema.paths || {};
    return {runtime_status: runtime.status, health_http: response.status,
      document_learning_endpoint: Boolean(paths['/v1/sessions/{session_id}/document-monitor/learning-context']),
      document_ocr_endpoint: Boolean(paths['/v1/sessions/{session_id}/document-monitor/ocr']),
      daily_learning_summary_endpoint: Boolean(paths['/v1/children/{child_id}/daily-learning-summary']),
      session_learning_summary_endpoint: Boolean(paths['/v1/sessions/{session_id}/learning-summary']),
      bound_review_question_endpoint: Boolean(paths['/v1/plan-items/{plan_item_id}/review-question']),
      voice_tutorial_ipc: typeof window.desktop.getVoiceOnboarding === 'function' && typeof window.desktop.saveVoiceOnboarding === 'function',
      model_configured: model.configured, model_provider: model.provider, model: model.model,
      voice_configured: voice.configured, voice_provider: voice.provider,
      visible: document.visibilityState === 'visible', root_rendered: Boolean(document.querySelector('#root')?.children.length)};
  })()`
  const result = await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('Installed runtime inspection timed out')), 20000)
    socket.addEventListener('message', (event) => {
      const message = JSON.parse(event.data)
      if (message.id !== 1) return
      clearTimeout(timer)
      if (message.error || message.result?.exceptionDetails) reject(new Error('Installed runtime inspection failed'))
      else resolve(message.result?.result?.value)
    })
    socket.addEventListener('close', () => {
      if (close) { clearTimeout(timer); resolve({close_requested: true}) }
    }, {once: true})
    socket.send(JSON.stringify({id: 1, method: 'Runtime.evaluate', params: {expression, awaitPromise: true, returnByValue: true}}))
  })
  if (!close) assert(result.runtime_status === 'ready' && result.health_http === 200 && result.root_rendered)
  if (output) await writeFile(output, JSON.stringify(result, null, 2), 'utf8')
  console.log(JSON.stringify(result))
} finally {
  socket.close()
}
