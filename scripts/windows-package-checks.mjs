import { closeSync, lstatSync, openSync, readSync, readdirSync } from 'node:fs'
import { join, posix, win32 } from 'node:path'

export function validateBackendBuildVersion(desktopVersion, pythonSource, projectMetadata) {
  const python = [...pythonSource.matchAll(/^BUILD_VERSION\s*=\s*['"]([^'"]+)['"]\s*$/gm)]
  const project = [...projectMetadata.matchAll(/^version\s*=\s*['"]([^'"]+)['"]\s*$/gm)]
  if (typeof desktopVersion !== 'string' || !/^\d+\.\d+\.\d+$/.test(desktopVersion)
      || python.length !== 1 || project.length !== 1
      || python[0][1] !== desktopVersion || project[0][1] !== desktopVersion) {
    return ['Desktop and backend source build versions must match exactly']
  }
  return []
}

export function validateModelManifest(manifest) {
  const failures = []
  const safePath = (value) => typeof value === 'string' && value.length > 0
    && !posix.isAbsolute(value) && !win32.isAbsolute(value) && !value.includes('\\')
    && !/[<>:"|?*\x00-\x1f]/.test(value)
    && !value.split('/').some((part) => !part || /[. ]$/.test(part)
      || /^(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)/i.test(part))
  if (!manifest || typeof manifest !== 'object' || Array.isArray(manifest)) {
    return ['Model manifest must be an object']
  }
  if (!safePath(manifest.model_dir)) failures.push('Model manifest has an unsafe model_dir')
  if (!manifest.files || typeof manifest.files !== 'object' || Array.isArray(manifest.files)
      || Object.keys(manifest.files).length === 0) {
    failures.push('Model manifest must declare nonempty files')
  } else {
    for (const [name, entry] of Object.entries(manifest.files)) {
      if (!safePath(name)) failures.push('Model manifest has an unsafe file path')
      if (!entry || !Number.isSafeInteger(entry.minimum_bytes) || entry.minimum_bytes < 1
          || typeof entry.sha256 !== 'string' || !/^[a-f0-9]{64}$/.test(entry.sha256)) {
        failures.push('Model manifest has an invalid size or SHA-256 entry')
      }
    }
  }
  return failures
}

export function validatePackageScope(build) {
  const failures = []
  const expectedResources = [
    { from: 'dist/home-study-local-agent.exe', to: 'local-agent/home-study-local-agent.exe' },
    { from: 'dist/hsc-smtp-worker', to: 'local-agent/smtp-worker' },
    { from: 'services/local_agent/models', to: 'local-models' },
    { from: 'services/local_agent/model-manifest.json', to: 'local-models/model-manifest.json' },
    { from: 'dist/knowledge/atomic-knowledge.db', to: 'knowledge/atomic-knowledge.db' },
    { from: 'dist/knowledge/atomic-knowledge-manifest.json', to: 'knowledge/atomic-knowledge-manifest.json' },
  ]
  const resources = build?.extraResources
  if (!Array.isArray(resources) || resources.length !== expectedResources.length
      || expectedResources.some((expected) => resources.filter((entry) => entry
        && Object.keys(entry).length === 2 && entry.from === expected.from && entry.to === expected.to).length !== 1)) {
    failures.push('Packaging resources must match the audited agent/model allowlist')
  }
  if (!Array.isArray(build?.files) || build.files.length !== 2
      || !build.files.includes('out/**/*') || !build.files.includes('package.json')) {
    failures.push('Packaging files must be limited to out/**/* and package.json')
  }
  if (build?.extraFiles && (!Array.isArray(build.extraFiles) || build.extraFiles.length)) {
    failures.push('Unaudited extraFiles are not allowed')
  }
  for (const platform of ['win', 'nsis']) {
    if (['files', 'extraFiles', 'extraResources'].some((key) => build?.[platform]?.[key] !== undefined)) {
      failures.push('Platform payload overrides require a separate packaging audit')
    }
  }
  return failures
}

export function validatePackageCommands(scripts) {
  const expected = {
    'verify:package:win': 'node scripts/verify-windows-package-inputs.mjs',
    'verify:package:win:static': 'node scripts/verify-windows-package-inputs.mjs --static-only',
    'build:smtp-worker:win': '.venv\\Scripts\\pyinstaller.exe --noconfirm --clean --onedir --name hsc-smtp-worker --distpath dist services/local_agent/src/home_study_agent/smtp_owned_worker.py && node scripts/smtp-worker-manifest.mjs dist/hsc-smtp-worker',
    'pack:win': 'npm run build:smtp-worker:win && npm run build && npm run verify:package:win && electron-builder --win nsis',
  }
  return Object.entries(expected).filter(([key, value]) => scripts?.[key] !== value)
    .map(([key]) => `Unaudited packaging command: ${key}; full gate must not be skipped or replaced by static mode`)
}

// Inspect names and file types only. Never read suspected private payloads.
export function inspectPackageTree(root, relativePath) {
  const failures = []
  const visit = (path) => {
    const info = lstatSync(join(root, path))
    if (info.isSymbolicLink()) {
      failures.push('Package payload contains a symbolic link')
      return
    }
    const name = path.split('/').at(-1)
    if (/^(?:\.env(?:\..*)?|family-data|userData|credentials?|secrets?|backups?|diagnostics|logs|\.ssh)$/i.test(name)
        || /\.(?:db(?:-(?:wal|shm))?|sqlite(?:3)?(?:-(?:wal|shm))?|pem|key|dpapi|jsonl|log)$/i.test(name)) {
      failures.push('Package payload contains a forbidden data/credential/log path')
      return
    }
    if (info.isDirectory()) {
      for (const entry of readdirSync(join(root, path))) visit(`${path}/${entry}`)
    } else if (!info.isFile()) {
      failures.push('Package payload contains a non-regular file')
    }
  }
  try { visit(relativePath) } catch { failures.push('Package payload is missing or unreadable') }
  return [...new Set(failures)]
}

export function validateWindowsAgent(path, minimumBytes = 5_000_000) {
  let fd
  try {
    const info = lstatSync(path)
    if (!info.isFile() || info.isSymbolicLink() || info.size < minimumBytes) {
      return [`Windows agent must be a regular executable of at least ${minimumBytes} bytes`]
    }
    fd = openSync(path, 'r')
    const dos = Buffer.alloc(64)
    if (readSync(fd, dos, 0, 64, 0) !== 64 || dos.toString('ascii', 0, 2) !== 'MZ') {
      return ['Windows agent is not a PE executable']
    }
    const offset = dos.readUInt32LE(60)
    const pe = Buffer.alloc(26)
    if (offset < 64 || offset + pe.length > info.size
        || readSync(fd, pe, 0, pe.length, offset) !== pe.length
        || pe.readUInt32LE(0) !== 0x00004550) return ['Windows agent has an invalid PE header']
    if (pe.readUInt16LE(4) !== 0x8664 || pe.readUInt16LE(24) !== 0x20b
        || !(pe.readUInt16LE(22) & 0x0002) || (pe.readUInt16LE(22) & 0x2000)) {
      return ['Windows agent must be an x64 PE32+ executable, not a DLL']
    }
    const sections = pe.readUInt16LE(6)
    const optionalSize = pe.readUInt16LE(20)
    const sectionStart = offset + 24 + optionalSize
    if (sections < 1 || sections > 96 || optionalSize < 112 || sectionStart + sections * 40 > info.size) {
      return ['Windows agent has an invalid optional header or section table']
    }
    const table = Buffer.alloc(sections * 40)
    if (readSync(fd, table, 0, table.length, sectionStart) !== table.length) {
      return ['Windows agent has a truncated section table']
    }
    for (let i = 0; i < sections; i++) {
      const size = table.readUInt32LE(i * 40 + 16)
      const start = table.readUInt32LE(i * 40 + 20)
      if (size && (start < sectionStart + table.length || start + size > info.size)) {
        return ['Windows agent has a section outside the executable']
      }
    }
    return []
  } catch {
    return ['Windows agent is missing or unreadable']
  } finally {
    if (fd !== undefined) closeSync(fd)
  }
}
