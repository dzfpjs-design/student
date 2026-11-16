import React from 'react'
import ReactDOM from 'react-dom/client'
import { App } from './App'
import './styles.css'

function reportRendererFailure(event: DesktopRendererDiagnosticEvent): void {
  void window.desktop?.recordRendererDiagnostic?.(event).catch(() => undefined)
}

window.addEventListener('error', (event) => {
  reportRendererFailure({
    event: 'window_error',
    level: 'error',
    message: event.message,
    details: { filename: event.filename, line: event.lineno, column: event.colno, stack: event.error instanceof Error ? event.error.stack : undefined }
  })
})

window.addEventListener('unhandledrejection', (event) => {
  const reason = event.reason
  reportRendererFailure({
    event: 'unhandled_rejection',
    level: 'error',
    message: reason instanceof Error ? reason.message : String(reason),
    details: { stack: reason instanceof Error ? reason.stack : undefined }
  })
})

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
)
