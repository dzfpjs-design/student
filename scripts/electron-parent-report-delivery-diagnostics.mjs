import { writeFile } from 'node:fs/promises'
import { join } from 'node:path'

export function redactHarnessDiagnostic(value, secrets = []) {
  let text = String(value ?? '')
  for (const secret of secrets) if (secret) text = text.split(secret).join('[redacted]')
  return text.replace(/((?:sessionToken|authorization|password|api[_-]?key)\s*["']?\s*[:=]\s*["']?)([^\s,"'}]+)/gi, '$1[redacted]')
}

export function runtimeDiagnostic(runtime) {
  if (!runtime) return null
  return Object.fromEntries(['status', 'error', 'recoveryAttempt', 'nextRetryAt']
    .filter((key) => runtime[key] !== undefined).map((key) => [key, runtime[key]]))
}

// Read the ownership registry, not the caller's assignment: startup may throw first.
export async function captureOwnedFailure({ outputDir, temporaryRoot, apps, error, secrets = [], smtpMessages = 0 }) {
  const redact = (value) => redactHarnessDiagnostic(value, secrets)
  const failures = []
  const save = async (name, value) => {
    try { await writeFile(join(outputDir, name), redact(value), { mode: 0o600 }) }
    catch (error) { failures.push({ artifact: name, code: error.code ?? 'WRITE_FAILED' }) }
  }
  const states = []
  for (const [index, app] of apps.entries()) {
    const prefix = `owned-${index + 1}`
    states.push({ prefix, pid: app.child.pid ?? null, exit_code: app.child.exitCode,
      signal_code: app.child.signalCode, phase: app.phase, cdp_port: app.cdpPort,
      spawn_error: app.spawnError, runtime: runtimeDiagnostic(app.runtime),
      event_methods: (app.cdp?.events ?? []).slice(-100).map((event) => event.method) })
    await save(`${prefix}-electron.log`, app.logs())
    if (app.cdp) {
      try { await save(`${prefix}-ui.txt`, await app.cdp.evaluate('document.body.innerText')) }
      catch { failures.push({ artifact: `${prefix}-ui.txt`, code: 'CDP_UNAVAILABLE' }) }
    }
  }
  await save('failure-diagnostics.json', JSON.stringify({ synthetic_only: true, external_delivery: false,
    error: String(error.stack ?? error), retained_fixture: temporaryRoot, smtp_messages: smtpMessages,
    owned: states, capture_errors: failures }, null, 2) + '\n')
  return failures
}
