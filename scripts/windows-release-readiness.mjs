import { spawnSync } from 'node:child_process'
import { existsSync, lstatSync, readdirSync } from 'node:fs'
import { homedir } from 'node:os'
import { join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const scriptRoot = resolve(fileURLToPath(new URL('..', import.meta.url)))

export function checkFreshness(root, outputs, inputs) {
  const times = []
  const visit = (path) => {
    const info = lstatSync(join(root, path))
    if (info.isSymbolicLink()) throw new Error('Symlink input')
    if (info.isDirectory()) {
      for (const name of readdirSync(join(root, path))) {
        if (!['__pycache__', '.pytest_cache'].includes(name)) visit(`${path}/${name}`)
      }
    } else if (info.isFile()) times.push(info.mtimeMs)
  }
  try {
    inputs.forEach(visit)
    const oldestOutput = Math.min(...outputs.map((path) => {
      const info = lstatSync(join(root, path))
      if (!info.isFile() || info.isSymbolicLink()) throw new Error('Invalid output')
      return info.mtimeMs
    }))
    return times.length > 0 && oldestOutput >= Math.max(...times)
  } catch { return false }
}

export function sshProbeArgs({ target, identity, remoteRoot }) {
  if (!/^[A-Za-z0-9_.-]+@[A-Za-z0-9][A-Za-z0-9.-]*$/.test(target)) throw new Error('Invalid SSH user@host')
  if (!identity || !/^[A-Za-z]:\\[^\r\n\0]*$/.test(remoteRoot)) throw new Error('Invalid identity or Windows source path')
  const escaped = remoteRoot.replaceAll("'", "''")
  // Fixed read-only commands. No profile, application launch, DB access or env dump.
  const command = `$ErrorActionPreference = 'Stop'; $r = '${escaped}'; $node = Get-Command node.exe -ErrorAction SilentlyContinue; $npm = Get-Command npm.cmd -ErrorAction SilentlyContinue; [ordered]@{ platform = [Environment]::OSVersion.Platform.ToString(); x64 = [Environment]::Is64BitOperatingSystem; source_exists = (Test-Path -LiteralPath (Join-Path $r 'package.json') -PathType Leaf); node_available = [bool]$node; npm_available = [bool]$npm; python_available = (Test-Path -LiteralPath (Join-Path $r '.venv\\Scripts\\python.exe') -PathType Leaf); pyinstaller_available = (Test-Path -LiteralPath (Join-Path $r '.venv\\Scripts\\pyinstaller.exe') -PathType Leaf) } | ConvertTo-Json -Compress`
  return ['-F', 'none', '-i', identity, '-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes',
    '-o', 'StrictHostKeyChecking=yes', '-o', 'UpdateHostKeys=no', '-o', 'ConnectTimeout=6',
    '-o', 'ConnectionAttempts=1', '-o', 'PermitLocalCommand=no', '-o', 'ClearAllForwardings=yes',
    target, 'powershell.exe', '-NoLogo', '-NoProfile', '-NonInteractive', '-EncodedCommand',
    Buffer.from(command, 'utf16le').toString('base64')]
}

export function probeSsh(options, run = spawnSync) {
  const result = run('ssh', sshProbeArgs(options), {
    encoding: 'utf8', timeout: 15000, maxBuffer: 32768, windowsHide: true,
  })
  if (result.error || result.status !== 0) {
    const stderr = result.stderr ?? ''
    const reason = /Permission denied/i.test(stderr) ? 'authentication_failed'
      : /Host key verification failed|REMOTE HOST IDENTIFICATION HAS CHANGED/i.test(stderr) ? 'host_key_failed'
        : /timed out|ETIMEDOUT/i.test(`${stderr} ${result.error?.code}`) ? 'timeout'
          : 'connection_or_command_failed'
    return { status: 'blocked', connection: 'unverified', reason }
  }
  try {
    const data = JSON.parse(result.stdout.trim())
    const flags = ['x64', 'source_exists', 'node_available', 'npm_available', 'python_available', 'pyinstaller_available']
    if (!data || !flags.every((key) => typeof data[key] === 'boolean') || data.platform !== 'Win32NT') {
      return { status: 'blocked', connection: 'passed', reason: 'unexpected_probe_response' }
    }
    return { status: flags.every((key) => data[key]) ? 'passed' : 'blocked', connection: 'passed',
      platform: 'Win32NT', ...Object.fromEntries(flags.map((key) => [key, data[key]])) }
  } catch { return { status: 'blocked', connection: 'passed', reason: 'unexpected_probe_response' } }
}

function main() {
  const args = process.argv.slice(2)
  const options = { root: scriptRoot, staticOnly: false, remoteRoot: 'C:\\HSC\\source-20260904-1250' }
  const valued = { '--root': 'root', '--ssh-target': 'target', '--identity': 'identity', '--remote-root': 'remoteRoot' }
  for (let i = 0; i < args.length; i++) {
    if (args[i] === '--static-only') options.staticOnly = true
    else if (args[i] === '--ssh-only') options.sshOnly = true
    else if (valued[args[i]] && args[i + 1] && !args[i + 1].startsWith('--')) {
      const key = valued[args[i]]
      options[key] = args[++i]
    }
    else throw new Error('Usage: node scripts/windows-release-readiness.mjs [--root PATH] [--static-only | --ssh-only] [--ssh-target USER@HOST --identity PATH --remote-root WINDOWS_PATH]')
  }
  if (options.identity && !options.target) throw new Error('--identity requires --ssh-target')
  if (options.sshOnly && (!options.target || options.staticOnly)) throw new Error('--ssh-only requires --ssh-target and cannot be combined with --static-only')
  if (options.sshOnly) {
    const ssh = probeSsh({ ...options, identity: options.identity ?? join(homedir(), '.ssh/hsc_windows_deploy') })
    console.log(JSON.stringify({ status: ssh.status === 'passed' ? 'remote_prerequisites_present' : 'blocked',
      remote_root: options.remoteRoot, ssh,
      boundary: 'Read-only SSH and path checks only; not a dependency, build or deployment approval.',
    }, null, 2))
    process.exitCode = ssh.status === 'passed' ? 0 : 1
    return
  }
  const root = resolve(options.root)
  const gate = spawnSync(process.execPath, [join(scriptRoot, 'scripts/verify-windows-package-inputs.mjs'),
    ...(options.staticOnly ? ['--static-only'] : [])], { cwd: root, encoding: 'utf8', timeout: 60000, maxBuffer: 65536 })
  const checks = {
    package_inputs: gate.status === 0 && !gate.error ? 'passed' : 'blocked',
    frontend_freshness: checkFreshness(root, ['out/main/index.js', 'out/preload/index.js', 'out/renderer/index.html'],
      ['src', 'package.json', 'package-lock.json', 'electron.vite.config.ts']) ? 'passed' : 'blocked',
  }
  if (!options.staticOnly) {
    checks.native_windows_x64 = process.platform === 'win32' && process.arch === 'x64' ? 'passed' : 'blocked'
    checks.agent_freshness = checkFreshness(root, ['dist/home-study-local-agent.exe'],
      ['services/local_agent/src', 'services/local_agent/pyproject.toml', 'build/pyinstaller-hooks', 'package.json']) ? 'passed' : 'blocked'
    checks.smtp_worker_freshness = checkFreshness(root, ['dist/hsc-smtp-worker/hsc-smtp-worker.exe', 'dist/hsc-smtp-worker/worker-manifest.json'],
      ['services/local_agent/src/home_study_agent/smtp_owned_worker.py', 'scripts/smtp-worker-manifest.mjs', 'package.json']) ? 'passed' : 'blocked'
    for (const [name, binary, versionArgs] of [
      ['python', join(root, '.venv/Scripts/python.exe'), ['--version']],
      ['pyinstaller', join(root, '.venv/Scripts/pyinstaller.exe'), ['--version']],
    ]) {
      if (process.platform !== 'win32' || !existsSync(binary)) checks[name] = 'blocked'
      else {
        const result = spawnSync(binary, versionArgs, { encoding: 'utf8', timeout: 15000, maxBuffer: 8192 })
        checks[name] = result.status === 0 && !result.error
          && (name !== 'python' || /^Python 3\.11\./.test(result.stdout.trim())) ? 'passed' : 'blocked'
      }
    }
    checks.node_24 = Number(process.versions.node.split('.')[0]) === 24 ? 'passed' : 'blocked'
  }
  let ssh
  if (options.target) {
    ssh = probeSsh({ ...options, identity: options.identity ?? join(homedir(), '.ssh/hsc_windows_deploy') })
    checks.ssh_probe = ssh.status
  }
  const passed = Object.values(checks).every((status) => status === 'passed')
  console.log(JSON.stringify({
    status: passed ? (options.staticOnly ? 'static_checks_passed' : 'build_inputs_ready') : 'blocked',
    checks, ...(ssh ? { ssh } : {}),
    next_commands: options.staticOnly
      ? ['npm run build', 'node scripts/windows-release-readiness.mjs --static-only']
      : ['npm run build:agent:win', 'npm run build', 'node scripts/windows-release-readiness.mjs', 'npm run pack:win'],
    boundary: 'Read-only preflight; timestamps are not build provenance. No install, runtime smoke, family data, cloud quality or child outcome verification.',
  }, null, 2))
  process.exitCode = passed ? 0 : 1
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try { main() } catch (error) { console.error(error.message); process.exitCode = 2 }
}
