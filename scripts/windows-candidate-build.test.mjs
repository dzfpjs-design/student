import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const source = readFileSync(new URL('./build-windows-material-candidate.ps1', import.meta.url), 'utf8')

test('Python source binding avoids double quotes lost by Windows PowerShell native argument passing', () => {
  const argument = source.match(/\n\s*'(from pathlib import Path;[^\n]+)'\)/)?.[1]
  assert.ok(argument)
  assert.equal(argument.includes('"'), false)
  assert.match(argument, /assert source\.is_relative_to\(root\)/)
  assert.match(argument, /''Python package resolves outside candidate: ''/)
})

test('candidate source binding precedes build and cannot reuse old source silently', () => {
  assert.match(source, /Only an isolated candidates directory is allowed/)
  assert.match(source, /\$env:PYTHONPATH = \[IO.Path\]::GetFullPath\(\(Join-Path \$root 'services\\local_agent\\src'\)\)/)
  assert.ok(source.indexOf("Checked 'python-source-binding'") < source.indexOf("Checked 'python-build'"))
  assert.match(source, /if \(\$exitCode -ne 0\) \{ throw/)
})

test('desktop smoke retains the process handle and evidence before reporting exit status', () => {
  const desktop = readFileSync(new URL('./verify-windows-candidate-desktop.ps1', import.meta.url), 'utf8')
  assert.ok(desktop.indexOf('$null = $process.Handle') < desktop.indexOf('$process.WaitForExit(90000)'))
  assert.ok(desktop.indexOf('$process.Refresh()') < desktop.indexOf('$process.ExitCode -ne 0'))
  assert.match(desktop, /RedirectStandardError/)
  assert.match(desktop, /HSC_RUNTIME_MODE = 'trial'/)
  assert.match(desktop, /HSC_PILOT_TEST_EMAIL_ENABLED = '0'/)
  assert.ok(desktop.indexOf("Set-Content -Encoding UTF8 'candidate-desktop-status.json'") < desktop.indexOf('if ($failure) { throw $failure }'))
})

test('actual UI acceptance isolates trial data and never captures household media or desktop', () => {
  const runner = readFileSync(new URL('./verify-windows-candidate-ui.ps1', import.meta.url), 'utf8')
  const probe = readFileSync(new URL('./verify-windows-candidate-ui.mjs', import.meta.url), 'utf8')
  assert.match(runner, /HSC_RUNTIME_MODE = 'trial'/)
  assert.match(runner, /HSC_PILOT_TEST_EMAIL_ENABLED = '0'/)
  assert.match(runner, /--use-fake-device-for-media-stream/)
  assert.match(runner, /--remote-debugging-address=127\.0\.0\.1/)
  assert.match(runner, /taskkill\.exe \/PID \$process\.Id \/T \/F/)
  assert.match(probe, /runtime\.dataScope !== 'trial'/)
  assert.match(probe, /Page\.captureScreenshot/)
  assert.match(probe, /file:/)
  assert.match(probe, /PIN setup did not persist across application restart/)
  assert.doesNotMatch(probe, /desktopCapturer|capture_screen|getDisplayMedia|sessionToken:\s*runtime/)
})

test('missing-key probe preserves the original key and database and cannot erase a conflicting new key', () => {
  const runner = readFileSync(new URL('./verify-windows-candidate-ui.ps1', import.meta.url), 'utf8')
  const probe = readFileSync(new URL('./verify-windows-candidate-ui.mjs', import.meta.url), 'utf8')
  assert.match(runner, /'setup', 'restart', 'key-loss', 'recovered'/)
  assert.match(runner, /Move-Item -LiteralPath \$keyPath -Destination \$originalKey/)
  assert.match(runner, /if \(Test-Path \$keyPath\) \{ throw 'Missing key was silently replaced/)
  assert.match(runner, /Synthetic database changed during key loss/)
  assert.match(runner, /Test-Path \$originalKey\) -and -not \(Test-Path \$keyPath/)
  assert.doesNotMatch(runner, /Remove-Item.*keyPath|Move-Item.*-Force/)
  assert.match(probe, /backend_not_started: !runtime.localApiUrl && !runtime.sessionToken/)
  assert.match(probe, /Missing key did not fail before local service launch/)
  assert.match(probe, /Protected error was overwritten by automatic recovery/)
  assert.match(probe, /document\.body\?\.textContent \?\? ''/)
  assert.match(runner, /Existing acceptance status cannot be overwritten/)
  assert.match(probe, /basename\(output, '\.json'\)/)
})
